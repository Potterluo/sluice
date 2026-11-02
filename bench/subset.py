"""从 SWE-bench Verified 里选固定子集，产出 bench/subset.jsonl。

策略：
- 分层抽样：小仓库（requests/flask/seaborn）优先，保证单实例跑得快；
  再补 pytest / django / sympy 覆盖不同代码库形态。
- 固定排序（instance_id 升序）+ 固定索引 → 可复现。
- 每条记录带 image 名（LogicStar 压缩镜像，需 `--namespace logicstar`）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
PARQUET = ROOT / "data" / "swebench_verified.parquet"

# 分层：仓库 -> 选取数量（按 instance_id 升序取前 N，确定性）
# 注意：psf/requests 的测试依赖 httpbin 网络，评测环境无法稳定通过（金标准也会挂），
# 因此子集只选离线可跑的仓库。
PILOT = {"pallets/flask": 1, "mwaskom/seaborn": 2, "pytest-dev/pytest": 2}
EXTENDED = {"django/django": 3, "sympy/sympy": 2}


def image_name(instance_id: str) -> str:
    """LogicStar 压缩镜像命名：double underscore -> _1776_。"""
    return f"logicstar/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}".lower()


def build(tiers: dict[str, int]) -> list[dict]:
    table = pq.read_table(PARQUET)
    rows = table.to_pylist()
    by_repo: dict[str, list[dict]] = {}
    for r in rows:
        by_repo.setdefault(r["repo"], []).append(r)

    picked = []
    for repo, n in tiers.items():
        candidates = sorted(by_repo.get(repo, []), key=lambda r: r["instance_id"])
        if not candidates:
            print(f"[warn] 仓库 {repo} 无实例", file=sys.stderr)
            continue
        picked.extend(candidates[:n])

    out = []
    for r in picked:
        out.append({
            "instance_id": r["instance_id"],
            "repo": r["repo"],
            "base_commit": r["base_commit"],
            "image": image_name(r["instance_id"]),
            "problem_statement": r["problem_statement"],
            "hints_text": (r.get("hints_text") or "")[:2000],
            "fail_to_pass": json.loads(r["FAIL_TO_PASS"]) if isinstance(r["FAIL_TO_PASS"], str) else r["FAIL_TO_PASS"],
            "pass_to_pass": json.loads(r["PASS_TO_PASS"]) if isinstance(r["PASS_TO_PASS"], str) else r["PASS_TO_PASS"],
        })
    return out


def main() -> int:
    tier = sys.argv[1] if len(sys.argv) > 1 else "pilot"
    tiers = PILOT if tier == "pilot" else {**PILOT, **EXTENDED}
    items = build(tiers)

    path = ROOT / f"subset_{tier}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    print(f"子集 {tier}: {len(items)} 例 → {path}")
    for it in items:
        print(f"  {it['instance_id']:<42} {it['repo']:<24} 镜像 {it['image'].split('/')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
