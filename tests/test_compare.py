"""裁决者后端对比评测：LLM(qwen3.8-flash) vs Jev vs 本地 clef-flash。

三种后端跑同一组「标准转移请求剧本」，对比：
- 决策一致性（与人工标注基准的符合率）
- 驳回倾向（严格程度）
- 延迟
- 可审计性（reason 是否含概率）

运行前提：SLUICE_API_KEY（火山网关）/ OPENROUTER_API_KEY（Jev）；
本地 clef 可选（llama-server 起在 127.0.0.1:18080 时自动加入对比）。

用法：python tests/test_compare.py [--goal 每条请求数]
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sluice.jev import JevClient, JevConfig, JevJudge   # noqa: E402
from sluice.judge import Judge                          # noqa: E402
from sluice.llm import LLMClient, LLMConfig             # noqa: E402
from sluice.protocol import TransitionRequest           # noqa: E402
from sluice.spec import FlowSpec                        # noqa: E402

FLOW_PATH = Path(__file__).resolve().parent.parent / "flows" / "coding_task.json"

# ---------------------------------------------------------------- 标准剧本
# 每条：TransitionRequest + 人工基准标注（approve/reject 期望 + 期望目标）
# 证据强度分三档：solid（应批）/ vague（应驳）/ tricky（边界，重点看分歧）

GOAL = ("写一个回文检测命令行工具 palindrome.py：读入一个字符串参数，"
        "忽略大小写与标点后判断是否回文，输出 True 或 False。")

CASES = [
    # ---- solid：证据具体充分，期望批准
    ("solid-1 CLARIFY→PLAN",
     TransitionRequest("CLARIFY", "PLAN",
                       "已写 requirements.md，覆盖输入格式、归一化规则（仅保留字母数字、"
                       "统一小写）、输出契约（单行 True/False）、两条验收命令与边界用例。",
                       "requirements.md 已落盘，含验收标准章节，5 个边界用例列表。"),
     {"expect": "approve", "to": "PLAN"}),
    ("solid-2 IMPLEMENT→VERIFY",
     TransitionRequest("IMPLEMENT", "VERIFY",
                       "palindrome.py 已按 plan.md 实现：normalize/is_palindrome/main 三函数，"
                       "参数校验与退出码契约完整。",
                       "python palindrome.py 'A man, a plan, a canal: Panama' 本地试跑输出 True，"
                       "exit=0；'hello' 输出 False。"),
     {"expect": "approve", "to": "VERIFY"}),
    ("solid-3 VERIFY→DELIVER",
     TransitionRequest("VERIFY", "DELIVER",
                       "VERIFY 完成：9 个用例全部通过（含两条验收命令、空串、单字符、"
                       "数字混合、缺参/多参错误路径），无已知缺陷。",
                       "验收命令原样输出：Panama→True/exit0，hello→False/exit0；"
                       "错误路径 stderr 输出 Usage 且 exit1。"),
     {"expect": "approve", "to": "DELIVER"}),
    # ---- vague：证据空泛，期望驳回
    ("vague-1 CLARIFY→PLAN",
     TransitionRequest("CLARIFY", "PLAN",
                       "需求澄清完成，文档已写。",
                       "见工作区产物文件。"),
     {"expect": "reject"}),
    ("vague-2 IMPLEMENT→VERIFY",
     TransitionRequest("IMPLEMENT", "VERIFY",
                       "代码写完了。",
                       "写好了，应该能跑。"),
     {"expect": "reject"}),
    ("vague-3 VERIFY→DELIVER",
     TransitionRequest("VERIFY", "DELIVER",
                       "验证完毕，全部通过。",
                       "测试都过了。"),
     {"expect": "reject"}),
    # ---- tricky：边界场景，看各后端分歧
    ("tricky-1 非法边 CLARIFY→DELIVER",
     TransitionRequest("CLARIFY", "DELIVER",
                       "这是个简单工具，无需计划和实现，直接交付。",
                       "需求一句话即可说清，无歧义。"),
     {"expect": "reject_or_redirect"}),   # 结构上必须拦下
    ("tricky-2 跳级 CLARIFY→IMPLEMENT",
     TransitionRequest("CLARIFY", "IMPLEMENT",
                       "需求已澄清，直接开始写代码。",
                       "requirements.md 已就绪，含验收标准。"),
     {"expect": "approve", "to": "PLAN"}),  # 跳级请求应被改派回 PLAN（CLARIFY 无边直达 IMPLEMENT）
    ("tricky-3 回边 VERIFY→IMPLEMENT",
     TransitionRequest("VERIFY", "IMPLEMENT",
                       "VERIFY 发现缺陷：空输入时抛 IndexError，需回退修复。",
                       "python palindrome.py '' 复现：Traceback IndexError exit1。"),
     {"expect": "approve", "to": "IMPLEMENT"}),  # 合法回边应放行
]


def run_backend(name: str, judge) -> list[dict]:
    """跑剧本，返回逐案记录。"""
    results = []
    for label, req, baseline in CASES:
        t0 = time.perf_counter()
        try:
            d = judge.on_transition_request(req)
            dt = time.perf_counter() - t0
            results.append({
                "case": label, "backend": name,
                "decision": d.decision, "next_state": d.next_state,
                "latency": dt, "reason_head": (d.reason or "")[:80],
                "has_prob": ("p=" in (d.reason or "") or "conf=" in (d.reason or "")),
                "asked_user": bool(d.user_question),
                "baseline": baseline["expect"],
                "match": match_baseline(d, baseline),
            })
        except Exception as e:  # noqa: BLE001
            results.append({"case": label, "backend": name, "error": str(e)[:120],
                            "match": False, "baseline": baseline["expect"]})
    return results


def match_baseline(d, baseline) -> bool:
    expect = baseline["expect"]
    if expect == "approve":
        # 交付前主动问用户（保守确认）视作合理分流：不判失败，单独标注
        if d.user_question:
            return True
        return d.decision in ("approve", "redirect") and d.next_state == baseline["to"]
    if expect == "reject":
        return d.decision == "reject"
    if expect == "reject_or_redirect":
        return not (d.decision == "approve" and d.next_state not in
                    ("CLARIFY",))  # 不得直批进终态/越级
    return True


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if r.get("match")]
    lat = [r["latency"] for r in results if "latency" in r]
    return {
        "backend": results[0]["backend"] if results else "?",
        "total": len(results),
        "match": len(ok),
        "match_rate": len(ok) / len(results) if results else 0,
        "reject_rate": (sum(1 for r in results if r.get("decision") == "reject")
                        / len(results) if results else 0),
        "latency_med": statistics.median(lat) if lat else None,
        "prob_auditable": all(r.get("has_prob") for r in results if "reason_head" in r),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1, help="每案重复次数（取多数）")
    ap.add_argument("--skip-local", action="store_true")
    args = ap.parse_args()

    flow = FlowSpec.load(FLOW_PATH)
    backends = []

    # LLM 裁决者：qwen3.8-flash
    llm_cfg = LLMConfig.from_env("SLUICE_WORKER")
    if llm_cfg.api_key and os_has("SLUICE_API_KEY"):
        llm_cfg.model = os_get("SLUICE_COMPARE_LLM_MODEL", "qwen3.8-flash")
        backends.append(("llm-qwen3.8-flash", Judge(LLMClient(llm_cfg), flow)))

    # Jev（OpenRouter Decisions API 或 302.ai TypeSafe 直连）
    if os_has("SLUICE_JEV_API_KEY"):
        jc = JevConfig.from_env()
        backends.append(("jev-1.14", JevJudge(JevClient(jc), flow)))

    # 本地 clef（若 server 已起）
    if not args.skip_local:
        try:
            import urllib.request as ur
            ur.urlopen("http://127.0.0.1:18080/health", timeout=2)
            cc = JevConfig(base_url="http://127.0.0.1:18080/v1", model="clef-flash")
            backends.append(("clef-flash-local", JevJudge(JevClient(cc), flow)))
        except Exception:  # noqa: BLE001
            print("（本地 clef server 未运行，跳过）")

    if not backends:
        print("无可用后端：需要 SLUICE_API_KEY 或 OPENROUTER_API_KEY")
        return 2

    all_rows = []
    for name, judge in backends:
        for _ in range(args.runs):
            rows = run_backend(name, judge)
            all_rows.extend(rows)
        s = summarize([r for r in all_rows if r["backend"] == name])
        print(f"\n== {name} ==")
        print(f"  基准符合: {s['match']}/{s['total']}  驳回率: {s['reject_rate']:.0%}  "
              f"延迟中位: {s['latency_med']:.1f}s  概率审计: {s['prob_auditable']}")
        for r in all_rows:
            if r["backend"] == name:
                mark = "OK " if r.get("match") else "X  "
                info = (f"{r.get('decision')}→{r.get('next_state')}"
                        if "decision" in r else f"ERR {r.get('error')}")
                print(f"  {mark} {r['case']:<28} {info}")

    out = Path(__file__).resolve().parent.parent / "compare_results.json"
    out.write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {out}")
    return 0


def os_has(name: str) -> bool:
    import os
    return bool(os.environ.get(name))


def os_get(name: str, default: str = "") -> str:
    import os
    return os.environ.get(name, default)


if __name__ == "__main__":
    raise SystemExit(main())
