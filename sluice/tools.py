"""执行者工具箱：文件读写 / 列目录 / 跑 shell，全部限制在工作区根目录内。

request_transition 不是这里的工具——它是控制面通道，由 Worker 单独拦截。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

_MAX_READ = 20_000   # read_file 返回字符上限
_MAX_OUT = 8_000     # run_shell 输出字符上限


class ToolBox:
    def __init__(self, root: str | Path, shell_timeout: int = 60, allow_shell: bool = True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.shell_timeout = shell_timeout
        self.allow_shell = allow_shell
        self.written: list[str] = []  # 本次驻留写入过的文件（黑板产物追踪）

    # ------------------------------------------------------------ 工具定义

    def specs(self) -> list[dict[str, Any]]:
        def fn(name, desc, props, required):
            return {"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required}}}

        tools = [
            fn("list_dir", "列出目录内容（相对工作区路径）",
               {"path": {"type": "string", "description": "相对路径，默认 '.'"}},
               []),
            fn("read_file", "读取文件内容（相对工作区路径）",
               {"path": {"type": "string"}}, ["path"]),
            fn("write_file", "写入（覆盖）文件内容（相对工作区路径）",
               {"path": {"type": "string"}, "content": {"type": "string"}},
               ["path", "content"]),
            fn("append_file", "追加内容到文件末尾",
               {"path": {"type": "string"}, "content": {"type": "string"}},
               ["path", "content"]),
        ]
        if self.allow_shell:
            tools.append(fn(
                "run_shell", f"在工作区内执行 shell 命令（{self.shell_timeout}s 超时）",
                {"command": {"type": "string"}}, ["command"]))
        return tools

    # ------------------------------------------------------------ 调用分发

    def call(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "list_dir":
                return self._list_dir(args.get("path", "."))
            if name == "read_file":
                return self._read_file(args.get("path", ""))
            if name == "write_file":
                return self._write_file(args.get("path", ""), args.get("content", ""))
            if name == "append_file":
                return self._append_file(args.get("path", ""), args.get("content", ""))
            if name == "run_shell":
                return self._run_shell(args.get("command", ""))
            return f"[错误] 未知工具: {name}"
        except Exception as e:  # noqa: BLE001 —— 工具失败应反馈给执行者而不是炸掉 Loop
            return f"[错误] {type(e).__name__}: {e}"

    # ------------------------------------------------------------ 实现

    def _resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root != p and self.root not in p.parents:
            raise PermissionError(f"路径越出工作区: {rel}")
        return p

    def _list_dir(self, rel: str) -> str:
        p = self._resolve(rel)
        if not p.is_dir():
            return f"[错误] 不是目录: {rel}"
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        if not entries:
            return "(空目录)"
        return "\n".join(("d " if e.is_dir() else "f ") + e.name for e in entries)

    def _read_file(self, rel: str) -> str:
        p = self._resolve(rel)
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > _MAX_READ:
            text = text[:_MAX_READ] + f"\n...[截断，共 {len(text)} 字符]"
        return text

    def _write_file(self, rel: str, content: str) -> str:
        p = self._resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self._track_written(p)
        return f"[成功] 已写入 {p.relative_to(self.root)}（{len(content)} 字符）"

    def _append_file(self, rel: str, content: str) -> str:
        p = self._resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        self._track_written(p)
        return f"[成功] 已追加到 {p.relative_to(self.root)}（+{len(content)} 字符）"

    def _track_written(self, p: Path) -> None:
        rel = p.relative_to(self.root).as_posix()
        if rel not in self.written:
            self.written.append(rel)

    def _run_shell(self, command: str) -> str:
        if not self.allow_shell:
            return "[错误] shell 已被禁用"
        if not command.strip():
            return "[错误] 空命令"
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.root,
                capture_output=True, text=True, timeout=self.shell_timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"[错误] 命令超时（>{self.shell_timeout}s）"
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        out = out.strip() or "(无输出)"
        if len(out) > _MAX_OUT:
            out = out[:_MAX_OUT] + f"\n...[截断，共 {len(out)} 字符]"
        return f"[exit={proc.returncode}]\n{out}"
