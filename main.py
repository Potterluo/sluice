"""Sluice CLI 入口。

用法：
    python main.py --flow flows/coding_task.json --goal "写一个斐波那契命令行工具并测试"
    python main.py --mock            # 无 API Key：剧本 LLM 裁决者 + 剧本执行者
    python main.py --mock-jev        # 无 API Key：Jev 裁决者（Fake）+ 剧本执行者
    python main.py --judge jev       # 真实模式：Jev 做裁决者，聊天 LLM 做执行者

环境变量：
    执行者（聊天模型，OpenAI 兼容）：
        SLUICE_BASE_URL / SLUICE_API_KEY / SLUICE_MODEL
        SLUICE_WORKER_MODEL（可选，单独覆盖）
    裁决者-LLM 模式（默认）：SLUICE_JUDGE_MODEL（可选）
    裁决者-Jev 模式：SLUICE_JEV_API_KEY（或 OPENROUTER_API_KEY）
        SLUICE_JEV_MODEL（默认 typesafe/jev-1.14）/ SLUICE_JEV_BASE_URL
    SLUICE_JSON_MODE=1（LLM 裁决者端点支持 json_object 时建议开启）
    NO_COLOR=1 禁用彩色输出
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sluice import ui
from sluice.harness import Harness
from sluice.jev import JevClient, JevConfig, JevJudge
from sluice.judge import Judge
from sluice.llm import LLMClient, LLMConfig
from sluice.spec import FlowSpec
from sluice.tools import ToolBox
from sluice.ui import UiRenderer
from sluice.worker import Worker

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def build_harness(flow: FlowSpec, workspace: str, judge, llm_worker,
                  allow_shell: bool = True, renderer: UiRenderer | None = None) -> Harness:
    emit = renderer.render if renderer else ui.UiRenderer().render

    def make_worker() -> Worker:
        return Worker(llm_worker, ToolBox(workspace, allow_shell=allow_shell),
                      on_event=emit)  # 执行者的工具调用/状态事件也走同一渲染器

    def ask(question: str) -> str:
        y = ui.c
        print(y("\n┌ 裁决者向你提问 " + "─" * 30, ui.YELLOW))
        for line in question.split("\n"):
            print(y(f"│ {line}", ui.YELLOW))
        print(y("└" + "─" * 46, ui.YELLOW))
        try:
            return input("> ").strip()
        except EOFError:
            return ""

    return Harness(flow=flow, judge=judge,
                   worker_factory=make_worker,
                   ask_user=ask,
                   emit=emit)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sluice —— 状态机 + Loop 的 Agent Harness")
    ap.add_argument("--flow", default="flows/coding_task.json", help="状态机定义 JSON")
    ap.add_argument("--goal", default=None, help="用户原始目标；缺省则交互输入")
    ap.add_argument("--workspace", default="workspace", help="执行者的工作区目录")
    ap.add_argument("--no-shell", action="store_true", help="禁用执行者的 shell 工具")
    ap.add_argument("--judge", choices=["llm", "jev"], default="llm",
                    help="裁决者后端：llm（聊天模型，默认）或 jev（System One 决策模型）")
    ap.add_argument("--mock", action="store_true", help="剧本 LLM 裁决者 + 剧本执行者（无需 API）")
    ap.add_argument("--mock-jev", action="store_true", help="Fake Jev 裁决者 + 剧本执行者（无需 API）")
    args = ap.parse_args()

    flow = FlowSpec.load(args.flow)
    Path(args.workspace).mkdir(parents=True, exist_ok=True)
    allow_shell = not args.no_shell
    renderer = UiRenderer(flow.name, list(flow.states),
                          descriptions={k: v.description for k, v in flow.states.items()})
    mock = args.mock or args.mock_jev

    # ---- 选择后端
    if mock:
        from tests.test_smoke import FakeJevClient, scripted_llm
        worker_llm = scripted_llm(verbose=False)
        if args.mock_jev:
            judge = JevJudge(FakeJevClient(), flow)
            judge_desc = "FakeJev（离线剧本）"
        else:
            judge = Judge(worker_llm, flow)
            judge_desc = "ScriptedLLM（离线剧本）"
        worker_desc = "ScriptedLLM（离线剧本）"
        harness = build_harness(flow, args.workspace, judge, worker_llm,
                                allow_shell, renderer)
        goal = args.goal or "写一个斐波那契命令行工具并验证（Mock 演示）"
    else:
        worker_cfg = LLMConfig.from_env("SLUICE_WORKER")
        if not worker_cfg.api_key:
            print("错误：执行者需要 SLUICE_API_KEY（以及可选的 SLUICE_BASE_URL / SLUICE_MODEL）"
                  "，或使用 --mock / --mock-jev 运行演示。")
            return 2
        worker_desc = worker_cfg.model
        if args.judge == "jev":
            jev_cfg = JevConfig.from_env()
            if not jev_cfg.api_key:
                print("错误：Jev 裁决者需要 SLUICE_JEV_API_KEY 或 OPENROUTER_API_KEY。")
                return 2
            judge = JevJudge(JevClient(jev_cfg), flow)
            judge_desc = f"Jev（{jev_cfg.model}）"
        else:
            judge_cfg = LLMConfig.from_env("SLUICE_JUDGE")
            if not judge_cfg.api_key:
                print("错误：LLM 裁决者需要 SLUICE_API_KEY，或改用 --judge jev。")
                return 2
            judge = Judge(LLMClient(judge_cfg), flow)
            judge_desc = judge_cfg.model
        harness = build_harness(flow, args.workspace, judge, LLMClient(worker_cfg),
                                allow_shell, renderer)
        goal = args.goal or input("请输入目标：").strip()
        if not goal:
            print("错误：目标不能为空")
            return 2

    ui.banner(flow.name, list(flow.states), args.workspace, judge_desc, worker_desc, goal)
    result = harness.run(goal)
    ui.footer(result, result.visited or renderer.visited)
    # token 用量（评测用成本核算）：worker + judge 两侧累加
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    for side in (getattr(harness.worker, "llm", None), getattr(harness.judge, "llm", None)):
        u = getattr(side, "usage", None)
        if isinstance(u, dict):
            for k in usage:
                usage[k] += int(u.get(k) or 0)
    print(f"[usage] prompt={usage['prompt']} completion={usage['completion']} calls={usage['calls']}")
    return 0 if result.delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
