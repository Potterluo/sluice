"""在 Windows 上修复 swebench 的换行问题（eval.sh / patch.diff 被 CRLF 污染）。

背景：swebench 用 `Path.write_text(...)` 落盘脚本再拷进 Linux 容器；Windows 下
默认换行翻译会把 `\n` 变成 `\r\n`，导致容器里 heredoc 打补丁失败、`conda activate`
失效、`pytest: command not found`。

修复：给这两处 write_text 显式传 `newline="\\n"`（Python 3.10+ 支持）。
可重复执行（幂等）。

用法：python tools/patch_swebench_windows.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TARGETS = [
    ('patch_file.write_text(pred["model_patch"] or "")',
     'patch_file.write_text(pred["model_patch"] or "", newline="\\n")'),
    ('eval_file.write_text(_inject_asset_restore(test_spec.eval_script, restore_cmds))',
     'eval_file.write_text(_inject_asset_restore(test_spec.eval_script, restore_cmds), newline="\\n")'),
]


def main() -> int:
    try:
        import swebench
    except ImportError:
        print("swebench 未安装", file=sys.stderr)
        return 1
    path = Path(swebench.__file__).parent / "harness" / "run_evaluation.py"
    text = path.read_text(encoding="utf-8")
    changed = 0
    for old, new in TARGETS:
        if new in text:
            print(f"已修复过: {old[:50]}...")
            continue
        if old not in text:
            print(f"[warn] 未找到目标行: {old[:60]}", file=sys.stderr)
            continue
        text = text.replace(old, new)
        changed += 1
    if changed:
        path.write_text(text, encoding="utf-8")
        print(f"已修复 {changed} 处 → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
