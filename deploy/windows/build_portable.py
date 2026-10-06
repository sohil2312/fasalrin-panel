"""
Build a portable Fasalrin folder for a fresh Windows PC: nothing to install, unzip and double-click.

    .venv\\Scripts\\python deploy\\windows\\build_portable.py

Output: dist\\Fasalrin.zip, containing
    Fasalrin\\Start Fasalrin.bat   double-click: opens the control panel
    Fasalrin\\python\\              embedded Python 3.12 + playwright + openpyxl
    Fasalrin\\browsers\\            the Chromium build Playwright uses
    Fasalrin\\app\\                 the code (git-tracked files only: no farmer data, no logins)

The friend's own master files, work lists and logins are created inside app\\ on their PC.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
OUT = DIST / "Fasalrin"
PY_VER = "3.12.10"                        # same as the project's .venv (binary wheels must match)
PY_URL = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"
PW_CACHE = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
CODE_SKIP = ("excel_utility/", ".gitignore", ".gitattributes")

START_BAT = r"""@echo off
title Fasalrin control panel
cd /d "%~dp0app"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0browsers"
set "FASALRIN_PORT=8766"
echo Checking for updates...
"%~dp0python\python.exe" "%~dp0update.py"
echo.
echo Fasalrin control panel is starting... keep this window open while you work.
echo (Close it to stop the panel.)
"%~dp0python\python.exe" portal.py
pause
"""

README_TXT = """FASALRIN - how to use
=====================

1. Right-click Fasalrin.zip -> "Extract All..." -> pick a folder, e.g. Documents.
   (Do not run it from inside the zip.)
2. Open the extracted "Fasalrin" folder and double-click  "Start Fasalrin.bat".
   If Windows shows "Windows protected your PC": click "More info" -> "Run anyway".
3. A black window opens (keep it open) and the control panel opens in your browser
   at http://localhost:8766
4. In the panel: 1 upload your master file -> 2 build your branch -> 4 start a job.
   A Chrome window opens: log in to fasalrin.gov.in with your own login there.

To stop: close the black window.
Your data stays in the Fasalrin\\app folder on this PC. Keep a copy of that folder as backup.
"""


def run(*args, **kw):
    print("  >", " ".join(map(str, args)))
    subprocess.run(args, check=True, **kw)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    print("== 1/5 embedded Python", PY_VER)
    DIST.mkdir(exist_ok=True)
    pyzip = DIST / f"python-{PY_VER}-embed-amd64.zip"
    if not pyzip.exists():
        urllib.request.urlretrieve(PY_URL, pyzip)
    with zipfile.ZipFile(pyzip) as z:
        z.extractall(OUT / "python")
    pth = next((OUT / "python").glob("python3*._pth"))
    # embedded Python searches only the ._pth entries, not the script's folder: list the code folder too
    pth.write_text(pth.read_text().replace("#import site", "import site") + "Lib\\site-packages\n..\\app\n")

    print("== 2/5 libraries (playwright, openpyxl)")
    run(sys.executable, "-m", "pip", "install", "--no-warn-script-location", "--only-binary=:all:",
        "--python-version", "3.12", "--platform", "win_amd64",
        "--target", OUT / "python" / "Lib" / "site-packages", "-r", ROOT / "requirements.txt", "pip")
    # (pip is included so the updater can install new libraries later)

    print("== 3/5 Chromium for Playwright")
    builds = sorted(PW_CACHE.glob("chromium-*"), key=lambda p: int(p.name.split("-")[1]))
    if not builds:
        sys.exit(f"no Chromium in {PW_CACHE}: run  .venv\\Scripts\\python -m playwright install chromium  first")
    want = OUT / "browsers"
    want.mkdir()
    shutil.copytree(builds[-1], want / builds[-1].name)
    # check it is the build this playwright version expects (it fails at launch otherwise)
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(want))
    run(OUT / "python" / "python.exe", "-c",
        "from playwright.sync_api import sync_playwright\n"
        "import os\n"
        "with sync_playwright() as p: e = p.chromium.executable_path; print('  chromium', e); assert os.path.exists(e)",
        env=env)

    print("== 4/5 code (git-tracked files only)")
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    n = 0
    for rel in filter(None, files):
        if rel.startswith(CODE_SKIP) or rel in CODE_SKIP:
            continue
        dst = OUT / "app" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
        n += 1
    print(f"  {n} files")
    shutil.copy2(ROOT / "deploy" / "windows" / "update.py", OUT / "update.py")   # first start pulls the latest code
    (OUT / "Start Fasalrin.bat").write_text(START_BAT.replace("\n", "\r\n"), encoding="ascii")
    (OUT / "HOW TO USE.txt").write_text(README_TXT.replace("\n", "\r\n"), encoding="utf-8")

    print("== 5/5 zip")
    zpath = DIST / "Fasalrin.zip"
    zpath.unlink(missing_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in OUT.rglob("*"):
            if p.is_file():
                z.write(p, Path("Fasalrin") / p.relative_to(OUT))
    print(f"\nDone: {zpath}  ({zpath.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
