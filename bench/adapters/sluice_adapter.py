"""Sluice（本项目）的 SWE-bench 适配器。

跑法：宿主起 harness，工作区 = 镜像 /testbed 的宿主副本，shell 工具代理进容器。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "main.py"
FLOW = ROOT / "flows" / "swe_bench.json"


def run(instance: dict, workspace: Path, container: str, cfg: dict,
        timeout: int = 3600) -> dict:
    env = os.environ.copy()
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "SLUICE_BASE_URL": cfg["base_url"],
        "SLUICE_API_KEY": cfg["api_key"],
        "SLUICE_MODEL": cfg["model"],
        "SLUICE_JUDGE_MODEL": cfg.get("judge_model") or cfg["model"],
        "SLUICE_SHELL_DOCKER": container,      # shell 进容器跑，文件工具操作共享工作区
        "NO_COLOR": "1",
    })

    from bench.adapters.common import git_diff, reset_workspace
    reset_workspace(workspace)

    goal = (
        "请修复下面这个 GitHub issue。仓库就在当前工作区（/testbed）。\n\n"
        f"{instance['problem_statement']}\n\n"
        "要求：只改源码，不要修改测试文件来伪造通过；改完用仓库自带测试验证。"
    )

    t0 = time.time()
    log_path = Path("bench/results/logs") / f"sluice__{instance['instance_id']}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(
                [sys.executable, str(MAIN), "--flow", str(FLOW),
                 "--goal", goal, "--workspace", str(workspace)],
                env=env, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,  # 无交互：裁决者提问走 EOFError 默认分支
            )
            log.write(proc.stdout or "")
            log.write("\n[stderr]\n")
            log.write(proc.stderr or "")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as e:
            log.write(f"[超时 >{timeout}s]\n")
            log.write(str(e.stdout or "")[:5000])
            exit_code = 124
    wall = time.time() - t0

    # 从日志里提取工具调用数
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    tool_calls = log_text.count("[ * tool]") + log_text.count("⚙")
    judge_calls = log_text.count("[== judge]") + log_text.count("✔ approve") + log_text.count("✘ reject")

    from bench.adapters.common import git_diff
    patch = git_diff(workspace)

    return {
        "harness": "sluice",
        "instance_id": instance["instance_id"],
        "exit_code": exit_code,
        "wall_seconds": round(wall, 1),
        "tool_calls": tool_calls,
        "judge_decisions": judge_calls,
        "patch": patch,
        "log": str(log_path),
    }
