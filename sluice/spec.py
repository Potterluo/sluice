"""状态机定义（FlowSpec）：由 JSON 声明，渲染进裁决者系统提示词并被 Harness 强制执行。

裁决者是转移的「决策者」，FlowSpec 是转移的「合法性边界」：
裁决者批准一条不存在的边时，Harness 会拒绝并要求其重新裁决。
自环（请求留在当前状态）恒合法。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StateSpec:
    name: str
    description: str = ""
    worker_goal: str = ""           # 该状态下执行者的任务目标
    entry_conditions: list[str] = field(default_factory=list)
    exit_conditions: list[str] = field(default_factory=list)
    deliverable: str = ""           # 离开该状态时应沉淀到黑板的产物说明
    terminal: bool = False


@dataclass
class FlowSpec:
    name: str
    initial: str
    states: dict[str, StateSpec]
    edges: set[tuple[str, str]]
    delivery_criteria: list[str] = field(default_factory=list)  # 终态交付标准
    max_steps_per_state: int = 25   # 执行者单次驻留状态的最大工具步数
    max_cycles: int = 40            # 全局最大裁决轮数（防死循环）

    # ------------------------------------------------------------ 校验

    def state(self, name: str) -> StateSpec:
        if name not in self.states:
            raise KeyError(f"未知状态: {name}")
        return self.states[name]

    def is_legal(self, frm: str, to: str) -> bool:
        if frm == to:
            return True  # 自环恒合法（执行者可请求继续驻留）
        return (frm, to) in self.edges

    def legal_next(self, frm: str) -> list[str]:
        nxt = sorted(to for f, to in self.edges if f == frm)
        return nxt + [f"{frm}（自环：继续驻留）"]

    # ------------------------------------------------------------ 加载

    @classmethod
    def from_dict(cls, data: dict) -> "FlowSpec":
        states = {}
        for name, s in data["states"].items():
            states[name] = StateSpec(
                name=name,
                description=s.get("description", ""),
                worker_goal=s.get("worker_goal", ""),
                entry_conditions=list(s.get("entry_conditions", [])),
                exit_conditions=list(s.get("exit_conditions", [])),
                deliverable=s.get("deliverable", ""),
                terminal=bool(s.get("terminal", False)),
            )
        edges = {(e[0], e[1]) for e in data.get("edges", [])}
        spec = cls(
            name=data.get("name", "flow"),
            initial=data["initial"],
            states=states,
            edges=edges,
            delivery_criteria=list(data.get("delivery_criteria", [])),
            max_steps_per_state=int(data.get("max_steps_per_state", 25)),
            max_cycles=int(data.get("max_cycles", 40)),
        )
        spec._validate()
        return spec

    @classmethod
    def load(cls, path: str | Path) -> "FlowSpec":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def _validate(self) -> None:
        if self.initial not in self.states:
            raise ValueError(f"初始状态 {self.initial} 未定义")
        for f, t in self.edges:
            self.state(f)
            self.state(t)
        if not any(s.terminal for s in self.states.values()):
            raise ValueError("状态机至少需要一个 terminal 状态")

    # ------------------------------------------------------------ 渲染给裁决者

    def render_for_judge(self) -> str:
        lines = [f"# 状态机：{self.name}", "", f"初始状态：{self.initial}", "", "## 状态集合"]
        for s in self.states.values():
            tag = "【终态】" if s.terminal else ""
            lines.append(f"\n### {s.name} {tag}")
            if s.description:
                lines.append(f"- 含义：{s.description}")
            if s.worker_goal:
                lines.append(f"- 执行者目标：{s.worker_goal}")
            for label, items in (("进入条件", s.entry_conditions),
                                 ("退出条件", s.exit_conditions)):
                if items:
                    lines.append(f"- {label}：" + "；".join(items))
            if s.deliverable:
                lines.append(f"- 状态产物：{s.deliverable}")
        lines += ["", "## 合法转移边（自环恒合法，不在此列出）"]
        for f, t in sorted(self.edges):
            lines.append(f"- {f} → {t}")
        if self.delivery_criteria:
            lines += ["", "## 最终交付标准（进入终态前必须全部满足）"]
            lines += [f"- {c}" for c in self.delivery_criteria]
        return "\n".join(lines)
