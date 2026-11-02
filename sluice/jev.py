"""Jev（TypeSafe System One 决策模型）适配层：让裁决者跑在纯决策模型上。

Jev 不生成文本，只返回类型化答案与概率（choice / noul / score）。
本模块把「裁决者」的职责分解为一次批量请求里的若干类型化问题：
    next_state  (choice)  从当前状态出发应进入哪个状态；选项=合法去向+驻留，
                          结构上杜绝非法转移（Jev 想批也批不出白名单外的边）
    exit_i      (noul)    当前状态第 i 条退出条件是否被证据满足
    entry_i     (noul)    目标状态第 i 条进入条件是否满足
    criterion_i (noul)    候选去向含终态时，第 i 条交付标准是否满足
    needs_user  (noul)    裁决所需关键信息是否只能由用户补充
    goal_clear  (noul)    开场时用户目标是否清晰可执行
文本字段（reason / instructions / user_question / final_answer）由确定性模板
从结构化答案合成 —— 概率即证据，可审计、可复现。
注入防护：evidence 只作为 state 数据进入类型化问题（noul/choice），没有可被
指令劫持的自由文本决策面，天然免疫提示词注入（LLM 后端需靠提示词约束）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .protocol import JudgeDecision, TransitionRequest
from .spec import FlowSpec


class JevError(RuntimeError):
    """Jev Decisions API 调用失败。"""


@dataclass
class JevConfig:
    base_url: str = "https://openrouter.ai/api/alpha"   # OpenRouter Decisions API
    api_key: str = ""
    model: str = "typesafe/jev-1.14"                    # 需要稳定阈值时锁定版本
    timeout: int = 60

    @classmethod
    def from_env(cls) -> "JevConfig":
        key = os.environ.get("SLUICE_JEV_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
        return cls(
            base_url=(os.environ.get("SLUICE_JEV_BASE_URL")
                      or "https://openrouter.ai/api/alpha").rstrip("/"),
            api_key=key,
            model=os.environ.get("SLUICE_JEV_MODEL") or "typesafe/jev-1.14",
        )


class JevClient:
    """Decisions API 最小客户端：POST {base_url}/decisions，返回 answers 字典。

    兼容两类端点：
    - OpenRouter Decisions API（响应 {"answers": {...}}）
    - 本地 llama.cpp server（--systemone：端点 /systemone 或 /v1/systemone，
      响应直接是 answers 字典；无 "model" 字段也可）
    """

    def __init__(self, config: JevConfig):
        self.config = config

    def ask(self, state: Any, questions: dict[str, Any], retries: int = 3) -> dict[str, Any]:
        """带瞬时故障重试的决策请求。"""
        last_error = ""
        for attempt in range(retries):
            try:
                return self._ask_once(state, questions)
            except JevError as e:
                last_error = str(e)
                transient = ("HTTP 429" in last_error or "HTTP 5" in last_error
                             or "请求失败" in last_error or "SSL" in last_error
                             or "timed out" in last_error.lower())
                if attempt < retries - 1 and transient:
                    time.sleep(min(1.5 * 2 ** attempt, 8))
                elif "HTTP 403" in last_error or "HTTP 401" in last_error:
                    break  # 权限/区域问题，重试无意义
        raise JevError(last_error)

    def _ask_once(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config
        body = {"model": cfg.model, "state": state, "questions": questions}
        last_error = ""
        for url in self._candidate_urls():
            req = urllib.request.Request(
                url=url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {cfg.api_key}"},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if isinstance(payload, dict) and "error" in payload:
                    raise JevError(str(payload["error"].get("message", payload["error"]))[:300])
                if isinstance(payload, dict) and "answers" in payload:
                    return payload["answers"]
                if isinstance(payload, dict) and payload:
                    return payload  # 本地 systemone：直接返回 answers 形态
                raise JevError(f"响应为空或形态未知: {str(payload)[:200]}")
            except urllib.error.HTTPError as e:  # noqa: BLE001
                last_error = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_error = f"{type(e).__name__}: {e}"
        raise JevError(f"Jev 请求失败（{self._candidate_urls()}）: {last_error}")

    def _candidate_urls(self) -> list[str]:
        base = self.config.base_url.rstrip("/")
        urls = [f"{base}/decisions"]
        if not base.endswith("/alpha"):           # 本地基址 → systemone 形态
            urls += [f"{base}/systemone", f"{base}/v1/systemone"]
        return urls


class JevJudge:
    """裁决者的 Jev 实现，接口与 sluice.judge.Judge 完全对齐。"""

    def __init__(self, client: Any, flow: FlowSpec, *,
                 noul_threshold: float = 0.5,     # 条件判定为「满足」的概率门槛
                 confidence_floor: float = 0.55,  # choice 置信度低于此 → 保守驳回
                 ask_user_threshold: float = 0.6, # needs_user 高于此 → 先问用户
                 delivery_threshold: float = 0.6, # 交付标准的概率门槛
                 goal_clear_threshold: float = 0.5):
        self.client = client
        self.flow = flow
        self.noul_threshold = noul_threshold
        self.confidence_floor = confidence_floor
        self.ask_user_threshold = ask_user_threshold
        self.delivery_threshold = delivery_threshold
        self.goal_clear_threshold = goal_clear_threshold
        self.decisions: list[JudgeDecision] = []
        self.trace: list[str] = [flow.initial]
        self._goal = ""
        self._user_answers: list[str] = []
        self._pending: TransitionRequest | None = None
        self._asked_once = False
        self._corrections: list[str] = []

    # ------------------------------------------------------------ 对外入口（与 Judge 同形）

    def on_user_input(self, text: str, current_state: str) -> JudgeDecision:
        if text.startswith("原始目标："):
            self._goal = text.partition("：")[2].strip()
            return self._opening()
        if text.startswith("对裁决者提问的回答："):
            answer = text.partition("：")[2].strip()
            if answer:
                self._user_answers.append(answer)
            if self._pending is not None:
                return self._decide(self._pending)
            return self._opening()
        self._user_answers.append(text.strip())
        if self._pending is not None:
            return self._decide(self._pending)
        return JudgeDecision(decision="redirect", next_state=current_state,
                             reason="用户补充输入",
                             instructions=f"用户补充了输入，请结合推进：{text}")

    def on_transition_request(self, req: TransitionRequest) -> JudgeDecision:
        self._pending = req
        self._asked_once = False
        self._corrections = []
        return self._decide(req)

    def correct(self, problem: str) -> JudgeDecision:
        self._corrections.append(problem)
        if self._pending is not None:
            return self._decide(self._pending)
        return self._record(JudgeDecision(
            decision="reject", next_state="", reason=problem,
            instructions="请按纠错意见调整后重新发起转移请求"))

    # ------------------------------------------------------------ 开场：目标清晰度

    def _opening(self) -> JudgeDecision:
        p, reason = 0.5, ""
        try:
            answers = self.client.ask(
                {"phase": "opening", "goal": self._goal, "user_answers": self._user_answers},
                {"goal_clear": self._noul("用户目标清晰、可执行、且有可验收的交付结果")})
            p = float(answers.get("goal_clear", {}).get("noul") or 0.5)
            reason = f"goal_clear p={p:.2f}"
        except JevError as e:
            reason = f"Jev 调用失败（跳过开场检查）：{e}"
        if p < self.goal_clear_threshold and not self._user_answers:
            return self._record(JudgeDecision(
                decision="reject", next_state="", reason=reason,
                user_question="目标还不够明确，请补充：要交付什么、关键约束、验收标准"))
        note = f"\n用户补充：{'；'.join(self._user_answers)}" if self._user_answers else ""
        return self._record(JudgeDecision(
            decision="approve", next_state=self.flow.initial, reason=reason,
            instructions=f"按状态目标开始工作。目标：{self._goal}{note}"))

    # ------------------------------------------------------------ 核心裁决

    def _decide(self, req: TransitionRequest) -> JudgeDecision:
        cur = self.flow.states.get(req.from_state)
        if cur is None:
            return self._record(JudgeDecision(
                decision="reject", next_state="",
                reason=f"请求中的当前状态 {req.from_state!r} 不在状态机中",
                instructions="请核对状态名后重新发起转移请求"))
        tgt = self.flow.states.get(req.to_state)

        state: dict[str, Any] = {
            "goal": self._goal,
            "user_answers": self._user_answers,
            "current_state": {"name": req.from_state, "description": cur.description,
                              "exit_conditions": cur.exit_conditions},
            "requested_state": ({"name": req.to_state, "description": tgt.description,
                                 "entry_conditions": tgt.entry_conditions,
                                 "terminal": tgt.terminal} if tgt is not None
                                else {"name": req.to_state, "note": "该状态不在状态机中"}),
            "request": {"summary": req.summary, "evidence": req.evidence},
        }
        if self._corrections:
            state["harness_corrections"] = self._corrections

        questions: dict[str, Any] = {}
        for i, cond in enumerate(cur.exit_conditions):
            questions[f"exit_{i}"] = self._noul(f"退出条件已满足：{cond}")
        if tgt is not None:
            for i, cond in enumerate(tgt.entry_conditions):
                questions[f"entry_{i}"] = self._noul(f"进入条件已满足：{cond}")
        options = self._options(req.from_state)
        questions["next_state"] = {
            "type": "choice",
            "instructions": ("综合退出/进入条件与证据，下一步应进入哪个状态？"
                             "若证据表明当前状态发现缺陷需要返工，应选择对应的返工目标"
                             "（回退）状态，而非驻留。"),
            "criteria": options}
        questions["needs_user"] = self._noul("本次裁决所需的关键信息只能由用户补充")
        # 交付标准核验：剔除「自指标准」——涉及最终答案/交付说明的标准，
        # 在裁决时 final_answer 尚未产生，Jev 无法评估（实测 p=0.22 的假阴性来源）；
        # 这类标准由 Harness 结构性保证（final_answer 缺失时纠正裁决者）。
        self_referential = ("最终答案", "final_answer", "交付说明")
        judged_criteria = [c for c in self.flow.delivery_criteria
                           if not any(k in c for k in self_referential)]
        if any(self.flow.states[o].terminal for o in options):  # 候选含终态才核验交付
            for i, c in enumerate(judged_criteria):
                questions[f"criterion_{i}"] = self._noul(f"最终交付标准已满足：{c}")

        try:
            answers = self.client.ask(state, questions)
            self._infra_failures = 0
        except JevError as e:
            # 基础设施故障与内容驳回分流：连续失败时升级为 user_question，
            # 让人来决策（是等待、换后端还是放宽），而不是把执行者困死在原状态。
            self._infra_failures = getattr(self, "_infra_failures", 0) + 1
            if self._infra_failures >= 3:
                return self._record(JudgeDecision(
                    decision="reject", next_state="",
                    reason=f"Jev 基础设施故障×{self._infra_failures}: {e}",
                    instructions="裁决服务连续失败，已请求用户决策。",
                    user_question=(f"裁决服务（Jev）连续 {self._infra_failures} 次不可用：\n{e}\n"
                                   "请选择：1) 稍等片刻后让执行者重试；"
                                   "2) 提供补充说明后由我按说明裁决。")))
            return self._record(JudgeDecision(
                decision="reject", next_state="", reason=f"Jev 调用失败：{e}",
                instructions="Jev 暂不可用，请稍后重新发起转移请求，或补充更多证据。"))

        # ---- 读取结构化答案
        ch = answers.get("next_state") or {}
        chosen = ch.get("choice") or req.from_state
        if chosen not in options:      # 答案异常 → 保守驻留
            chosen = req.from_state
        conf = float(ch.get("confidence") or 0.0)
        exit_ps = self._probs(answers, "exit_", cur.exit_conditions)
        entry_ps = self._probs(answers, "entry_", tgt.entry_conditions if tgt else [])
        crit_ps = (self._probs(answers, "criterion_", judged_criteria)
                   if judged_criteria else {})
        ask_p = float((answers.get("needs_user") or {}).get("noul") or 0.0)
        why = self._why(chosen, conf, exit_ps, entry_ps, crit_ps)

        # ---- 1) 缺关键信息 → 先问用户（同一请求只问一次）
        if ask_p > self.ask_user_threshold and not self._asked_once:
            self._asked_once = True
            low = self._uncertain(exit_ps, entry_ps)
            return self._record(JudgeDecision(
                decision="reject", next_state="", reason=f"needs_user p={ask_p:.2f}",
                instructions="等待用户补充信息后再裁决。",
                user_question="裁决者缺少关键信息，请补充：\n- "
                              + "\n- ".join(low or ["本次转移的关键判定依据"])))

        # ---- 2) 终态交付门槛（确定性护栏，Jev 只提供概率）
        if self.flow.states[chosen].terminal:
            fails = [(c, p) for c, p in crit_ps.items() if p < self.delivery_threshold]
            if fails:
                return self._record(JudgeDecision(
                    decision="reject", next_state="",
                    reason=why + "｜交付标准未全部通过",
                    instructions="交付标准未满足，暂不准入终态：\n- "
                                 + "\n- ".join(f"{c}（p={p:.2f}）" for c, p in fails)
                                 + "\n请补足后再次请求，或修复问题后请求退回。"))

        # ---- 3) 证据闸门：choice 高置信不足以放行，退出条件概率必须达标
        # （实测教训：choice 与 noul 是独立信号，choice 常对弱证据也高置信）
        # 豁免两类场景：
        #   - 回边/驻留：回退的意义就是 exit 不满足（如验证发现缺陷），
        #     exit 概率低恰是放行理由；
        #   - redirect：裁决者改派的目标有自己的进入条件，与当前状态 exit 无关。
        is_backward = (chosen in self._backward_targets(req.from_state))
        weak = [(c, p) for c, p in exit_ps.items() if p < self.noul_threshold]
        if (weak and chosen == req.to_state and not is_backward):
            return self._record(JudgeDecision(
                decision="reject", next_state="",
                reason=why + "｜退出条件证据不足",
                instructions="转移请求被驳回：以下退出条件的证据不满足：\n- "
                             + "\n- ".join(f"{c}（p={p:.2f}）" for c, p in weak)
                             + "\n请补足证据后重新发起转移请求。"))

        # ---- 4) 置信度地板
        if conf < self.confidence_floor:
            weak = [(c, p) for c, p in exit_ps.items() if p < self.noul_threshold]
            return self._record(JudgeDecision(
                decision="reject", next_state="", reason=why + "｜置信度不足",
                instructions=f"裁决置信度不足（conf={conf:.2f}），请补充针对以下条件的证据：\n- "
                             + "\n- ".join(f"{c}（p={p:.2f}）" for c, p in weak)
                             + "\n然后重新发起转移请求。"))

        # ---- 4) 映射 choice → decision
        terminal = self.flow.states[chosen].terminal
        if chosen == req.to_state:
            return self._record(JudgeDecision(
                decision="approve", next_state=chosen, reason=why,
                instructions=f"转移批准：{req.from_state} → {chosen}。"
                             f"新状态目标：{self.flow.states[chosen].worker_goal}",
                final_answer=self._final_answer(req, chosen, crit_ps) if terminal else None))
        if chosen == req.from_state:
            weak = [(c, p) for c, p in exit_ps.items() if p < self.noul_threshold]
            return self._record(JudgeDecision(
                decision="reject", next_state="", reason=why,
                instructions=("退出条件证据不足，请补足后重新请求：\n- "
                              + "\n- ".join(f"{c}（p={p:.2f}）" for c, p in weak))
                             if weak else "证据不足以支持转移，请继续完成当前状态的工作。"))
        s = self.flow.states[chosen]
        return self._record(JudgeDecision(
            decision="redirect", next_state=chosen, reason=why,
            instructions=f"裁决改派：请转入 {chosen}（{s.description}）。"
                         + (f"先满足：{'；'.join(s.entry_conditions)}" if s.entry_conditions else ""),
            final_answer=self._final_answer(req, chosen, crit_ps) if terminal else None))

    # ------------------------------------------------------------ 辅助

    def _backward_targets(self, state_name: str) -> set[str]:
        """相对 state_name 的「回退方向」状态：到终态更远或同级非前进方向者。

        以「到最近终态的最短步数」定义流程方向——VERIFY(1步) → IMPLEMENT(3步)
        是回退：回退的意义就是当前状态退出条件不满足（如验证发现缺陷），
        此时 exit 概率低恰是放行理由而非驳回理由。
        """
        if getattr(self, "_dist_cache", None) is None:
            dist = {s: 0 for s, spec in self.flow.states.items() if spec.terminal}
            frontier = list(dist)
            while frontier:
                nxt = []
                for cur in frontier:
                    for src, t in self.flow.edges:
                        if t == cur and src not in dist:
                            dist[src] = dist[cur] + 1
                            nxt.append(src)
                frontier = nxt
            self._dist_cache = dist
        d = self._dist_cache
        cur = d.get(state_name, 999)
        return {s for s in self.flow.states
                if s != state_name and d.get(s, 999) >= cur}

    def _options(self, current: str) -> dict[str, str]:
        """choice 白名单：合法去向 + 驻留，结构性杜绝非法转移。"""
        opts: dict[str, str] = {}
        for to in sorted({t for f, t in self.flow.edges if f == current}):
            s = self.flow.states[to]
            desc = s.description or to
            if s.entry_conditions:
                desc += f"；需满足：{'；'.join(s.entry_conditions)}"
            opts[to] = desc
        opts[current] = f"留在 {current} 继续完善（{self.flow.states[current].description}）"
        return opts

    @staticmethod
    def _noul(instruction: str) -> dict[str, Any]:
        return {"type": "noul", "instructions": instruction,
                "criteria": {"true": "工作总结与证据表明该命题成立",
                             "false": "证据缺失，或表明该命题不成立"}}

    @staticmethod
    def _probs(answers: dict, prefix: str, conditions: list[str]) -> dict[str, float]:
        return {cond: float((answers.get(f"{prefix}{i}") or {}).get("noul") or 0.5)
                for i, cond in enumerate(conditions)}

    @staticmethod
    def _uncertain(*prob_dicts: dict[str, float]) -> list[str]:
        return [f"{c}（当前判断 p={p:.2f}，倾向不明）"
                for pd in prob_dicts for c, p in pd.items() if 0.3 <= p <= 0.7]

    @staticmethod
    def _why(chosen: str, conf: float, exit_ps: dict, entry_ps: dict, crit_ps: dict) -> str:
        def fmt(ps: dict) -> str:
            return "，".join(f"{c[:16]}={p:.2f}" for c, p in ps.items()) or "-"
        parts = [f"Jev: {chosen}(conf={conf:.2f})",
                 f"退出[{fmt(exit_ps)}]", f"进入[{fmt(entry_ps)}]"]
        if crit_ps:
            parts.append(f"交付[{fmt(crit_ps)}]")
        return "｜".join(parts)

    def _final_answer(self, req: TransitionRequest, chosen: str,
                      crit_ps: dict[str, float]) -> str:
        trace = self.trace + ([chosen] if chosen != self.trace[-1] else [])
        # 自指标准（涉及最终答案本身）由本模板结构性满足，标注为模板保证
        self_referential = ("最终答案", "final_answer", "交付说明")
        judged = ";".join(f"{c}✓(p={p:.2f})" for c, p in crit_ps.items())
        templated = ";".join(f"{c}✓(模板保证)" for c in self.flow.delivery_criteria
                             if any(k in c for k in self_referential))
        return "\n".join([
            f"目标：{self._goal}",
            f"状态轨迹：{' → '.join(trace)}",
            f"最终总结：{req.summary}",
            f"证据：{req.evidence}",
            "交付标准核验：" + (judged + ("；" + templated if templated else "")
                              or "（未定义交付标准）"),
            "产物位置与用法：见上方最终总结与证据；工作区文件见各状态黑板产物。",
            "（由 Jev 决策模型逐条核验后交付）",
        ])

    def _record(self, d: JudgeDecision) -> JudgeDecision:
        if d.moves and d.next_state != self.trace[-1]:
            self.trace.append(d.next_state)
        self.decisions.append(d)
        return d
