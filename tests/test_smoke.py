"""端到端冒烟测试：剧本化 MockLLM 跑完整状态机，无需真实 API。

同时被 main.py --mock 复用为演示剧本。

运行：
    python -m pytest tests/          # 若安装了 pytest
    python tests/test_smoke.py       # 或直接用内置 runner
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sluice.harness import Harness          # noqa: E402
from sluice.jev import JevJudge             # noqa: E402
from sluice.judge import Judge              # noqa: E402
from sluice.llm import MockLLM              # noqa: E402
from sluice.protocol import JudgeDecision, TransitionRequest  # noqa: E402
from sluice.spec import FlowSpec            # noqa: E402
from sluice.tools import ToolBox            # noqa: E402
from sluice.worker import Worker            # noqa: E402

FLOW_PATH = Path(__file__).resolve().parent.parent / "flows" / "coding_task.json"

# 每个状态：第一次 LLM 调用做一个业务动作，第二次发起转移请求
_SCRIPT = {
    "CLARIFY": ("write_file", {"path": "requirements.md",
                               "content": "# 需求\n- 实现 fib(n) 命令行工具\n- 验收：python fib.py 输出正确"}, "PLAN"),
    "PLAN": ("write_file", {"path": "plan.md",
                            "content": "# 计划\n1. 写 fib.py\n2. 运行验证"}, "IMPLEMENT"),
    "IMPLEMENT": ("write_file", {"path": "fib.py",
                                 "content": "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n\nprint([fib(i) for i in range(10)])\n"}, "VERIFY"),
    "VERIFY": ("run_shell", {"command": "python fib.py"}, "DELIVER"),
}


def scripted_llm(verbose: bool = False, reject_once: bool = False) -> MockLLM:
    """剧本 LLM：裁决者一律批准（可选先驳回一次），执行者按状态剧本行动。"""
    counters = {"worker": {}, "rejected": False}

    def judge_reply(payload: str) -> dict:
        def d(**kw):
            base = {"decision": "approve", "next_state": "", "reason": "剧本裁决",
                    "instructions": "继续", "user_question": None, "final_answer": None}
            base.update(kw)
            return {"role": "assistant", "content": json.dumps(base, ensure_ascii=False)}

        if "原始目标" in payload:
            return d(next_state="CLARIFY", instructions="先澄清需求并写 requirements.md")
        m = re.search(r"转移请求：(\w+) → (\w+)", payload)
        if not m:
            return d(decision="reject", reason="无法理解请求", instructions="重新组织后再请求")
        frm, to = m.group(1), m.group(2)
        if reject_once and not counters["rejected"] and frm == "CLARIFY":
            counters["rejected"] = True
            return d(decision="reject", next_state=frm,
                     reason="验收标准不够具体", instructions="在 requirements.md 中补充验收标准后重新请求")
        if to == "DELIVER":
            return d(next_state=to, reason="交付标准全部满足",
                     final_answer="交付完成：fib.py 可运行，输出 [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]。")
        return d(next_state=to, reason=f"证据充分，批准 {frm} → {to}")

    def worker_reply(messages, state: str) -> dict:
        count = counters["worker"].get(state, 0) + 1
        counters["worker"][state] = count
        has_feedback = any("【裁决反馈】" in (m.get("content") or "") for m in messages)
        if count == 1 and not has_feedback:
            name, args, _ = _SCRIPT[state]
            return {"role": "assistant", "content": None,
                    "tool_calls": [{"id": f"call_{state}_1", "name": name, "arguments": args}]}
        _, _, to = _SCRIPT[state]
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"call_{state}_{count}", "name": "request_transition",
                                "arguments": {"to_state": to,
                                              "summary": f"{state} 工作完成",
                                              "evidence": "见工作区产物文件"}}]}

    def handler(messages, tools):
        system = messages[0]["content"]
        if "你是「裁决者」" in system:
            return judge_reply(messages[-1]["content"] or "")
        state = re.search(r"当前状态：(\w+)", system).group(1)
        reply = worker_reply(messages, state)
        if verbose:
            print(f"    [mock-llm] state={state} calls={counters['worker'][state]}")
        return reply

    llm = MockLLM(handler)
    llm.counters = counters  # type: ignore[attr-defined]
    return llm


def build(workspace: str, reject_once: bool = False):
    flow = FlowSpec.load(FLOW_PATH)
    llm = scripted_llm(reject_once=reject_once)
    harness = Harness(flow=flow, judge=Judge(llm, flow),
                      worker_factory=lambda: Worker(llm, ToolBox(workspace)),
                      ask_user=lambda q: "按默认假设继续",
                      emit=lambda kind, text: None)
    return harness, llm


# ------------------------------------------------------------------ Fake Jev

class FakeJevClient:
    """Jev Decisions API 的测试替身：按序消费 policies，耗尽后用默认值。

    policy 里可指定各问题的 noul 概率（如 needs_user / criterion_0 / exit_1），
    以及 next_state / confidence 覆盖 choice 答案；默认条件类 noul=0.9、
    needs_user=0.05、choice=执行者请求的目标状态、confidence=0.8
    （即「全部批准、不需要用户」）。
    """

    def __init__(self, policies: list[dict] | None = None):
        self.policies = list(policies or [])
        self.calls: list[dict] = []

    def ask(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        policy = self.policies.pop(0) if self.policies else {}
        requested = (state.get("requested_state") or {}).get("name", "")
        answers = {}
        for key, q in questions.items():
            if q["type"] == "noul":
                default = 0.05 if key == "needs_user" else 0.9
                answers[key] = {"type": "noul", "noul": float(policy.get(key, default))}
        if "next_state" in questions:
            answers["next_state"] = {
                "type": "choice",
                "choice": policy.get("next_state", requested),
                "confidence": float(policy.get("confidence", 0.8))}
        return answers


def build_jev(workspace: str, policies: list[dict] | None = None, user_answers=None):
    flow = FlowSpec.load(FLOW_PATH)
    llm = scripted_llm()
    jev_client = FakeJevClient(policies)
    user_answers = list(user_answers or [])

    def ask(q):
        return user_answers.pop(0) if user_answers else ""

    harness = Harness(flow=flow, judge=JevJudge(jev_client, flow),
                      worker_factory=lambda: Worker(llm, ToolBox(workspace)),
                      ask_user=ask,
                      emit=lambda kind, text: None)
    return harness, jev_client


# ------------------------------------------------------------------ 测试

def test_flow_spec():
    flow = FlowSpec.load(FLOW_PATH)
    assert flow.initial == "CLARIFY"
    assert flow.state("DELIVER").terminal
    assert flow.is_legal("VERIFY", "IMPLEMENT")       # 回边
    assert not flow.is_legal("CLARIFY", "DELIVER")    # 不存在边
    assert flow.is_legal("PLAN", "PLAN")              # 自环恒合法
    rendered = flow.render_for_judge()
    assert "CLARIFY → PLAN" in rendered and "最终交付标准" in rendered


def test_judge_parse():
    raw = '前言\n```json\n{"decision": "approve", "next_state": "PLAN", "reason": "ok", "final_answer": null}\n```\n后记'
    decision = Judge._parse(raw)
    assert decision.decision == "approve" and decision.next_state == "PLAN"
    try:
        Judge._parse("没有 JSON")
        raise AssertionError("应当抛 ValueError")
    except ValueError:
        pass


def test_e2e_mock():
    with tempfile.TemporaryDirectory() as ws:
        harness, llm = build(ws)
        result = harness.run("写一个斐波那契命令行工具并验证")
        assert result.delivered, result.reason
        assert result.visited == ["CLARIFY", "PLAN", "IMPLEMENT", "VERIFY", "DELIVER"]
        assert "fib.py" in (result.final_answer or "")
        for f in ("requirements.md", "plan.md", "fib.py"):
            assert (Path(ws) / f).exists(), f"缺少产物 {f}"
        # 黑板应记录 4 个非终态的沉淀
        assert len(harness.board.entries) == 4
        # 裁决者从未拿到工具
        assert all(True for _ in llm.calls)


def test_reject_path():
    with tempfile.TemporaryDirectory() as ws:
        harness, llm = build(ws, reject_once=True)
        result = harness.run("写一个斐波那契命令行工具并验证")
        assert result.delivered, result.reason
        assert llm.counters["rejected"] is True     # 确实走过驳回
        assert result.cycles == 5                   # 4 次转移 + 1 次驳回重提


# ------------------------------------------------------------------ Jev 裁决者

def test_jev_e2e():
    with tempfile.TemporaryDirectory() as ws:
        harness, jev = build_jev(ws)
        result = harness.run("写一个斐波那契命令行工具并验证")
        assert result.delivered, result.reason
        assert result.visited == ["CLARIFY", "PLAN", "IMPLEMENT", "VERIFY", "DELIVER"]
        # 1 次开场（goal_clear）+ 4 次转移裁决，全部批量提问
        assert len(jev.calls) == 5
        assert "goal_clear" in jev.calls[0]["questions"]
        assert {"exit_0", "entry_0", "next_state", "needs_user"} <= set(jev.calls[1]["questions"])
        # 终态裁决附带交付标准核验
        assert "criterion_0" in jev.calls[4]["questions"]
        assert "状态轨迹" in result.final_answer and "Jev" in result.final_answer
        # 每条 reason 都是概率合成，可审计
        assert all("p=" in d.reason or "Jev" in d.reason
                   for d in harness.judge.decisions)


def test_jev_structural_guard():
    """Jev 即便输出白名单外的选项，也会被结构性挡下（CLARIFY→DELIVER 无边）。"""
    flow = FlowSpec.load(FLOW_PATH)
    judge = JevJudge(FakeJevClient([{"next_state": "DELIVER", "confidence": 0.99}]), flow)
    judge.on_user_input("原始目标：测试目标", flow.initial)
    d = judge.on_transition_request(TransitionRequest("CLARIFY", "DELIVER", "做完了", "证据"))
    assert d.decision == "reject" and d.next_state == ""


def test_jev_needs_user():
    """needs_user 概率超阈值 → 裁决者先问用户，拿到回答后再完成裁决。"""
    with tempfile.TemporaryDirectory() as ws:
        harness, jev = build_jev(ws, policies=[{}, {"needs_user": 0.95}],
                                 user_answers=["目标是 fib 命令行工具，验收：输出前 10 项"])
        result = harness.run("写一个工具并验证")
        assert result.delivered, result.reason
        asked = [d for d in harness.judge.decisions if d.user_question]
        assert asked, "应当产生一次 user_question"
        # 用户回答进入了后续 Jev 请求的 state
        assert any("fib 命令行工具" in str(c["state"].get("user_answers"))
                   for c in jev.calls)
        # 第二次裁决同一请求时不再重复提问
        assert sum(1 for d in harness.judge.decisions if d.user_question) == 1


def test_jev_delivery_gate():
    """交付标准概率不达标 → 拒绝进入终态；补足后放行。"""
    with tempfile.TemporaryDirectory() as ws:
        harness, jev = build_jev(ws, policies=[{}, {}, {}, {}, {"criterion_0": 0.2}])
        result = harness.run("写一个斐波那契命令行工具并验证")
        assert result.delivered, result.reason
        assert any("交付标准未全部通过" in d.reason for d in harness.judge.decisions)
        # 驳回指示里带着未达标项与概率
        assert any("p=0.20" in (d.instructions or "") for d in harness.judge.decisions)
        # VERIFY 被驳回后执行者带反馈重提 → Jev 多裁决一次
        assert len(jev.calls) == 6 and result.cycles == 5


def test_jev_confidence_floor():
    """choice 置信度低于地板 → 保守驳回，要求补证据。"""
    flow = FlowSpec.load(FLOW_PATH)
    judge = JevJudge(FakeJevClient([{}, {"confidence": 0.3}]), flow)
    judge.on_user_input("原始目标：测试目标", flow.initial)
    d = judge.on_transition_request(TransitionRequest("CLARIFY", "PLAN", "需求已写", "requirements.md"))
    assert d.decision == "reject" and "置信度不足" in d.reason


# ------------------------------------------------------------------ P0：黑板结构化 / Watchdog / 注入防护

def test_blackboard_artifacts_inline_spill():
    """黑板 Inline 摘要+产物路径+证据开头；产物本体 Spill 在磁盘。"""
    from sluice.harness import Blackboard
    board = Blackboard()
    board.record("CLARIFY", "写好需求", "requirements.md 覆盖三点需求" + "x" * 300,
                 artifacts=["requirements.md", "requirements.md"])
    text = board.render()
    assert "产物：requirements.md" in text
    assert "可用 read_file 按需读取" in text          # 召回指引
    assert "requirements.md 覆盖三点需求" in text      # 证据开头（Inline）
    assert "x" * 201 not in text                       # 证据截断（Spill）
    assert board.entries[0].artifacts == ["requirements.md"]  # 去重
    # 端到端：产物路径来自 toolbox.written 真实追踪
    with tempfile.TemporaryDirectory() as ws:
        harness, _ = build(ws)
        harness.run("写一个斐波那契命令行工具并验证")
        kinds = {e.state: e.artifacts for e in harness.board.entries}
        assert "requirements.md" in kinds["CLARIFY"]
        assert "fib.py" in kinds["IMPLEMENT"]
        assert "fib.py" not in kinds["CLARIFY"]        # 各状态只记自己的驻留产物


def test_watchdog_reject_escalation_and_abort():
    """同状态连续驳回 → 注入升级反馈；持续驳回 → Watchdog 终止。"""
    from sluice.watchdog import Watchdog
    wd = Watchdog(reject_warn=2, reject_abort=4)
    req = TransitionRequest("CLARIFY", "PLAN", "做完了", "证据")
    d_rej = JudgeDecision(decision="reject", next_state="", reason="不够",
                          instructions="继续")
    d_ok = JudgeDecision(decision="approve", next_state="PLAN", reason="ok")

    a1 = wd.observe("CLARIFY", d_rej, req)             # 第1次驳回
    assert a1.feedback is None and a1.abort is None
    a2 = wd.observe("CLARIFY", d_rej, req)             # 第2次驳回 → 升级反馈
    assert a2.feedback and "换一种做法" in a2.feedback
    a3 = wd.observe("CLARIFY", d_rej, req)             # 第3次驳回
    assert a3.abort is None
    a4 = wd.observe("CLARIFY", d_rej, req)             # 第4次驳回 → 终止
    assert a4.abort and "驳回" in a4.abort
    a5 = wd.observe("CLARIFY", d_ok, req)              # 正常批准不触发
    assert a5.abort is None
    # 换状态后驳回计数重置
    wd2 = Watchdog(reject_warn=2, reject_abort=3)
    wd2.observe("PLAN", d_ok, req)
    assert wd2.observe("PLAN", d_rej, req).abort is None


def test_watchdog_stall_and_revisit():
    """停滞连发与往复打转都会终止。"""
    from sluice.watchdog import Watchdog
    wd = Watchdog(stall_abort=2, revisit_abort=3)
    stall = TransitionRequest("CLARIFY", "CLARIFY", "[自动驻留请求] 执行者停滞", "")
    d = JudgeDecision(decision="approve", next_state="CLARIFY", reason="驻留")
    assert wd.observe("CLARIFY", d, stall).abort is None
    assert wd.observe("CLARIFY", d, stall).abort       # 连续2轮停滞
    # 打转：CLARIFY/PLAN 来回切换
    ok = JudgeDecision(decision="approve", next_state="PLAN", reason="ok")
    ok_back = JudgeDecision(decision="approve", next_state="CLARIFY", reason="back")
    wd2 = Watchdog(revisit_abort=3)
    wd2.observe("CLARIFY", ok, stall)   # CLARIFY 第1次
    wd2.observe("PLAN", ok_back, stall) # PLAN 第1次
    wd2.observe("CLARIFY", ok, stall)   # CLARIFY 第2次
    wd2.observe("PLAN", ok_back, stall) # PLAN 第2次
    a = wd2.observe("CLARIFY", ok, stall)  # CLARIFY 第3次 → 打转
    assert a.abort and "往复打转" in a.abort


def test_watchdog_e2e_abort_path():
    """端到端：裁决者永远驳回 → Watchdog 先注入升级反馈，最终终止运行。"""
    flow = FlowSpec.load(FLOW_PATH)

    def handler(messages, tools):
        system = messages[0]["content"]
        if "你是「裁决者」" in system:
            return {"role": "assistant", "content": json.dumps({
                "decision": "reject", "next_state": "", "reason": "不充分",
                "instructions": "补充证据", "user_question": None,
                "final_answer": None}, ensure_ascii=False)}
        state = re.search(r"当前状态：(\w+)", system).group(1)
        _, _, to = _SCRIPT[state]
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{len(messages)}", "name": "request_transition",
                                "arguments": {"to_state": to, "summary": "done",
                                              "evidence": "文件"}}]}

    with tempfile.TemporaryDirectory() as ws:
        llm = MockLLM(handler)
        harness = Harness(flow=flow, judge=Judge(llm, flow),
                          worker_factory=lambda: Worker(llm, ToolBox(ws)),
                          ask_user=lambda q: "",
                          emit=lambda k, t: None)
        result = harness.run("目标")
        assert not result.delivered
        assert result.reason.startswith("Watchdog:")
        assert "驳回" in result.reason


def test_injection_guard_prompts():
    """LLM 裁决者与执行者的系统提示词都含数据/指令分离约束。"""
    import sluice.judge as judge_mod
    import sluice.worker as worker_mod
    assert "数据" in judge_mod._JUDGE_RULES and "指令" in judge_mod._JUDGE_RULES
    assert "数据" in worker_mod._WORKER_RULES and "指令" in worker_mod._WORKER_RULES


def test_stall_request_carries_evidence():
    """停滞兜底请求必须携带真实工作痕迹，不能让裁决者对空证据失明。"""
    import tempfile as _tf
    from sluice.worker import Worker
    with _tf.TemporaryDirectory() as ws:
        flow = FlowSpec.load(FLOW_PATH)
        llm = scripted_llm()
        worker = Worker(llm, ToolBox(ws))
        worker.begin(flow.state("VERIFY"), "goal", "instr", "", 2)
        # 先让工具箱有真实产物与输出记录
        worker.toolbox.call("write_file", {"path": "run.log", "content": "PASS 9/9"})
        worker.messages.append({"role": "tool", "tool_call_id": "x",
                                "content": "[exit=0]\nALL PASS 9/9"})
        req = worker._stall_request(reason="测试停滞")
        assert "run.log" in req.evidence            # 产物清单
        assert "ALL PASS 9/9" in req.evidence       # 最近工具输出
        assert req.to_state == ""                   # 去处交给裁决者
        # 无任何痕迹时也要有明确说明
        empty_worker = Worker(scripted_llm(), ToolBox(ws + "2"))
        empty_worker.begin(flow.state("VERIFY"), "g", "i", "", 2)
        req2 = empty_worker._stall_request()
        assert "无可用证据" in req2.evidence


def test_stall_request_advanced_forward():
    """停滞兜底请求会被改写成「向前一步」的具体提案（消除死循环）。"""
    from sluice.harness import Harness
    flow = FlowSpec.load(FLOW_PATH)
    harness = Harness(flow=flow, judge=None, worker_factory=lambda: None,
                      ask_user=lambda q: "", emit=lambda k, t: None)
    req = TransitionRequest("VERIFY", "", "[自动驻留请求] 超过单状态最大步数", "证据")
    out = harness._advance_stalled_request(req, "VERIFY")
    assert out.to_state == "DELIVER"                 # VERIFY 的唯一前进方向
    assert "[停滞自动前进]" in out.summary
    # 已有明确目标 / 非停滞请求不改动
    ok = TransitionRequest("VERIFY", "DELIVER", "正常请求", "证据")
    assert harness._advance_stalled_request(ok, "VERIFY").to_state == "DELIVER"
    other = TransitionRequest("PLAN", "", "普通驻留", "e")
    assert harness._advance_stalled_request(other, "PLAN").to_state == ""


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
