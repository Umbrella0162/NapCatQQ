#!/usr/bin/env python3
"""提取 / 比较镜像契约指纹，用来断言本仓库构建的镜像与官方一致。

    parity.py extract <docker-archive.tar> -o <out.json>
    parity.py compare <official.json> <actual.json>

只依赖 Python 标准库。契约内容：image config、环境变量、napcat 用户、
官方注入文件的 sha256、QQ 版本与 package.main、必需命令与 apt 包。
"""

import argparse
import gzip
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

TRACKED_FILES = [
    "/app/entrypoint.sh",
    "/app/templates/astrbot.json",
    "/app/templates/dice.json",
    "/app/templates/koishi.json",
    "/app/templates/nonebot.json",
    "/app/templates/olivos.json",
    "/app/templates/q2tg.json",
    "/app/templates/qq-ai-bot.json",
    "/app/templates/sealdice.json",
    "/app/templates/ws.json",
    "/opt/QQ/resources/app/loadNapCat.js",
]

REQUIRED_PACKAGES = [
    "libnss3", "libnotify4", "libsecret-1-0", "libgbm1", "xvfb",
    "libasound2", "fonts-wqy-zenhei", "gnutls-bin", "libglib2.0-dev",
    "libdbus-1-3", "libgtk-3-0", "libxss1", "libxtst6", "libatspi2.0-0",
    "libx11-xcb1", "ffmpeg", "unzip", "dbus-user-session", "curl",
    "jq", "gosu", "tzdata", "linuxqq",
]

REQUIRED_COMMANDS = ["Xvfb", "gosu", "jq", "unzip", "ffmpeg", "curl"]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _decompress(src, dst):
    """把 src 解压到 dst；返回压缩类型名。"""
    with open(src, "rb") as f:
        magic = f.read(4)
    if magic[:2] == b"\x1f\x8b":
        with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        return "gzip"
    if magic == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard  # type: ignore
            with open(src, "rb") as fin, open(dst, "wb") as fout:
                zstandard.ZstdDecompressor().copy_stream(fin, fout)
        except ImportError:
            if shutil.which("zstd") is None:
                sys.exit("错误：层是 zstd 压缩，但缺少 zstandard 模块与 zstd 命令")
            with open(dst, "wb") as fout:
                subprocess.run(["zstd", "-dc", src], check=True, stdout=fout)
        return "zstd"
    return "none"


def _extract_layer(layer_path, dest):
    """展开单层；层可能是未压缩 tar、gzip 或 zstd（docker save 的三种产物）。"""
    tar_path = layer_path
    workdir = os.path.dirname(layer_path)
    decompressed = os.path.join(workdir, "layer.dec.tar")
    kind = _decompress(layer_path, decompressed)
    if kind != "none":
        tar_path = decompressed

    with tarfile.open(tar_path, "r|") as layer:
        for member in layer:
            name = member.name.lstrip("./")
            if not name:
                continue
            base = posixpath.basename(name)
            if base.startswith(".wh."):  # 上层删除
                victim = os.path.join(dest, posixpath.dirname(name),
                                      base[len(".wh."):])
                subprocess.run(["rm", "-rf", victim], check=False)
                continue
            target = os.path.join(dest, name)
            if not os.path.realpath(target).startswith(
                    os.path.realpath(dest) + os.sep):
                continue
            try:
                # filter="tar" 在 Python 3.12+ 可用，能挡掉绝对路径与 ..
                try:
                    layer.extract(member, dest, numeric_owner=True, filter="tar")
                except TypeError:
                    layer.extract(member, dest, numeric_owner=True)
            except (PermissionError, OSError):
                pass


def _spill(tf, member_name, workdir, prefix):
    """把外层归档里的成员流式落盘，返回路径。"""
    out = os.path.join(workdir, f"{prefix}-{posixpath.basename(member_name)}")
    stream = tf.extractfile(member_name)
    if stream is None:
        return None
    with open(out, "wb") as f:
        shutil.copyfileobj(stream, f, 1 << 20)
    return out


def _resolve_layers(tf, names, workdir):
    """返回 (config 路径, 按顺序的层路径)。

    支持两种布局：docker-archive（manifest.json）与 OCI（index.json + blobs/）。
    """
    if "manifest.json" in names:
        manifest = json.loads(tf.extractfile("manifest.json").read())
        config = _spill(tf, manifest[0]["Config"], workdir, "cfg")
        layers = [_spill(tf, n, workdir, "layer") for n in manifest[0]["Layers"]]
        return config, [l for l in layers if l]

    if "index.json" in names:  # OCI / containerd 镜像存储
        index = json.loads(tf.extractfile("index.json").read())
        manifests = index.get("manifests") or []
        if not manifests:
            sys.exit("错误：OCI 布局里没有 manifests")
        digest = manifests[0]["digest"].split(":")[-1]
        manifest = json.loads(
            tf.extractfile(f"blobs/sha256/{digest}").read())
        cfg_digest = manifest["config"]["digest"].split(":")[-1]
        config = _spill(tf, f"blobs/sha256/{cfg_digest}", workdir, "cfg")
        layers = [
            _spill(tf, "blobs/sha256/" + item["digest"].split(":")[-1],
                   workdir, "layer")
            for item in manifest["layers"]
        ]
        return config, [l for l in layers if l]

    sys.exit("错误：既没有 manifest.json 也没有 index.json，不是镜像归档")


def unpack(archive, dest):
    """按顺序展开层，返回 image config。"""
    workdir = tempfile.mkdtemp(prefix="layers-")
    try:
        with tarfile.open(archive) as tf:
            names = set(tf.getnames())
            config_path, layer_paths = _resolve_layers(tf, names, workdir)
            for layer_path in layer_paths:
                _extract_layer(layer_path, dest)
            with open(config_path, "rb") as f:
                return json.loads(f.read())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def installed_packages(root):
    status = os.path.join(root, "var/lib/dpkg/status")
    if not os.path.exists(status):
        return set()
    names = set()
    with open(status, encoding="utf-8", errors="ignore") as f:
        for block in f.read().split("\n\n"):
            pkg = re.search(r"^Package: (.+)$", block, re.M)
            st = re.search(r"^Status: (.+)$", block, re.M)
            if pkg and st and "install ok installed" in st.group(1):
                names.add(pkg.group(1))
    return names


def build_contract(cfg, root):
    config = cfg.get("config", {})
    env = dict(
        (item.partition("=")[0], item.partition("=")[2])
        for item in (config.get("Env") or [])
    )

    os_release = {}
    release = os.path.join(root, "etc/os-release")
    if os.path.exists(release):
        for line in open(release, encoding="utf-8", errors="ignore"):
            if "=" in line:
                k, _, v = line.strip().partition("=")
                os_release[k] = v.strip('"')

    files = {}
    for path in TRACKED_FILES:
        real = os.path.join(root, path.lstrip("/"))
        files[path] = sha256_file(real) if os.path.isfile(real) else None

    passwd = {}
    pw = os.path.join(root, "etc/passwd")
    if os.path.exists(pw):
        for line in open(pw, encoding="utf-8", errors="ignore"):
            parts = line.strip().split(":")
            if len(parts) >= 7:
                passwd[parts[0]] = {
                    "uid": int(parts[2]), "gid": int(parts[3]),
                    "home": parts[5], "shell": parts[6],
                }

    qq_version = qq_main = None
    qq_app = os.path.join(root, "opt/QQ/resources/app/package.json")
    if os.path.exists(qq_app):
        try:
            d = json.load(open(qq_app, encoding="utf-8"))
            qq_version, qq_main = d.get("version"), d.get("main")
        except (json.JSONDecodeError, OSError):
            pass

    commands = {}
    for cmd in REQUIRED_COMMANDS:
        hit = None
        for d in ("usr/bin", "usr/sbin", "bin", "sbin", "usr/local/bin"):
            p = os.path.join(root, d, cmd)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                hit = f"/{d}/{cmd}"
                break
        commands[cmd] = hit

    entrypoint = os.path.join(root, "app/entrypoint.sh")
    installed = installed_packages(root)

    return {
        "os": {"id": os_release.get("ID"), "version_id": os_release.get("VERSION_ID")},
        "config": {
            "User": config.get("User"),
            "WorkingDir": config.get("WorkingDir"),
            "Entrypoint": config.get("Entrypoint"),
            "Cmd": config.get("Cmd"),
            "Volumes": sorted((config.get("Volumes") or {}).keys()),
            "ExposedPorts": sorted((config.get("ExposedPorts") or {}).keys()),
            "StopSignal": config.get("StopSignal"),
        },
        "env": {
            "DEBIAN_FRONTEND": env.get("DEBIAN_FRONTEND"),
            "TZ": env.get("TZ"),
        },
        "napcat_user": passwd.get("napcat"),
        "files": files,
        "entrypoint_mode": oct(os.stat(entrypoint).st_mode & 0o777)[2:]
        if os.path.exists(entrypoint) else None,
        "qq": {
            "version": qq_version,
            "package_main": qq_main,
            "binary": os.path.isfile(os.path.join(root, "opt/QQ/qq")),
        },
        "commands": commands,
        "packages_missing": [p for p in REQUIRED_PACKAGES if p not in installed],
    }


def walk(prefix, want, got, diffs):
    if type(want) is not type(got):
        diffs.append((prefix, want, got))
        return
    if isinstance(want, dict):
        for key in sorted(set(want) | set(got)):
            if key not in want:
                diffs.append((f"{prefix}.{key}", "<缺失>", got[key]))
            elif key not in got:
                diffs.append((f"{prefix}.{key}", want[key], "<缺失>"))
            else:
                walk(f"{prefix}.{key}", want[key], got[key], diffs)
    elif want != got:
        diffs.append((prefix, want, got))


def cmd_extract(args):
    with tempfile.TemporaryDirectory(prefix="imgroot-") as dest:
        contract = build_contract(unpack(args.archive, dest), dest)
    text = json.dumps(contract, indent=2, ensure_ascii=False, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


def cmd_compare(args):
    want = json.load(open(args.official, encoding="utf-8"))
    got = json.load(open(args.actual, encoding="utf-8"))
    diffs = []
    walk("", want, got, diffs)
    if not diffs:
        print("✅ 契约完全一致（配置 / 环境变量 / 文件哈希 / 依赖 / 用户 / QQ）")
        return 0
    print("❌ 契约存在差异：")
    for path, a, b in diffs:
        print(f"  - {path.lstrip('.')}")
        print(f"      官方: {json.dumps(a, ensure_ascii=False)}")
        print(f"      本镜像: {json.dumps(b, ensure_ascii=False)}")
    print(f"\n共 {len(diffs)} 处差异")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract")
    e.add_argument("archive")
    e.add_argument("-o", "--out")
    e.set_defaults(func=cmd_extract)

    c = sub.add_parser("compare")
    c.add_argument("official")
    c.add_argument("actual")
    c.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())