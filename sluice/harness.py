"""Harness 编排器：串起用户、裁决者、执行者与状态机。

主循环：
  执行者在当前状态跑 ReAct Loop
    → 发起 request_transition
    → Harness 转呈裁决者（处理用户提问、协议纠错、合法性校验）
    → Watchdog 观测本周期，异常模式时注入干预或终止
    → approve / redirect：沉淀黑板、切换状态、拉起新执行者
    → reject：原执行者带着裁决反馈继续原 Loop
    → 进入终态且裁决者给出 final_answer：交付，结束
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .judge import Judge
from .protocol import JudgeDecision
from .spec import FlowSpec
from .watchdog import Watchdog
from .worker import Worker

Emitter = Callable[[str, str], None]


def _default_emit(kind: str, text: str) -> None:
    icons = {"state": ">>", "tool": " *", "request": "->", "judge": "==",
             "user": "??", "deliver": "**", "warn": "!!"}
    print(f"[{icons.get(kind, '  ')} {kind}] {text}", flush=True)


@dataclass
class BoardEntry:
    """一条黑板记录（Inline & Spill）：只内联摘要与引用，完整内容留在磁盘。"""

    state: str
    summary: str
    artifacts: list[str] = field(default_factory=list)  # 产物路径（下一跳引用）
    evidence_head: str = ""                             # 证据开头（截断）


@dataclass
class Blackboard:
    """跨状态共享的轻量上下文（链式召回 lite）。

    每条记录 Inline 摘要 + 产物路径 + 证据开头；产物本体 Spill 在工作区磁盘上，
    执行者用 read_file 沿路径按需召回 —— 引用即下一跳。
    """

    entries: list[BoardEntry] = field(default_factory=list)

    def record(self, state: str, summary: str, evidence: str,
               artifacts: list[str] | tuple[str, ...] = ()) -> None:
        head = " ".join((evidence or "").split())
        self.entries.append(BoardEntry(
            state=state,
            summary=(summary or "").strip() or "（无总结）",
            artifacts=list(dict.fromkeys(artifacts)),
            evidence_head=head[:200]))

    def render(self) -> str:
        blocks = []
        for e in self.entries:
            lines = [f"[{e.state}] {e.summary}"]
            if e.artifacts:
                lines.append("产物：" + "、".join(e.artifacts)
                             + "（完整内容已留在工作区，可用 read_file 按需读取）")
            lines.append(f"证据摘要：{e.evidence_head or '（无）'}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


@dataclass
class RunResult:
    delivered: bool
    final_answer: str | None
    visited: list[str]
    cycles: int
    reason: str = ""


class Harness:
    def __init__(self, flow: FlowSpec, judge: Judge,
                 worker_factory: Callable[[], Worker],
                 ask_user: Callable[[str], str] | None = None,
                 emit: Emitter = _default_emit,
                 watchdog: Watchdog | None = None):
        self.flow = flow
        self.judge = judge
        self.worker_factory = worker_factory
        self.ask_user = ask_user or (lambda q: "")
        self.emit = emit
        self.board = Blackboard()
        self.watchdog = watchdog or Watchdog()
        self.worker: Worker | None = None
        self.visited: list[str] = []

    # ------------------------------------------------------------ 主入口

    def run(self, goal: str) -> RunResult:
        state_name = self.flow.initial
        self.emit("state", f"状态机启动：{state_name}")
        # 原始目标先呈交裁决者，由它给出给首个执行者的指示（允许开场追问用户）
        opening = self.judge.on_user_input(f"原始目标：{goal}", state_name)
        for _ in range(3):
            if not opening.user_question:
                break
            self.emit("user", opening.user_question)
            answer = self.ask_user(opening.user_question)
            opening = self.judge.on_user_input(
                f"对裁决者提问的回答：{answer or '（用户未作答）'}", state_name)
        instructions = opening.instructions or "请按当前状态目标开始工作。"
        self._spawn(state_name, goal, instructions)

        feedback: str | None = None
        for cycle in range(1, self.flow.max_cycles + 1):
            assert self.worker is not None
            req = self.worker.step_until_transition(feedback=feedback)
            feedback = None
            decision = self._consult(req, state_name)

            # Watchdog 外控回路：观测异常模式，注入干预或终止
            action = self.watchdog.observe(state_name, decision, req, self.worker)
            if action.abort:
                self.emit("warn", f"Watchdog 终止：{action.abort}")
                return RunResult(False, None, self.visited, cycle,
                                 reason=f"Watchdog: {action.abort}")

            if decision.moves:
                if decision.next_state == state_name:
                    # 自环：保留原执行者上下文，仅注入新指示
                    feedback = decision.instructions or "裁决者要求继续驻留当前状态。"
                else:
                    self.board.record(state_name, req.summary, req.evidence,
                                      artifacts=self.worker.toolbox.written)
                    state_name = decision.next_state
                    if self.flow.state(state_name).terminal:
                        self.emit("deliver", "进入终态，交付完成")
                        return RunResult(True, decision.final_answer,
                                         self.visited + [state_name], cycle)
                    self._spawn(state_name, goal, decision.instructions)
                if action.feedback and feedback is not None:
                    feedback += f"\n【Watchdog】{action.feedback}"
                elif action.feedback:
                    self.emit("warn", f"Watchdog 提示（随状态切换丢弃）：{action.feedback}")
            else:
                feedback = decision.feedback_for_worker()
                if action.feedback:
                    feedback += f"\n【Watchdog】{action.feedback}"

        return RunResult(False, None, self.visited, self.flow.max_cycles,
                         reason=f"达到最大裁决轮数 {self.flow.max_cycles}")

    # ------------------------------------------------------------ 内部

    def _spawn(self, state_name: str, goal: str, instructions: str) -> None:
        spec = self.flow.state(state_name)
        self.worker = self.worker_factory()
        self.worker.begin(spec, goal, instructions,
                          self.board.render(), self.flow.max_steps_per_state)
        self.visited.append(state_name)

    def _consult(self, req, state_name: str) -> JudgeDecision:
        """呈交裁决者，并处理用户提问 / 合法性校验 / 交付校验的有限回合。"""
        self.emit("request", f"{req.from_state} → {req.to_state}\n{req.summary}")
        decision = self.judge.on_transition_request(req)

        for _ in range(3):
            if decision.user_question:
                self.emit("user", decision.user_question)
                answer = self.ask_user(decision.user_question)
                decision = self.judge.on_user_input(
                    f"对裁决者提问的回答：{answer or '（用户未作答）'}", state_name)
                continue
            problem = self._validate(decision, state_name)
            if not problem:
                break
            self.emit("warn", f"协议纠错：{problem}")
            decision = self.judge.correct(problem)
        self.emit("judge", f"{decision.decision} → {decision.next_state}｜{decision.reason}")
        return decision

    def _validate(self, decision: JudgeDecision, state_name: str) -> str | None:
        """返回 None 表示合法，否则返回给裁决者的纠错说明。"""
        if not decision.moves:
            return None
        target = decision.next_state
        if target not in self.flow.states:
            return f"状态 {target!r} 不存在。合法去向：{'、'.join(self.flow.legal_next(state_name))}"
        if not self.flow.is_legal(state_name, target):
            return f"边 {state_name} → {target} 不存在。合法去向：{'、'.join(self.flow.legal_next(state_name))}"
        if self.flow.state(target).terminal and not decision.final_answer:
            return "批准进入终态时必须给出 final_answer（最终交付物）。"
        return None
