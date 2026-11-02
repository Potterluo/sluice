"""pi（earendil-works/pi）的 SWE-bench 适配器。

设置：评测镜像里没有 Node，因此挂载宿主准备好的 Linux Node + pi 包 + agent 配置，
在容器内 /testbed 直接运行 `pi --mode json`，bash 工具天然落在正确的 conda 环境里。

前置（一次性）：
- D:/bench-tools/node-v24.9.0-linux-x64（Node ≥22.8，pi 需要 enableCompileCache）
- bench/pi_home/agent/models.json（指向本项目的 OpenAI 兼容网关）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PI_PKG = next((ROOT / "bench" / "vendor" / "pi" / "node_modules" / "@earendil-works" / "pi-coding-agent").glob("dist")).parent
PI_HOME = ROOT / "bench" / "pi_home"


def _node_dir() -> str:
    candidates = sorted(Path("D:/bench-tools").glob("node-v24.*-linux-x64"))
    if not candidates:
        raise RuntimeError("缺少 Linux Node（D:/bench-tools/node-v24.*-linux-x64）")
    return str(candidates[-1]).replace("\\", "/")


def run(instance: dict, workspace: Path, container: str, cfg: dict,
        timeout: int = 3600) -> dict:
    from bench.adapters.common import git_diff, reset_workspace

    reset_workspace(workspace)
    log_path = Path("bench/results/logs") / f"pi__{instance['instance_id']}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ws = str(workspace).replace("\\", "/")
    task = ("仓库就在当前目录（/testbed）。请修复下面的 GitHub issue，只改源码，"
            "不要修改测试文件。\n\n" + instance["problem_statement"])

    cmd = [
        "docker", "run", "--rm", "--entrypoint", "bash",
        "-v", f"{_node_dir()}:/opt/node:ro",
        "-v", f"{str(PI_PKG).replace(chr(92), '/')}:/opt/pi:ro",
        "-v", f"{str(PI_HOME).replace(chr(92), '/')}:/root/.pi",
        "-v", f"{ws}:/testbed",
        "-w", "/testbed",
        "-e", "PI_CODING_AGENT_DIR=/root/.pi/agent",
        "-e", f"PI_GATEWAY_KEY={cfg['api_key']}",
        instance["image"],
        "/root/.pi/run_pi.sh", "--mode", "json",
        "--model", f"gw/{cfg['model']}",
        task,
    ]

    t0 = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            log.write(proc.stdout or "")
            log.write("\n[stderr]\n")
            log.write((proc.stderr or "")[-8000:])
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            log.write(f"[超时 >{timeout}s]\n")
            exit_code = 124
    wall = time.time() - t0

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    # 步数：pi 的 JSON 事件流里 tool_execution_start / turn_start 各一次
    steps = log_text.count('"type":"tool_execution_start"') or log_text.count('"turn_start"')
    error = None
    if exit_code != 0:
        m = re.search(r'"type"\s*:\s*"error"[^\n]*', log_text)
        error = (m.group(0)[:300] if m else "exit != 0")

    patch = git_diff(workspace)
    return {
        "harness": "pi",
        "instance_id": instance["instance_id"],
        "exit_code": exit_code,
        "wall_seconds": round(wall, 1),
        "tool_calls": steps,
        "judge_decisions": 0,
        "patch": patch,
        "log": str(log_path),
        "error": error,
    }
