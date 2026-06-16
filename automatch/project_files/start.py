#!/usr/bin/env python3
"""automatch entry point: scrape -> score -> rank in Docker, open results.

First run (no profile or resume yet) auto-launches the setup wizard.
Staleness is handled inside the pipeline: every score row carries a
rubric hash, so editing your rubric or resume re-judges automatically,
while weight/threshold/filter edits just re-rank.
"""
import sys

if sys.version_info < (3, 10):
    sys.exit("automatch needs Python 3.10+; you have "
             + sys.version.split()[0])

import os
import subprocess
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "config" / "profile.yaml"
RESUME = HERE / "config" / "resume.txt"

# Windows installs no python3.exe ("python3" there hits the Store stub)
PY = "python" if os.name == "nt" else "python3"

# user-owned BEFORE docker can create it as root on a fresh clone
(HERE / "output").mkdir(exist_ok=True)

if not PROFILE.exists() or not RESUME.exists():
    print("first run: starting the setup wizard\n")
    rc = subprocess.run([sys.executable, str(HERE / "setup.py")]).returncode
    if rc == 2:        # wizard finished but flagged unfinished [--] items
        print(f"\nfinish the setup items above, then run:  {PY} start.py")
        sys.exit(0)
    if rc != 0:        # cancelled; the wizard already explained itself
        sys.exit(1)

# compose interpolates ${UID}/${GID} from the environment, but shells don't
# export them; inject the real ids so container writes match the host user
env = dict(os.environ)
if hasattr(os, "getuid"):
    env.setdefault("UID", str(os.getuid()))
    env.setdefault("GID", str(os.getgid()))

try:
    subprocess.run(
        ["docker", "compose", "run", "--build", "--rm", "automatch", "run"],
        cwd=HERE, env=env, check=True)
except FileNotFoundError:
    sys.exit(f"docker isn't installed; run:  {PY} setup.py")
except subprocess.CalledProcessError:
    sys.exit("the run failed; see the messages above "
             f"({PY} setup.py re-checks your setup)")
webbrowser.open((HERE / "output" / "matches.html").as_uri())
