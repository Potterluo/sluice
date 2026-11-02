"""构建评测用本地数据集：把 SWE-bench 官方 test parquet 裁剪到我们的子集，
并把 image 字段指向 LogicStar 压缩镜像（无需重打 tag、无需 --namespace）。

产出：bench/dataset_<tier>.jsonl（swebench run_evaluation 可直接读取的 --dataset_name）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
FULL = ROOT / "data" / "swebench_full_test.parquet"


def logicstar_image(instance_id: str) -> str:
    """官方 image 名 -> LogicStar 压缩镜像名（double underscore 换成 _1776_）。"""
    return f"logicstar/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


def main() -> int:
    tier = sys.argv[1] if len(sys.argv) > 1 else "pilot"
    subset_path = ROOT / f"subset_{tier}.jsonl"
    if not subset_path.exists():
        raise SystemExit(f"先跑 subset.py 生成 {subset_path}")

    ids = [json.loads(l)["instance_id"] for l in subset_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = {r["instance_id"]: r for r in pq.read_table(FULL).to_pylist()}

    out = []
    for iid in ids:
        if iid not in rows:
            print(f"[warn] 官方数据集里没有 {iid}", file=sys.stderr)
            continue
        r = dict(rows[iid])
        r["image"] = logicstar_image(iid)
        out.append(r)

    path = ROOT / f"dataset_{tier}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"数据集 {tier}: {len(out)} 例 → {path}")
    for r in out:
        print(f"  {r['instance_id']:<42} image={r['image']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
