#!/usr/bin/env python3
"""automatch entry point: scrape -> score -> rank in Docker, open results.

First run (no profile or resume yet) auto-launches the setup wizard, then
OFFERS to pre-build the Docker image -- but it never auto-scrapes and never
auto-starts the bot. A first-time user shouldn't get a wall of download output
they didn't approve, so we ask first; then they start things themselves
(`python start.py` for a scrape, or `automatch` for the ATS bot).

Staleness is handled inside the pipeline: every score row carries a rubric
hash, so editing your rubric or resume re-judges automatically, while
weight/threshold/filter edits just re-rank.
"""
import sys

if sys.version_info < (3, 10):
    sys.exit("automatch needs Python 3.10+; you have "
             + sys.version.split()[0])

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

# Know our own root before doing anything else: every path below is derived
# from this file's location (resolve() follows symlinks), so `python start.py`
# works from any working directory. If we're somehow NOT sitting in the project
# (the old "can't find start.py / its files" failure), say so loudly up front
# instead of dying deep in the pipeline with a confusing error.
HERE = Path(__file__).resolve().parent
_missing_anchors = [a for a in ("setup.py", "docker-compose.yml", "config")
                    if not (HERE / a).exists()]
if _missing_anchors:
    sys.exit("start.py can't find the automatch project around it (missing: "
             + ", ".join(_missing_anchors) + "). Keep start.py inside the "
             "automatch folder, or re-extract the download fresh.")

PROFILE = HERE / "config" / "profile.yaml"
RESUME = HERE / "config" / "resume.txt"
ENV = HERE / ".env"

# Windows installs no python3.exe ("python3" there hits the Store stub)
PY = "python" if os.name == "nt" else "python3"

# user-owned BEFORE docker can create it as root on a fresh clone
(HERE / "output").mkdir(exist_ok=True)


def _missing(p: Path) -> bool:
    # a zero-byte file (e.g. an accidentally-shipped empty generated file)
    # counts as missing, so the wizard still runs instead of being skipped
    return (not p.exists()) or p.stat().st_size == 0


def _compose_env() -> dict:
    # compose interpolates ${UID}/${GID} from the environment, but shells don't
    # export them; inject the real ids so container writes match the host user
    env = dict(os.environ)
    if hasattr(os, "getuid"):
        env.setdefault("UID", str(os.getuid()))
        env.setdefault("GID", str(os.getgid()))
    return env


def _docker_ready() -> bool:
    """True only when docker is installed AND its daemon answers; prints the
    fix and returns False otherwise (compose throws a raw npipe/socket error
    if you skip this check)."""
    if shutil.which("docker") is None:
        print("  Docker isn't installed yet -- double-click "
              "'windows start here.bat' to set it up "
              f"(or run {PY} setup.py).")
        return False
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("  Docker is installed but not running -- open Docker Desktop, "
              "wait for the whale icon to go steady, then try again.")
        return False
    return True


def _finish_first_run() -> None:
    """Right after the wizard, the last thing to do is download the container.
    Guide the user into it with a friendly 'ready to begin?' rather than a
    blunt 'required' -- we still confirm before a few GB of download, and never
    auto-scrape or auto-start the bot; the user starts those themselves."""
    advanced = ENV.exists()       # step_discord wrote .env -> advanced setup
    print()
    ans = input(
        "  the final step is to download the container file (a few GB, one\n"
        "  time). are you ready to begin? (y/n --> enter) ").strip().lower()
    if ans in ("", "y", "yes"):
        if _docker_ready():
            print("\n  downloading and setting up the container now...\n")
            cmd = ["docker", "compose"]
            if advanced:                       # also build the bot image
                cmd += ["--profile", "advanced"]
            cmd += ["build"]
            rc = subprocess.run(cmd, cwd=HERE, env=_compose_env()).returncode
            print("\n  all set -- automatch is ready to go." if rc == 0 else
                  "\n  that didn't finish downloading; whenever you're ready, "
                  "run  " + PY + " start.py  to pick it back up.")
        else:
            print("  start Docker Desktop first, then run  " + PY + " start.py "
                  " to download the container.")
    else:
        print("  no problem -- whenever you're ready, run  " + PY + " start.py "
              " to download the container and finish up.")
    # GO.bat prints its own terminal-landing banner with these same
    # instructions, so skip ours there -- the user should see one block, not
    # two. Standalone `python start.py` has no such banner, so we print it.
    if os.environ.get("AUTOMATCH_FROM_GOBAT") == "1":
        return
    print("\n" + "=" * 66)
    if advanced:
        print("  you're all set! to begin:")
        print("    run the command  automatch  in a terminal and then")
        print("    message the bot  !match  on discord to begin.")
    else:
        print("  you're all set! nothing is running yet -- to begin:")
        print(f"    run  {PY} start.py   (scrape -> score -> open matches.html)")
    print("=" * 66)


def _run_pipeline() -> None:
    """The real thing: build (cached after the first time) + run the scrape ->
    score -> rank pipeline, then open the results."""
    if not _docker_ready():
        sys.exit(1)
    try:
        subprocess.run(
            ["docker", "compose", "run", "--build", "--rm", "automatch", "run"],
            cwd=HERE, env=_compose_env(), check=True)
    except FileNotFoundError:
        sys.exit(f"docker isn't installed; run:  {PY} setup.py")
    except subprocess.CalledProcessError:
        sys.exit("the run failed; see the messages above "
                 f"({PY} setup.py re-checks your setup)")
    webbrowser.open((HERE / "output" / "matches.html").as_uri())


if _missing(PROFILE) or _missing(RESUME):
    print("first run: starting the setup wizard\n")
    # AUTOMATCH_FROM_START lets the wizard skip its own closing "run it"
    # banner -- we print the first-run next-steps (with the build prompt) here
    # instead, so the user sees one clean ending, not two.
    rc = subprocess.run(
        [sys.executable, str(HERE / "setup.py")],
        env={**os.environ, "AUTOMATCH_FROM_START": "1"}).returncode
    if rc == 2:        # wizard finished but flagged unfinished [--] items
        print(f"\nfinish the setup items above, then run:  {PY} start.py")
        sys.exit(0)
    if rc != 0:        # cancelled; the wizard already explained itself
        sys.exit(1)
    _finish_first_run()        # ask-to-build, then stop -- no auto-run
    sys.exit(0)

# Already set up. If the INSTALLER (the "start here" wrapper) re-invoked us, do
# NOT scrape -- the user double-clicked to set up, not to run a job search. Only
# a direct `python start.py` (AUTOMATCH_INSTALLER unset) falls through to the
# pipeline below, exactly as before.
if os.environ.get("AUTOMATCH_INSTALLER") == "1":
    # On Windows GO.bat prints its own :ready banner with these instructions,
    # so stay silent there (same reason the first-run banner is skipped above);
    # on Linux there's no such banner, so print the guidance here.
    if os.environ.get("AUTOMATCH_FROM_GOBAT") != "1":
        print("  automatch is already set up -- the installer doesn't scrape.")
        print(f"  to fetch jobs now:         run  {PY} start.py")
        print("  to start the Discord bot:  run  automatch")
    sys.exit(0)

_run_pipeline()
