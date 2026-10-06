"""
Copy the committed code (HEAD) to the public, code-only repo that portable copies update from.

    .venv\\Scripts\\python deploy\\publish_public.py

Runs by itself from the git pre-push hook (deploy/install_hook.py), so every push of main also updates the
public repo. Only git-tracked files of HEAD are published (git archive): never work lists, masters, reports,
logs or logins - those are not tracked (.gitignore). excel_utility/ is left out.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = "sohil2312/fasalrin-panel"
CLONE = ROOT / ".public_repo"
SKIP = ("excel_utility/",)


def git(*args, cwd=ROOT, out=False):
    r = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=not out)
    return r.stdout


def main() -> int:
    head = git("rev-parse", "--short", "HEAD").strip()
    if not CLONE.exists():
        subprocess.run(["gh", "repo", "clone", PUBLIC, str(CLONE)], check=True)
    git("fetch", "-q", "origin", cwd=CLONE)
    if git("ls-remote", "--heads", "origin", "main", cwd=CLONE).strip():
        git("checkout", "-q", "-B", "main", "origin/main", cwd=CLONE)
    else:
        git("checkout", "-q", "-B", "main", cwd=CLONE)

    for p in CLONE.iterdir():                     # replace everything except the clone's own .git
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    tar = tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD", out=True)))
    for m in tar.getmembers():
        if m.isfile() and not m.name.startswith(SKIP):
            dst = CLONE / m.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(tar.extractfile(m).read())

    git("add", "-A", cwd=CLONE)
    if not git("status", "--porcelain", cwd=CLONE).strip():
        print(f"[public] already up to date with {head}")
        return 0
    git("commit", "-q", "-m", f"Update from {head}", cwd=CLONE)
    git("push", "-q", "origin", "main", cwd=CLONE)
    print(f"[public] published {head} to github.com/{PUBLIC} - portable copies update on their next start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
