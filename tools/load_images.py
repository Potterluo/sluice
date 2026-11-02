"""把 zstd 压缩的 docker save 归档流式导入 docker（不需要 31GB 临时文件）。

用法：python tools/load_images.py D:\\swebench\\saved.tar.zst
参考：LogicStar/SWE-Bench-Verified-Compressed 的官方加载方式
      （zstd -d --long=31 --stdout | docker load）
"""

from __future__ import annotations

import subprocess
import sys
import time

import zstandard

CHUNK = 8 << 20  # 8 MiB


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python tools/load_images.py <arch.tar.zst>")
        return 2
    path = sys.argv[1]

    # --long=31 意味着窗口上限 2GiB，Python 端要放开限制
    dctx = zstandard.ZstdDecompressor(max_window_size=2 ** 31)
    t0 = time.time()
    proc = subprocess.Popen(["docker", "load"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    total = 0
    with open(path, "rb") as f:
        reader = dctx.stream_reader(f)
        while True:
            chunk = reader.read(CHUNK)
            if not chunk:
                break
            proc.stdin.write(chunk)
            total += len(chunk)
            if total % (2 << 30) < CHUNK:
                print(f"  已解压 {total/2**30:.1f} GiB ({time.time()-t0:.0f}s)", flush=True)
    proc.stdin.close()
    rc = proc.wait()
    print(f"docker load 退出码 {rc}，用时 {time.time()-t0:.0f}s，共解压 {total/2**30:.1f} GiB")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
