"""Watchdog：主循环外部的轻量控制回路（纯代码，无 LLM）。

对应 Super-agent 第 6 节的思想：长期运行的 Agent 系统不仅要监控基础设施存活
（这里即 harness 主循环是否还在跑），还要监控「任务是否有实质进展」。
Watchdog 消费主循环的观测点（裁决结果 / 转移请求 / 执行者消息），在异常
模式出现时介入：

- 同状态连续驳回  → 给执行者注入升级反馈；持续恶化 → 终止运行
- 执行者停滞循环（自动驻留请求连发）→ 终止
- 同一状态反复进入（往复打转）→ 终止
- 工具调用大量失败 → 注入环境检查反馈

它是确定性代码（Fast Path）：不调用模型、自身不会成为新的失控点。
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import JudgeDecision, TransitionRequest

_STALL_MARK = "[自动驻留请求]"


@dataclass
class WatchdogAction:
    feedback: str | None = None  # 注入给执行者的干预信息
    abort: str | None = None     # 非空则终止整个运行


class Watchdog:
    def __init__(self, *,
                 reject_warn: int = 3,    # 同状态连续驳回达到该值 → 注入升级反馈
                 reject_abort: int = 6,   # 持续驳回达到该值 → 终止
                 stall_abort: int = 3,    # 连续停滞请求达到该值 → 终止
                 revisit_abort: int = 5,  # 同一状态进入次数达到该值 → 终止
                 tool_fail_ratio: float = 0.6,  # 工具失败率阈值
                 tool_fail_min: int = 4):       # 触发检查的最少工具结果数
        self.reject_warn = reject_warn
        self.reject_abort = reject_abort
        self.stall_abort = stall_abort
        self.revisit_abort = revisit_abort
        self.tool_fail_ratio = tool_fail_ratio
        self.tool_fail_min = tool_fail_min
        self._prev_state: str | None = None
        self._occupancy: dict[str, int] = {}
        self._reject_streak = 0
        self._stall_streak = 0
        self._tool_warned = False

    # ------------------------------------------------------------ 观测点

    def observe(self, state: str, decision: JudgeDecision,
                req: TransitionRequest | None, worker=None) -> WatchdogAction:
        """每个裁决周期调用一次。state 为本轮执行者驻留的状态。"""
        feedback: str | None = None
        abort: str | None = None

        if state != self._prev_state:
            self._prev_state = state
            self._occupancy[state] = self._occupancy.get(state, 0) + 1
            self._reject_streak = 0
            self._stall_streak = 0
            self._tool_warned = False

        # ---- 往复打转
        if self._occupancy[state] >= self.revisit_abort:
            abort = (f"状态 {state} 已第 {self._occupancy[state]} 次进入，"
                     f"疑似往复打转（上限 {self.revisit_abort}）")

        # ---- 驳回连击
        if decision.decision == "reject":
            self._reject_streak += 1
            if self._reject_streak >= self.reject_abort:
                abort = abort or (f"状态 {state} 连续 {self._reject_streak} 次被驳回"
                                  f"（上限 {self.reject_abort}）")
            elif self._reject_streak >= self.reject_warn:
                feedback = (f"已连续 {self._reject_streak} 次被裁决者驳回。"
                            "请显著换一种做法，或明确说明当前阻塞点与所需支持，"
                            "不要重复同样的请求。")
        else:
            self._reject_streak = 0

        # ---- 停滞连击（执行者被合成自环请求说明它已无法自主推进）
        if req is not None and req.summary.startswith(_STALL_MARK):
            self._stall_streak += 1
            if self._stall_streak >= self.stall_abort:
                abort = abort or (f"执行者连续 {self._stall_streak} 轮停滞"
                                  f"（上限 {self.stall_abort}）")
        else:
            self._stall_streak = 0

        # ---- 工具失败率（每状态驻留只提醒一次）
        if worker is not None and not self._tool_warned and abort is None:
            fails = self._tool_failures(worker)
            if fails is not None:
                self._tool_warned = True
                note = (f"近期 {fails[0]} 次工具调用中 {fails[1]} 次失败。"
                        "请先检查工作区环境（路径/依赖/命令可用性），再继续推进。")
                feedback = f"{feedback}\n{note}" if feedback else note

        return WatchdogAction(feedback=feedback, abort=abort)

    # ------------------------------------------------------------ 内部

    def _tool_failures(self, worker) -> tuple[int, int] | None:
        """返回 (工具结果数, 失败数)；样本不足时返回 None。"""
        results = [m.get("content") or "" for m in worker.messages
                   if m.get("role") == "tool"][-self.tool_fail_min * 2:]
        if len(results) < self.tool_fail_min:
            return None
        fails = sum(1 for r in results if r.startswith("[错误]"))
        if fails / len(results) >= self.tool_fail_ratio:
            return len(results), fails
        return None
