"""汇总评测：把所有 harness（含 Sluice 优化前/后）的 patch 一次性交给官方 swebench 打分，
并输出对比表 + 报告 markdown。

用法：python bench/final_eval.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "bench" / "results"
MODEL = "deepseek-v4.1-flash"
RUN_ID = "final"

# (标签, 记录文件, 变体)
GROUPS = [
    ("sluice-v1", RESULTS / "archive_v1" / "records_pilot_sluice_v1.jsonl", "v1"),
    ("sluice-v2", RESULTS / "records_pilot_sluice.jsonl", "v2"),
    ("mini-swe-agent", RESULTS / "records_pilot_mini-swe-agent.jsonl", ""),
    ("mini-agent-harness", RESULTS / "records_pilot_mini-agent-harness.jsonl", ""),
]


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def touches_tests(patch: str) -> bool:
    """patch 是否改动了测试文件（防作弊检查）。"""
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            path = line.split(" b/")[-1]
            if re.search(r"(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$", path):
                return True
    return False


def eval_group(label: str, rows: list[dict]) -> set[str]:
    """逐组单独跑官方评分（swebench 会按 instance_id 去重，混在一起只留最后一组）。"""
    preds = RESULTS / f"preds_{label}.jsonl"
    with preds.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps({
                "instance_id": r["instance_id"],
                "model_name_or_path": f"{MODEL}__{label}",
                "model_patch": r.get("patch", ""),
            }, ensure_ascii=False) + "\n")
    run_id = f"grp-{label}"
    cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
           "--predictions_path", str(preds),
           "--dataset_name", str(ROOT / "bench" / "dataset_pilot.jsonl"),
           "--run_id", run_id, "--max_workers", "2"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [l for l in (proc.stdout or "").splitlines() if "Instances resolved" in l or "Total instances" in l]
    print(f"  [{label}] " + " | ".join(tail))
    reports = list(ROOT.glob(f"*{run_id}.json"))
    if not reports:
        return set()
    data = json.loads(max(reports, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8"))
    return set(data.get("resolved_ids") or [])


def main() -> int:
    groups: dict[str, list[dict]] = {}
    for label, path, variant in GROUPS:
        rows = load(path)
        if not rows:
            print(f"[skip] {label}: 无记录")
            continue
        groups[label] = rows

    resolved_by_group = {}
    for label, rows in groups.items():
        print(f"\n=== 评分 {label} ({len(rows)} 例) ===", flush=True)
        resolved_by_group[label] = eval_group(label, rows)

    lines = ["| harness | resolved | 平均墙钟 | 平均步数 | 平均 patch | 改测试 |",
             "| --- | --- | --- | --- | --- | --- |"]
    summary = {}
    for label, rows in groups.items():
        resolved = resolved_by_group.get(label, set())
        n = len(rows)
        res = sum(1 for r in rows if r["instance_id"] in resolved)
        wall = sum(r.get("wall_seconds", 0) for r in rows) / max(n, 1)
        steps = sum(r.get("tool_calls", 0) for r in rows) / max(n, 1)
        patch = sum(len(r.get("patch", "")) for r in rows) / max(n, 1)
        touched = sum(1 for r in rows if touches_tests(r.get("patch", "")))
        summary[label] = {"n": n, "resolved": res, "wall": wall, "steps": steps,
                          "patch": patch, "touched_tests": touched,
                          "resolved_ids": sorted({r["instance_id"] for r in rows} & resolved)}
        lines.append(f"| {label} | {res}/{n} | {wall:.0f}s | {steps:.0f} | {patch:.0f}B | {touched} |")

    table = "\n".join(lines)
    print("\n===== 最终对比（SWE-bench Verified pilot 子集，同模型）=====\n" + table)

    out = RESULTS / "final_comparison.md"
    out.write_text(f"# 对比结果\n\n模型：`{MODEL}`\n\n{table}\n\n明细：\n\n```json\n"
                   + json.dumps(summary, ensure_ascii=False, indent=2) + "\n```\n", encoding="utf-8")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
