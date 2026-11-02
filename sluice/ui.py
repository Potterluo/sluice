"""CLI 美化输出层：颜色、轨迹条、分节框线。

Windows Terminal / ANSI 终端优先；检测到哑终端或 NO_COLOR 时自动降级为纯文本。
harness 的 emit(kind, text) 协议不变，这里只做渲染。
"""

from __future__ import annotations

import os
import re
import sys
import time

# ------------------------------------------------------------ ANSI 基础

def _supports_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("WT_SESSION") or os.environ.get("TERM"):
        return True
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return bool(mode.value & 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:  # noqa: BLE001
            return False
    return False


_ANSI = _supports_ansi()

_RESET = "\x1b[0m" if _ANSI else ""
_BOLD = "\x1b[1m" if _ANSI else ""
_DIM = "\x1b[2m" if _ANSI else ""
_CYAN = "\x1b[36m" if _ANSI else ""
_GREEN = "\x1b[32m" if _ANSI else ""
_YELLOW = "\x1b[33m" if _ANSI else ""
_RED = "\x1b[31m" if _ANSI else ""
_MAGENTA = "\x1b[35m" if _ANSI else ""
_BLUE = "\x1b[34m" if _ANSI else ""

# 公开别名（供 main 等外部使用）
RESET, BOLD, DIM = _RESET, _BOLD, _DIM
CYAN, GREEN, YELLOW, RED = _CYAN, _GREEN, _YELLOW, _RED
MAGENTA, BLUE = _MAGENTA, _BLUE


def c(text: str, *codes: str) -> str:
    if not _ANSI or not codes:
        return text
    return "".join(codes) + text + _RESET


def _bar(width: int = 46) -> str:
    return "─" * width


def _t0() -> float:
    return time.perf_counter()


_START = _t0()


def _since() -> str:
    return f"{time.perf_counter() - _START:5.0f}s"


# ------------------------------------------------------------ 事件渲染

# 状态轨迹（由 UiRenderer 维护）
class UiRenderer:
    """把 harness 的 emit(kind, text) 渲染成分节式界面。"""

    def __init__(self, flow_name: str = "", states: list[str] | None = None,
                 descriptions: dict[str, str] | None = None):
        self.flow_name = flow_name
        self.state_order = states or []
        self._desc = descriptions or {}
        self.current: str = ""
        self.visited: list[str] = []
        self.cycle = 0

    # ---- 状态轨迹条
    def _track(self) -> str:
        if not self.state_order:
            return ""
        parts = []
        for s in self.state_order:
            if s == self.current:
                parts.append(c(f"▶{s}", _BOLD, _CYAN))
            elif s in self.visited:
                parts.append(c(s, _DIM))
            else:
                parts.append(c("·", _DIM))
        return " ".join(parts)

    # ---- 主入口
    def render(self, kind: str, text: str) -> None:
        out = getattr(self, f"_{kind}", self._other)(text)
        if out is not None:
            print(out, flush=True)

    def _stamp(self) -> str:
        return c(f"[{_since()}]", _DIM)

    def _state(self, text: str) -> str:
        # text 形如 "状态机启动：CLARIFY" 或 "执行者进入状态 PLAN"
        m = re.search(r"(?:启动：|进入状态\s*)(\w+)", text)
        if m and m.group(1) in self.state_order:
            new_state = m.group(1)
            if new_state not in self.visited:
                self.visited.append(new_state)
            if text.startswith("状态机启动"):
                self.current = new_state
                return (f"\n{c('━' * 46, _CYAN)}\n"
                        f"{c(' PHASE ', _BOLD, _CYAN)}{c(new_state, _BOLD, _CYAN)}"
                        f"{self._sub(new_state)}\n{self._track()}\n")
            if new_state != self.current:
                self.current = new_state
                return (f"\n{c('━' * 46, _CYAN)}\n"
                        f"{c(' PHASE ', _BOLD, _CYAN)}{c(new_state, _BOLD, _CYAN)}"
                        f"{self._sub(new_state)}\n{self._track()}\n")
            return None  # 同状态重入不重绘
        return f"{self._stamp()} {text}"

    def _sub(self, state: str) -> str:
        desc = self._desc.get(state, "")
        return c(f"  {desc}", _DIM)

    _desc: dict[str, str] = {}

    def _tool(self, text: str) -> str:
        # text 形如 "write_file(path='x')"
        name = text.split("(")[0]
        return f"  {self._stamp()} {c('⚙', _DIM)} {c(name, _BLUE)}{c(text[len(name):], _DIM)}"

    def _request(self, text: str) -> str:
        # text 形如 "CLARIFY → PLAN\n<summary>"
        lines = text.split("\n", 1)
        route = lines[0]
        summary = (lines[1] if len(lines) > 1 else "").strip()
        if summary.startswith("[自动驻留请求]"):
            head = c("⏸ 停滞兜底请求", _YELLOW)
        else:
            head = c(f"➜ {route}", _BOLD, _MAGENTA)
        if not summary:
            return f"  {self._stamp()} {head}"
        body = "  " + c(summary[:160] + ("…" if len(summary) > 160 else ""), _DIM)
        return f"  {self._stamp()} {head}\n{body}"

    def _judge(self, text: str) -> str:
        # text 形如 "approve → PLAN｜reason"
        m = re.match(r"(approve|reject|redirect)\s*(?:→\s*(\w*))?(.*)", text, re.S)
        if not m:
            return f"{self._stamp()} {text}"
        decision, target, reason = m.groups()
        self.cycle += 1
        icon = {"approve": ("✔ approve", _GREEN),
                "reject": ("✘ reject ", _RED),
                "redirect": ("⤳ redirect", _YELLOW)}[decision]
        head = f"{self._stamp()} {c(icon[0], _BOLD, icon[1])}"
        if target:
            head += c(f" → {target}", _BOLD)
        # reason：Jev 概率行按｜分段缩进展示，普通理由折叠一行
        reason = (reason or "").strip().strip("｜").strip()
        if reason:
            if len(reason) > 90 or reason.count("｜") >= 2:
                segs = [s.strip() for s in reason.split("｜") if s.strip()]
                lines = [head] + [c(f"      {s[:110]}", _DIM) for s in segs[:5]]
                return "\n".join(lines) + f"\n{c('  ' + _bar(44), _DIM)}"
            head += c(f"  {reason[:110]}", _DIM)
        return f"{head}\n{c('  ' + _bar(44), _DIM)}"

    def _user(self, text: str) -> str:
        wrapped = "\n".join(f"  │ {line}" for line in text.split("\n")[:8])
        return (f"{self._stamp()} {c('┌ 裁决者提问 ─────────────', _YELLOW, _BOLD)}\n"
                f"{c(wrapped, _YELLOW)}\n"
                f"{c('  └────────────────────────', _YELLOW)}")

    def _warn(self, text: str) -> str:
        return f"{self._stamp()} {c('⚠ WATCHDOG  ', _BOLD, _YELLOW)}{c(text, _YELLOW)}"

    def _deliver(self, text: str) -> str:
        return (f"\n{c('━' * 46, _GREEN)}\n"
                f"{c(' ✔ 交付完成 ', _BOLD, _GREEN)}{c(text, _GREEN)}\n"
                f"{c('━' * 46, _GREEN)}\n")

    def _other(self, text: str) -> str:
        return f"{self._stamp()} {c(text, _DIM)}"


# ------------------------------------------------------------ 横幅与结尾

def banner(flow_name: str, states: list[str], workspace: str,
           judge_desc: str, worker_desc: str, goal: str) -> None:
    line = c("═" * 46, _CYAN)
    print(f"\n{line}")
    print(c("  ⛲ Sluice", _BOLD, _CYAN) + c("  状态机 × Agent Loop Harness", _DIM))
    print(c(f"  flow      {flow_name}", _DIM))
    print(c(f"  states    {' → '.join(states)}", _DIM))
    print(c(f"  judge     {judge_desc}", _DIM))
    print(c(f"  worker    {worker_desc}", _DIM))
    print(c(f"  workspace {workspace}", _DIM))
    print(line)
    print(c("  goal  ", _BOLD) + goal[:120] + ("…" if len(goal) > 120 else ""))
    print(line + "\n")


def footer(result, visited: list[str]) -> None:
    line = c("═" * 46, _CYAN)
    print(f"\n{line}")
    if result.delivered:
        print(c("  最终交付", _BOLD, _GREEN))
        answer = (result.final_answer or "").strip()
        print("\n".join(f"  {ln}" for ln in answer.split("\n")[:20]))
        print(c(f"\n  轨迹 {' → '.join(visited)} ｜ {result.cycles} 轮裁决", _DIM))
        print(line)
        return
    print(c("  ✘ 未能交付", _BOLD, _RED))
    print(c(f"  {result.reason}", _RED))
    print(c(f"  轨迹 {' → '.join(visited)} ｜ {result.cycles} 轮裁决", _DIM))
    print(line)
