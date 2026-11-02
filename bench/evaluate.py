"""用官方 swebench 评测 predictions，并输出可比的对比表。

用法：
    python bench/evaluate.py --subset pilot --model deepseek-v4.1-flash
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "bench" / "results"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", default="pilot")
    ap.add_argument("--model", default="deepseek-v4.1-flash")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--records", default=None, help="指定 records 文件（默认 records_<subset>.jsonl）")
    args = ap.parse_args(argv)

    recs_path = Path(args.records) if args.records else RESULTS / f"records_{args.subset}.jsonl"
    records = [json.loads(l) for l in recs_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    preds = RESULTS / f"preds_{args.subset}.jsonl"
    with preds.open("w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps({
                "instance_id": r["instance_id"],
                "model_name_or_path": f"{args.model}__{r['harness']}",
                "model_patch": r.get("patch", ""),
            }, ensure_ascii=False) + "\n")

    run_id = args.run_id or f"cmp-{args.subset}"
    cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
           "--predictions_path", str(preds),
           "--dataset_name", str(ROOT / "bench" / f"dataset_{args.subset}.jsonl"),
           "--run_id", run_id,
           "--max_workers", "2"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = "\n".join((proc.stdout or "").splitlines()[-16:])
    print(tail)

    reports = list(ROOT.glob(f"*.{run_id}.json"))
    if not reports:
        print(f"[warn] 未找到报告 *.{run_id}.json")
        return 1
    data = json.loads(max(reports, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8"))
    resolved = set(data.get("resolved_ids") or [])

    # 逐 harness 汇总
    summary: dict[str, dict] = {}
    for r in records:
        h = r["harness"]
        s = summary.setdefault(h, {"total": 0, "resolved": 0, "patch_bytes": 0,
                                   "wall": 0.0, "tools": 0, "judge": 0})
        s["total"] += 1
        s["resolved"] += 1 if r["instance_id"] in resolved else 0
        s["patch_bytes"] += len(r.get("patch", ""))
        s["wall"] += r.get("wall_seconds", 0)
        s["tools"] += r.get("tool_calls", 0)
        s["judge"] += r.get("judge_decisions", 0)

    print("\n===== 对比（同模型）=====")
    print(f"{'harness':<20} {'resolved':>10} {'avg wall':>10} {'avg tools':>10} {'avg patch':>10}")
    for h, s in summary.items():
        n = max(s["total"], 1)
        print(f"{h:<20} {s['resolved']}/{s['total']:<8} {s['wall']/n:>9.0f}s "
              f"{s['tools']/n:>10.1f} {s['patch_bytes']/n:>9.0f}B")

    out = RESULTS / f"summary_{args.subset}.json"
    out.write_text(json.dumps({h: {**s, "resolved_ids": sorted(resolved & {
        r["instance_id"] for r in records if r["harness"] == h})} for h, s in summary.items()},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
