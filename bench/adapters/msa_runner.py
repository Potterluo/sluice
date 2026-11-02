"""mini-swe-agent 的程序化运行器（避开需要 TTY 的 CLI/TUI）。

由 bench/adapters/msa_adapter.py 以子进程方式调用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from minisweagent import package_dir
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.models.litellm_model import LitellmModel


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--step-limit", type=int, default=30)
    ap.add_argument("--config", default="swebench.yaml",
                    help="使用 minisweagent/config/benchmarks/ 下的配置（默认 swebench.yaml，tool-call 风格）")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    cfg_path = Path(package_dir) / "config" / "benchmarks" / args.config
    agent_cfg = yaml.safe_load(cfg_path.read_text())["agent"]
    agent_cfg["step_limit"] = args.step_limit
    agent_cfg.pop("cost_limit", None)  # 自建模型无价格表，关掉成本上限

    model = LitellmModel(
        model_name=f"openai/{args.model}",
        model_kwargs={"api_base": args.base_url, "api_key": args.api_key,
                      "temperature": 0.0, "drop_params": True},
    )
    ws = str(Path(args.workspace)).replace("\\", "/")
    env = DockerEnvironment(
        image=args.image,
        cwd="/testbed",
        timeout=180,
        run_args=["--rm", "-v", f"{ws}:/testbed"],
        env={"PAGER": "cat", "LESS": "-R", "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1",
             "BASH_ENV": "/root/.bashrc"},
    )
    agent = DefaultAgent(model, env, **agent_cfg)
    exit_status = "unknown"
    try:
        agent.run(args.task)
        exit_status = "finished"
    except Exception as e:  # noqa: BLE001
        exit_status = f"error: {type(e).__name__}: {e}"
    finally:
        try:
            env.cleanup()
        except Exception:  # noqa: BLE001
            pass

    n_messages = len(getattr(agent, "messages", []))
    usage = {"calls": getattr(agent, "n_calls", 0), "cost": getattr(agent, "cost", 0.0),
             "prompt": 0, "completion": 0}
    # litellm 在消息里回填 usage（若模型价格未知，token 仍在 response 中）
    for m in getattr(agent, "messages", []):
        u = (m.get("extra") or {}).get("response", {}).get("usage") if isinstance(m.get("extra"), dict) else None
        if isinstance(u, dict):
            usage["prompt"] += int(u.get("prompt_tokens") or 0)
            usage["completion"] += int(u.get("completion_tokens") or 0)
    print(json.dumps({"exit_status": exit_status, "messages": n_messages, "usage": usage},
                     ensure_ascii=False))
    if args.output:
        try:
            Path(args.output).write_text(json.dumps(getattr(agent, "messages", []), ensure_ascii=False),
                                         encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
