"""评测驱动：对 (harness × instance) 跑一遍，收集 patch 与指标。

用法：
    python bench/run.py --subset pilot --harness sluice --limit 1
    python bench/run.py --subset pilot --harness all
    python bench/run.py --subset pilot --harness all --evaluate   # 跑完直接评分
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.adapters import common  # noqa: E402

RESULTS = ROOT / "bench" / "results"
WORKSPACES = ROOT / "bench" / "workspaces"

ADAPTERS = {
    "sluice": "bench.adapters.sluice_adapter",
    "mini-agent-harness": "bench.adapters.mah_adapter",
    "mini-swe-agent": "bench.adapters.msa_adapter",
    "pi": "bench.adapters.pi_adapter",
}


def load_subset(tier: str) -> list[dict]:
    path = ROOT / "bench" / f"subset_{tier}.jsonl"
    if not path.exists():
        raise SystemExit(f"子集不存在: {path}（先跑 python bench/subset.py {tier}）")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_one(harness: str, instance: dict, cfg: dict, timeout: int) -> dict:
    import importlib

    adapter = importlib.import_module(ADAPTERS[harness])
    inst_id = instance["instance_id"]
    ws = WORKSPACES / inst_id
    container = f"sw-{inst_id.replace('__', '-').replace('_', '-').lower()[:40]}"
    print(f"\n=== {harness} × {inst_id} ===", flush=True)

    t0 = time.time()
    common.extract_workspace(instance["image"], ws)
    print(f"  工作区就绪 ({time.time()-t0:.0f}s)", flush=True)

    # sluice / mah 走共享容器；msa 自己管容器
    if harness in ("sluice", "mini-agent-harness"):
        common.start_container(instance["image"], container, ws)
    try:
        rec = adapter.run(instance, ws, container, cfg, timeout=timeout)
    finally:
        if harness in ("sluice", "mini-agent-harness"):
            common.stop_container(container)

    rec["resolved"] = None  # 由 swebench 评分回填
    print(f"  exit={rec['exit_code']} wall={rec['wall_seconds']}s "
          f"tools={rec['tool_calls']} patch={len(rec['patch'])}B", flush=True)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", default="pilot")
    ap.add_argument("--harness", default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    cfg = {
        "base_url": os.environ["SLUICE_BASE_URL"],
        "api_key": os.environ["SLUICE_API_KEY"],
        "model": os.environ.get("SLUICE_MODEL", "deepseek-v4.1-flash"),
        "judge_model": os.environ.get("SLUICE_JUDGE_MODEL"),
        "max_steps": int(os.environ.get("BENCH_MAX_STEPS", "30")),
    }
    instances = load_subset(args.subset)
    harnesses = list(ADAPTERS) if args.harness == "all" else [args.harness]
    harnesses = [h for h in harnesses if h in ADAPTERS]

    RESULTS.mkdir(parents=True, exist_ok=True)
    if args.harness == "all":
        out_path = RESULTS / f"records_{args.subset}.jsonl"
    else:
        out_path = RESULTS / f"records_{args.subset}_{args.harness}.jsonl"
    done = set()
    if args.resume and out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done.add((r["harness"], r["instance_id"]))

    records = []
    with out_path.open("a" if args.resume else "w", encoding="utf-8") as f:
        for harness in harnesses:
            for inst in instances:
                if (harness, inst["instance_id"]) in done:
                    continue
                if args.limit and len(records) >= args.limit:
                    break
                try:
                    rec = run_one(harness, inst, cfg, args.timeout)
                except Exception as e:  # noqa: BLE001
                    rec = {"harness": harness, "instance_id": inst["instance_id"],
                           "error": f"{type(e).__name__}: {e}", "patch": "",
                           "wall_seconds": 0, "tool_calls": 0, "exit_code": -1}
                    print(f"  [失败] {rec['error']}", flush=True)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                records.append(rec)

    print(f"\n记录 {len(records)} 条 → {out_path}")

    if args.evaluate and records:
        from bench import evaluate
        evaluate.main(["--subset", args.subset, "--model", cfg["model"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
