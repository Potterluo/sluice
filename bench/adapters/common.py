"""评测公共层：从 SWE-bench 镜像取出工作区、起容器、代理 shell、收集 patch。

设计要点：
- 工作区双向可见：把镜像里的 /testbed 复制到宿主目录，再以 bind mount 挂回
  容器 `/testbed`。宿主的 agent 文件工具与容器的测试命令操作同一份文件。
- 模型调用留在宿主（走宿主网络），容器只负责跑代码/测试（正确 conda 环境）。
- 所有 harness 共用同一套容器与同一份工作区，保证对比公平。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

CONTAINER_CWD = "/testbed"


def run(cmd: list[str], timeout: int = 600, check: bool = False,
        capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, timeout=timeout, check=check, capture_output=capture,
                          text=True, encoding="utf-8", errors="replace")


def docker(*args: str, timeout: int = 600, check: bool = False) -> subprocess.CompletedProcess:
    return run(["docker", *args], timeout=timeout, check=check)


def extract_workspace(image: str, dest: Path, timeout: int = 900) -> Path:
    """把镜像里的 /testbed 复制到宿主 dest，并打一个 baseline commit。

    baseline commit 的作用：镜像本身带未提交改动（docker cp 丢执行位、
    build/ 目录等），先提交成基线，之后 `git diff HEAD` 就只含 agent 的改动。
    """
    if (dest / ".git").exists() and (dest.parent / f".{dest.name}.baseline").exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    name = f"x-{int(time.time() * 1000) % 10**9}"
    docker("create", "--name", name, image, timeout=300, check=True)
    try:
        cp = docker("cp", f"{name}:{CONTAINER_CWD}/.", str(dest), timeout=timeout)
        if cp.returncode != 0:
            raise RuntimeError(f"docker cp 失败: {cp.stderr[:500]}")
    finally:
        docker("rm", "-f", name, timeout=120)

    git = ["git", "-C", str(dest)]
    run([*git, "config", "core.fileMode", "false"], timeout=60)
    run([*git, "config", "core.autocrlf", "false"], timeout=60)
    run([*git, "config", "user.email", "bench@local"], timeout=60)
    run([*git, "config", "user.name", "bench"], timeout=60)
    run([*git, "add", "-A"], timeout=300)
    run([*git, "commit", "-m", "bench baseline", "--no-verify", "--allow-empty"], timeout=300)
    # 标记放在工作区外，避免自己出现在 diff 里
    (dest.parent / f".{dest.name}.baseline").write_text("1", encoding="utf-8")
    return dest


def start_container(image: str, name: str, workspace: Path, cwd: str = CONTAINER_CWD) -> str:
    """起常驻容器，把宿主工作区挂到 /testbed。"""
    docker("rm", "-f", name, timeout=60)
    host_path = str(workspace).replace("\\", "/")
    cp = docker("run", "-d", "--name", name,
                "-v", f"{host_path}:{cwd}",
                "-w", cwd,
                "--entrypoint", "sleep",
                image, "infinity", timeout=300)
    if cp.returncode != 0:
        raise RuntimeError(f"容器启动失败: {cp.stderr[:500]}")
    return name


def stop_container(name: str) -> None:
    docker("rm", "-f", name, timeout=120)


def exec_in(name: str, command: str, timeout: int = 600, cwd: str = CONTAINER_CWD,
            env: dict[str, str] | None = None) -> tuple[int, str]:
    cmd = ["docker", "exec", "-w", cwd]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [name, "bash", "-lc", command]
    try:
        p = run(cmd, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"[超时 >{timeout}s]"


def reset_workspace(workspace: Path) -> None:
    """把工作区恢复到 baseline commit（每套 harness 必须从同一干净起点开始）。"""
    if not (workspace / ".git").exists():
        return
    run(["git", "-C", str(workspace), "reset", "--hard", "HEAD"], timeout=300)
    run(["git", "-C", str(workspace), "clean", "-fdx"], timeout=300)


def git_diff(workspace: Path) -> str:
    """收集 agent 相对 baseline commit 的改动（含新增文件），并剔除执行位噪音。"""
    run(["git", "-C", str(workspace), "add", "-A", "-N"], timeout=300)
    p = run(["git", "-C", str(workspace), "diff", "HEAD", "--binary",
             "--no-color", "--no-ext-diff"], timeout=300)
    return p.stdout or ""


def harness_meta(harness: str, instance: dict, workspace: Path, patch: str) -> dict:
    return {
        "harness": harness,
        "instance_id": instance["instance_id"],
        "workspace": str(workspace),
        "patch_bytes": len(patch.encode("utf-8")),
        "patch": patch,
    }


def cleanup_workspace(workspace: Path) -> None:
    shutil.rmtree(workspace, ignore_errors=True)


def write_predictions(records: list[dict], path: Path, model_name: str) -> Path:
    """生成 swebench 评测所需的 predictions 文件。"""
    seen: dict[tuple[str, str], dict] = {}
    for r in records:
        key = (r["harness"], r["instance_id"])
        seen[key] = {
            "instance_id": r["instance_id"],
            "model_name_or_path": f"{model_name}--{r['harness']}",
            "model_patch": r["patch"],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for v in seen.values():
            f.write(json.dumps(v, ensure_ascii=False) + "\n")
    return path
