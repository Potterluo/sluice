"""mini-swe-agent 的 SWE-bench 适配器（公平口径：统一取 git diff）。

跑法：宿主用程序化 API 起 mini-swe-agent，environment=docker，
镜像与挂载点与其他 harness 完全一致（同一份工作区文件）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "bench" / "adapters" / "msa_runner.py"


def run(instance: dict, workspace: Path, container: str, cfg: dict,
        timeout: int = 3600) -> dict:
    from bench.adapters.common import git_diff, reset_workspace

    reset_workspace(workspace)
    task = (
        "仓库在 /testbed。请修复下面的 GitHub issue，只改源码，不要修改测试文件。\n\n"
        f"{instance['problem_statement']}"
    )
    log_path = Path("bench/results/logs") / f"msa__{instance['instance_id']}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    import os
    env = os.environ.copy()
    env["MSWEA_COST_TRACKING"] = "ignore_errors"   # 网关自建模型名 litellm 不认识价格
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    with log_path.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(
                [sys.executable, str(RUNNER),
                 "--task", task,
                 "--image", instance["image"],
                 "--workspace", str(workspace),
                 "--model", cfg["model"],
                 "--base-url", cfg["base_url"],
                 "--api-key", cfg["api_key"],
                 "--step-limit", str(cfg.get("max_steps", 30))],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, env=env,
            )
            log.write(proc.stdout or "")
            log.write("\n[stderr]\n")
            log.write((proc.stderr or "")[-8000:])
        except subprocess.TimeoutExpired:
            log.write(f"[超时 >{timeout}s]\n")
    wall = time.time() - t0

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    # runner 最后一行输出 {"exit_status":..., "messages": N}
    steps = 0
    for line in reversed(log_text.splitlines()):
        if line.startswith('{"exit_status"'):
            try:
                steps = json.loads(line).get("messages", 0)
            except json.JSONDecodeError:
                pass
            break
        if line.startswith("{"):
            try:
                steps = json.loads(line).get("messages", 0)
                break
            except json.JSONDecodeError:
                continue

    patch = git_diff(workspace)
    return {
        "harness": "mini-swe-agent",
        "instance_id": instance["instance_id"],
        "exit_code": 0 if steps else 1,
        "wall_seconds": round(wall, 1),
        "tool_calls": steps,          # 消息数近似步数（每步一个 assistant+result）
        "judge_decisions": 0,
        "patch": patch,
        "log": str(log_path),
    }
