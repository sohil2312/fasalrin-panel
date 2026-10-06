"""
Auto-update for the portable Fasalrin folder. "Start Fasalrin.bat" runs it before the panel:

    Fasalrin\\update.py      this file (copied here by build_portable.py, refreshed by itself)
    Fasalrin\\app\\           the code it updates
    Fasalrin\\python\\        embedded Python (+ pip) it uses for new libraries

It asks GitHub for the latest commit of the public code repo. When that differs from app\\.version, it downloads
the code zip and copies the files over app\\. It only adds or replaces code files: work lists, masters, reports,
logs and logins in app\\ are never touched. When requirements.txt changed, it installs the new libraries (and
Chromium) first, and the code is only replaced after that worked. When GitHub can't be reached, the panel
starts with the code it already has.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "sohil2312/fasalrin-panel"          # public, code only
BRANCH = "main"
HERE = Path(__file__).resolve().parent
APP = HERE / "app"
PY = HERE / "python" / "python.exe"
SITE = HERE / "python" / "Lib" / "site-packages"
VERSION = APP / ".version"
DATA_DIRS = {"branches", "master", "reports", "screenshots", "dist", ".venv"}
DATA_EXT = {".csv", ".xlsx", ".xlsm", ".log", ".zip", ".bak", ".flag"}


def get(url: str, timeout=20, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "fasalrin-updater", **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def is_data(rel: str) -> bool:
    parts = rel.split("/")
    return parts[0] in DATA_DIRS or parts[0].startswith(".pw_profile") or Path(rel).suffix.lower() in DATA_EXT


def missing_libs(req: str) -> list[str]:
    """'name==version' lines of requirements.txt that the bundled Python does not have installed."""
    from importlib.metadata import PackageNotFoundError, version
    out = []
    for line in req.splitlines():
        line = line.split("#")[0].strip()
        if "==" not in line:
            continue
        name, want = (s.strip() for s in line.split("==", 1))
        try:
            if version(name) != want:
                out.append(line)
        except PackageNotFoundError:
            out.append(line)
    return out


def main() -> int:
    latest = ""
    for attempt in range(3):                       # slow / flaky internet: try a few times before giving up
        try:
            latest = get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}", 25,
                         "application/vnd.github.sha").decode().strip()
            break
        except Exception as e:
            why = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            print(f"[update] check failed (try {attempt + 1}/3): {why}")
    if not latest:
        print("[update] no update check - starting with the current version")
        return 0
    have = VERSION.read_text().strip() if VERSION.exists() else ""
    if latest == have:
        print(f"[update] up to date ({latest[:7]})")
        return 0

    print(f"[update] new version {latest[:7]} - downloading...")
    try:
        z = zipfile.ZipFile(io.BytesIO(get(f"https://codeload.github.com/{REPO}/zip/{latest}", 120)))
    except Exception as e:
        print(f"[update] download failed ({type(e).__name__}) - starting with the current version")
        return 0
    top = z.namelist()[0].split("/")[0] + "/"
    files = {n[len(top):]: n for n in z.namelist() if n.startswith(top) and not n.endswith("/") and n[len(top):]}

    # new libraries first: if that fails, keep the old code (it matches the old libraries)
    new_req = z.read(files["requirements.txt"]) if "requirements.txt" in files else b""
    need = missing_libs(new_req.decode("utf-8", "replace"))
    if need:
        print(f"[update] new libraries {', '.join(need)} - installing (a few minutes)...")
        tmp = HERE / "requirements.new.txt"
        tmp.write_bytes(new_req)
        try:
            subprocess.run([str(PY), "-m", "pip", "install", "--upgrade", "--no-warn-script-location",
                            "--target", str(SITE), "-r", str(tmp)], check=True)
            subprocess.run([str(PY), "-m", "playwright", "install", "chromium", "--no-shell"], check=True)
        except Exception as e:
            print(f"[update] library install failed ({e}) - starting with the current version")
            return 0
        finally:
            tmp.unlink(missing_ok=True)

    n = 0
    for rel, name in files.items():
        if is_data(rel):
            continue
        dst = APP / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = z.read(name)
        if not dst.exists() or dst.read_bytes() != data:
            dst.write_bytes(data)
            n += 1
    me = files.get("deploy/windows/update.py")         # the updater refreshes itself for the next start
    if me and z.read(me) != Path(__file__).read_bytes():
        Path(__file__).write_bytes(z.read(me))
    VERSION.write_text(latest)
    print(f"[update] updated to {latest[:7]} ({n} files changed)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                              # never block the panel because of the updater
        print(f"[update] skipped: {type(e).__name__}: {e}")
        sys.exit(0)
