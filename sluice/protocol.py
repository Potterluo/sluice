"""控制面协议：执行者 → 裁决者的转移请求，裁决者 → Harness 的裁决决定。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TransitionRequest:
    """执行者发起的唯一控制面动作。summary/evidence 是裁决者唯一可见的执行信息。"""

    from_state: str
    to_state: str
    summary: str = ""    # 本状态做了什么（会沉淀到黑板）
    evidence: str = ""   # 支撑退出条件已满足的证据

    def render(self) -> str:
        return (f"转移请求：{self.from_state} → {self.to_state}\n"
                f"工作总结：{self.summary or '(无)'}\n"
                f"证据：{self.evidence or '(无)'}")


@dataclass
class JudgeDecision:
    """裁决者的结构化输出。

    decision:
      - approve ：同意执行者请求的转移（next_state 即目标状态）
      - reject  ：驳回，执行者留在原状态按 instructions 继续
      - redirect：裁决者主动改派到另一合法状态
    """

    decision: str                 # approve | reject | redirect
    next_state: str
    reason: str = ""
    instructions: str = ""        # 给执行者的下一步指示
    user_question: str | None = None   # 非空表示裁决者要先问用户
    final_answer: str | None = None    # 进入终态时的最终交付

    @property
    def moves(self) -> bool:
        return self.decision in ("approve", "redirect")

    def feedback_for_worker(self) -> str:
        parts = [f"裁决结果：{self.decision}", f"理由：{self.reason or '(无)'}"]
        if self.instructions:
            parts.append(f"裁决者指示：{self.instructions}")
        return "\n".join(parts)
