"""一键发版：跑自检 → 打包 → 校验 → 打标签 → 推送 → 建 GitHub Release 并上传 exe。

用法（仓库根目录）：
    python tools/release.py 1.0.1
    python tools/release.py 1.0.1 --no-test      # 跳过自检（不建议）
    python tools/release.py 1.0.1 --dry-run      # 只打包校验，不碰 git / GitHub

前置：
    * 工作区干净（没有未提交改动），否则拒绝执行
    * 本机已登录 GitHub（git credential manager 里存了 github.com 凭据）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "ERun-3/subtitle-puzzle"
BUILD_ARGS = [
    "--onefile", "--windowed", "--name", "台词匠",
    "--icon", "subtitle_puzzle/icon.ico",
    "--add-data", "subtitle_puzzle/assets;subtitle_puzzle/assets",
    "--exclude-module", "PySide6.QtWebEngineCore",
    "--exclude-module", "PySide6.QtWebEngineWidgets",
    "--exclude-module", "PySide6.QtQuick",
    "--exclude-module", "PySide6.QtQml",
    "--exclude-module", "PySide6.Qt3DCore",
    "--exclude-module", "PySide6.QtCharts",
    "--exclude-module", "PySide6.QtMultimedia",
    "--exclude-module", "PySide6.QtTest",
    "--exclude-module", "PySide6.QtPdf",
    "--exclude-module", "tkinter",
    "run.py",
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, text=True, **kw)


def token() -> str:
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=30).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("没拿到 GitHub 凭据：请先在终端 git push 一次完成登录")


def api(tok: str, url: str, method: str = "GET", data=None, binary=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"token {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "subtitle-puzzle-release")
    req.add_header("Content-Type",
                   "application/octet-stream" if binary else "application/json")
    body = binary if binary is not None else (json.dumps(data).encode() if data else None)
    try:
        with urllib.request.urlopen(req, body, timeout=900) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode()[:400]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="版本号，如 1.0.1（会自动加 v 前缀）")
    ap.add_argument("--no-test", action="store_true", help="跳过 selftest.py")
    ap.add_argument("--dry-run", action="store_true", help="只打包校验，不提交/不发版")
    args = ap.parse_args()

    ver = args.version.lstrip("v")
    tag = f"v{ver}"

    # 1) 工作区必须干净，避免把半成品打进 exe
    dirty = run(["git", "status", "--porcelain"], capture_output=True).stdout.strip()
    if dirty and not args.dry_run:
        print("工作区有未提交改动，先提交再发版：\n" + dirty)
        return 1

    # 2) 自检
    if not args.no_test:
        r = run([sys.executable, "selftest.py"], capture_output=True)
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        print("自检:", tail[0])
        if r.returncode != 0:
            print((r.stdout or "")[-1500:])
            return 1

    # 3) 打包
    r = run([sys.executable, "-m", "PyInstaller", *BUILD_ARGS], capture_output=True)
    if r.returncode != 0:
        print((r.stdout or "")[-2000:])
        return 1
    exe = ROOT / "dist" / "台词匠.exe"
    if not exe.exists():
        print("打包产物缺失")
        return 1
    asset_name = f"Taicijiang_v{ver}.exe"
    asset = ROOT / "dist" / asset_name
    asset.write_bytes(exe.read_bytes())
    print(f"产物: {asset.name}  {asset.stat().st_size / 1048576:.1f} MB")

    # 4) 打包程序自检
    r = run([str(exe), "--check"], capture_output=True)
    if r.returncode != 0:
        print((r.stdout or "")[-1500:])
        return 1
    print("打包自检: OK")

    if args.dry_run:
        print("dry-run：跳过 git 与 GitHub 操作")
        return 0

    # 5) 标签 + 推送
    has_tag = run(["git", "tag", "-l", tag], capture_output=True).stdout.strip()
    if not has_tag:
        run(["git", "tag", "-a", tag, "-m", f"台词匠 {tag}"])
    for cmd in (["git", "push", "origin", "main"], ["git", "push", "origin", tag]):
        r = run(cmd, capture_output=True)
        if r.returncode != 0:
            print((r.stderr or r.stdout)[-800:])
            return 1

    # 6) Release + 附件
    tok = token()
    st, rel = api(tok, f"https://api.github.com/repos/{REPO}/releases", "POST", {
        "tag_name": tag, "name": f"台词匠 {tag}",
        "body": f"见 [CHANGELOG.md](../blob/main/CHANGELOG.md) 的 {tag} 段。\n\n"
                f"Windows 单文件：`{asset_name}`（免安装，双击即用）。",
        "draft": False, "prerelease": False,
    })
    if st not in (200, 201):
        print("建 Release 失败:", st, rel)
        return 1
    rid = rel["id"]
    st, a = api(tok, f"https://uploads.github.com/repos/{REPO}/releases/{rid}/assets"
                     f"?name={asset_name}", "POST", binary=asset.read_bytes())
    if st not in (200, 201):
        print("上传附件失败:", st, a)
        return 1
    print("发布完成:", rel.get("html_url"))
    print("下载地址:", a.get("browser_download_url"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
