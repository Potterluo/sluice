"""mini-agent-harness（用户仓库）的 SWE-bench 适配器。

跑法：宿主起 cli.py，工作区 = 镜像 /testbed 的宿主副本，
shell 工具被 HARNESS_SHELL_DOCKER 代理进容器（见 vendored tools.py 的适配注释）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = next((ROOT / "bench" / "vendor").glob("Potterluo-mini-agent-harness-*"))


def run(instance: dict, workspace: Path, container: str, cfg: dict,
        timeout: int = 3600, max_iterations: int = 30) -> dict:
    env = os.environ.copy()
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "HARNESS_BASE_URL": cfg["base_url"],
        "HARNESS_API_KEY": cfg["api_key"],
        "HARNESS_MODEL": cfg["model"],
        "HARNESS_SHELL_DOCKER": container,
    })
    # cli.py 依赖 harness 包在 sys.path（脚本所在目录即 SOURCE_DIR）
    goal = (
        "修复下面这个 GitHub issue。仓库就在当前目录。\n\n"
        f"{instance['problem_statement']}\n\n"
        "只改源码，不要改测试文件来伪造通过；改完跑测试验证。"
    )

    from bench.adapters.common import git_diff, reset_workspace
    reset_workspace(workspace)

    t0 = time.time()
    log_path = Path("bench/results/logs") / f"mah__{instance['instance_id']}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(
                [sys.executable, str(SOURCE_DIR / "cli.py"),
                 "--cwd", str(workspace), "--approval", "auto",
                 "--max-iterations", str(max_iterations), goal],
                env=env, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL,
            )
            log.write(proc.stdout or "")
            log.write("\n[stderr]\n")
            log.write(proc.stderr or "")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as e:
            log.write(f"[超时 >{timeout}s]\n")
            exit_code = 124
    wall = time.time() - t0

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    tool_calls = log_text.count("[tool]")
    review_calls = log_text.count("[review]")

    patch = git_diff(workspace)

    return {
        "harness": "mini-agent-harness",
        "instance_id": instance["instance_id"],
        "exit_code": exit_code,
        "wall_seconds": round(wall, 1),
        "tool_calls": tool_calls,
        "judge_decisions": review_calls,
        "patch": patch,
        "log": str(log_path),
    }
