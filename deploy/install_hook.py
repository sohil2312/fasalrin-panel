"""
Install the git pre-push hook that publishes the code to the public repo on every push of main.

    .venv\\Scripts\\python deploy\\install_hook.py      (once per PC / clone)
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".git" / "hooks" / "pre-push"
HOOK.write_text("""#!/bin/sh
# publish the code to the public repo (portable copies update from it); never blocks the push
while read local_ref local_sha remote_ref remote_sha; do
  if [ "$remote_ref" = "refs/heads/main" ]; then
    if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; else PY=.venv/bin/python; fi
    "$PY" deploy/publish_public.py || echo "[public] publish failed - run: .venv\\\\Scripts\\\\python deploy\\\\publish_public.py"
  fi
done
exit 0
""", newline="\n")
print(f"installed {HOOK}")
