"""断点续传下载器：Range 分块 + 重试（用于被限速抖动的镜像）。"""
import os
import sys
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0"}


def download(url: str, dest: str, tries: int = 40) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    while True:
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = dict(UA)
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                total = have + int(r.headers.get("Content-Length") or 0)
                if r.status == 200 and have:
                    os.remove(part)  # 服务端不支持 Range，重来
                    continue
                mode = "ab" if have else "wb"
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done = f.tell()
                        if done % (50 << 20) < (1 << 20):
                            print(f"  {done/1e9:.2f}/{total/1e9:.2f} GB", flush=True)
            size = os.path.getsize(part)
            if total and size < total:
                raise IOError(f"不完整: {size}/{total}")
            os.replace(part, dest)
            print(f"完成: {dest} ({size/1e9:.2f} GB)", flush=True)
            return
        except Exception as e:
            tries -= 1
            if tries <= 0:
                raise
            print(f"重试（剩 {tries}）: {e}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    download(sys.argv[1], sys.argv[2])
