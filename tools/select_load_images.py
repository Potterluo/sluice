"""从 docker save 大归档里选择性加载镜像（避免把 31GB 全灌进 Docker 的 C 盘 VHD）。

用法：
    python tools/select_load_images.py D:\\swebench\\saved.tar logicstar/sweb.eval.x86_64.psf_1776_requests-1142:latest [...]

流程：读取归档末尾的 manifest.json → 只取目标镜像的 config/层 → 重建一个最小 tar →
`docker load -i`。Docker 数据盘只增长这些镜像的体积。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def read_manifest(big_tar: Path) -> list[dict]:
    with tarfile.open(big_tar, "r") as tf:
        try:
            member = tf.getmember("manifest.json")
        except KeyError:
            member = next((m for m in tf.getmembers() if m.name.endswith("manifest.json")), None)
            if member is None:
                raise SystemExit("归档里找不到 manifest.json（可能是 OCI layout 格式）")
        f = tf.extractfile(member)
        assert f is not None
        return json.loads(f.read().decode("utf-8"))


def _norm(tag: str) -> str:
    """统一去掉 docker.io/ 前缀并补 :latest。"""
    t = tag.replace("docker.io/", "", 1).strip()
    return t if ":" in t.rsplit("/", 1)[-1] or ":" in t else f"{t}:latest"


def pick(manifest: list[dict], wanted: list[str]) -> list[dict]:
    """匹配 RepoTags；同时支持不带 :tag 的写法。"""
    norm = {_norm(w) for w in wanted}
    out, matched = [], set()
    for entry in manifest:
        tags = {_norm(t) for t in (entry.get("RepoTags") or [])}
        hit = tags & norm
        if hit:
            out.append(entry)
            matched |= hit
    missing = norm - matched
    if missing:
        print(f"[warn] 归档里没有: {sorted(missing)}", file=sys.stderr)
    return out


def members_for(entry: dict) -> list[str]:
    """一个镜像需要的成员：config + 每层目录下的全部文件。"""
    paths = [entry["Config"]]
    for layer in entry["Layers"]:
        d = layer.rsplit("/", 1)[0] if "/" in layer else ""
        paths.append(layer)
        if d:
            paths += [f"{d}/json", f"{d}/VERSION"]
    return paths


def build_subset_tar(big_tar: Path, entries: list[dict], out_tar: Path) -> int:
    """从大 tar 里拷贝目标成员 + 过滤后的 manifest.json。"""
    wanted: set[str] = set()
    for e in entries:
        wanted.update(members_for(e))
    manifest_bytes = json.dumps(entries).encode("utf-8")

    written = 0
    with tarfile.open(big_tar, "r") as src, tarfile.open(out_tar, "w") as dst:
        for m in src:
            if m.name in wanted:
                data = src.extractfile(m)
                if data is None:
                    continue
                info = tarfile.TarInfo(name=m.name)
                info.size = m.size
                info.mode = m.mode
                info.mtime = m.mtime
                dst.addfile(info, data)
                written += 1
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        import io
        dst.addfile(info, io.BytesIO(manifest_bytes))
    return written


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    big_tar = Path(sys.argv[1])
    wanted = sys.argv[2:]
    manifest = read_manifest(big_tar)
    entries = pick(manifest, wanted)
    if not entries:
        print("没有匹配到镜像", file=sys.stderr)
        return 1
    print(f"归档共 {len(manifest)} 个镜像，命中 {len(entries)} 个：")
    for e in entries:
        print("  -", e.get("RepoTags"))

    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False, dir=str(big_tar.parent)) as tmp:
        out = Path(tmp.name)
    try:
        n = build_subset_tar(big_tar, entries, out)
        size = out.stat().st_size / 1e9
        print(f"子归档 {n} 个成员，{size:.2f} GB → docker load ...")
        rc = subprocess.run(["docker", "load", "-i", str(out)]).returncode
        print(f"docker load 退出码 {rc}")
        return rc
    finally:
        out.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
