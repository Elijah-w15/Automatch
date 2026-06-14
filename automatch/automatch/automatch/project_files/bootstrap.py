#!/usr/bin/env python3
"""bootstrap.py -- automatch setup doctor (run by GO.bat once Python exists).

Detects EXISTING WSL2 / Docker / Ollama / models, starts or installs only
what's missing, places files from shipped templates without clobbering user
data, then hands off to start.py (wizard + token + first run). Pure stdlib.

WSL2 is installed first on Windows because it's Docker Desktop's engine
backend; doing it up front avoids Docker's first launch dead-ending on a
'WSL2 needed' prompt.

Reuses setup.py's detection helpers so that logic lives in ONE place;
`import setup` is side-effect-free (all its execution is under __main__).

Exit codes are read by GO.bat:
  0   done / handed off cleanly
  10  a PATH-changing install happened -> GO.bat relaunches in a fresh process
  20  a reboot is needed (first-time WSL2 / Docker) -> GO.bat reboots + resumes
  2   stopped with unfinished items -> GO.bat keeps the window open
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import setup  # import-safe; reuse judge_model / _ollama_models / _model_present / offer

WIN = os.name == "nt"
HERE = Path(__file__).resolve().parent
ANCHORS = ("start.py", "setup.py", "docker-compose.yml", "config/config.yaml")
STATE = HERE / ".automatch_state"          # remembers installs already attempted

RELAUNCH, REBOOT, UNFINISHED = 10, 20, 2

DOCKER_DESKTOP = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Docker" / "Docker" / "Docker Desktop.exe",
    Path(os.environ.get("ProgramW6432", r"C:\Program Files"))
    / "Docker" / "Docker" / "Docker Desktop.exe",
]


def ok(m):   print(f"  [ok] {m}", flush=True)
def info(m): print(f"  {m}", flush=True)
def warn(m): print(f"  [--] {m}", flush=True)
def head(m): print(f"\n==== {m} " + "=" * max(0, 52 - len(m)), flush=True)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"tried": []}


def _mark_tried(key: str) -> None:
    s = _state()
    if key not in s["tried"]:
        s["tried"].append(key)
        try:
            STATE.write_text(json.dumps(s), encoding="utf-8")
        except Exception:
            pass


def _already_tried(key: str) -> bool:
    return key in _state().get("tried", [])


def _wait(check, timeout: int, label: str) -> bool:
    """Poll check() every 3s up to timeout seconds; True once it passes."""
    spin = "|/-\\"
    i = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            print()
            return True
        print(f"\r  {label}... {spin[i % 4]} ", end="", flush=True)
        i += 1
        time.sleep(3)
    print()
    return False


def _docker_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              text=True, timeout=25).returncode == 0
    except Exception:
        return False


def _compose_ok() -> bool:
    try:
        return subprocess.run(["docker", "compose", "version"],
                              capture_output=True, timeout=25).returncode == 0
    except Exception:
        return False


def _wsl_ready() -> bool:
    """True when WSL2 is installed -- it's Docker Desktop's engine backend on
    Windows. `wsl --status`/`--version` exit 0 once the platform + kernel are
    in place, and error before that."""
    if shutil.which("wsl") is None:
        return False
    for probe in (["wsl", "--status"], ["wsl", "--version"]):
        try:
            if subprocess.run(probe, capture_output=True,
                              timeout=20).returncode == 0:
                return True
        except Exception:
            pass
    return False


def _run_elevated(exe: str, args: list) -> int | None:
    """Run a command in an elevated (UAC) window, wait for it, return its exit
    code. None if it couldn't launch or the user declined the UAC prompt."""
    arglist = ",".join(f"'{a}'" for a in args)
    ps = (f"$p = Start-Process -FilePath '{exe}' -ArgumentList {arglist} "
          "-Verb RunAs -Wait -PassThru; exit $p.ExitCode")
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              timeout=1800).returncode
    except Exception:
        return None


def _docker_service_state() -> str | None:
    """RUNNING / STOPPED / None(unknown) for the Docker Desktop backend
    service (Windows only)."""
    try:
        out = subprocess.run(["sc", "query", "com.docker.service"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    if "RUNNING" in out:
        return "RUNNING"
    if "STOPPED" in out:
        return "STOPPED"
    return None


def _start_docker_service() -> bool:
    """Start the Docker Desktop backend service. It ships set to Manual and on
    a first run sometimes never starts on its own -- which leaves Docker
    Desktop's UI stuck forever on 'Starting the Docker Engine' (the engine has
    no backend and the docker-desktop WSL distro never gets provisioned).
    Starting it BEFORE we launch the UI avoids that hang. Needs elevation."""
    if _docker_service_state() != "STOPPED":
        return _docker_service_state() == "RUNNING"
    info("starting the Docker Desktop backend service (UAC prompt -> click "
         "Yes)...")
    _run_elevated("powershell", ["-NoProfile", "-Command",
                                 "Start-Service com.docker.service"])
    return _docker_service_state() == "RUNNING"


def ensure_wsl() -> int:
    """Windows only: install WSL2 BEFORE Docker. Docker Desktop's engine runs
    on WSL2 here; if it's missing, Docker installs fine but its first launch
    dead-ends on a 'WSL2 needed' prompt and the engine never starts (the
    failure where setup looked done but Docker timed out). Doing it up front,
    with --no-distribution, also skips the Ubuntu username/password prompt."""
    if not WIN:
        return 0
    head("WSL2 (Docker's engine on Windows)")
    if _wsl_ready():
        ok("WSL2 is installed")
        return 0
    if _already_tried("wsl"):
        warn("WSL still isn't here. Open PowerShell as Administrator, run:")
        warn("    wsl --install")
        warn("then reboot and double-click 'windows start here.bat' again.")
        return UNFINISHED
    _mark_tried("wsl")
    info("WSL2 isn't installed; Docker Desktop needs it. Installing now --")
    info("a Windows admin (UAC) prompt will pop up; click Yes.")
    rc = _run_elevated("wsl", ["--install", "--no-distribution"])
    if rc in (0, 3010):              # 3010 = success, reboot required
        info("WSL2 installed. A reboot finishes it.")
        return REBOOT
    warn("Couldn't install WSL automatically (the admin prompt may have been")
    warn("declined). Open PowerShell as Administrator, run:")
    warn("    wsl --install")
    warn("then reboot and double-click 'windows start here.bat' again.")
    return UNFINISHED


def ensure_docker() -> int:
    head("Docker")
    if _docker_up():
        ok("Docker is installed and running")
        ok("docker compose available") if _compose_ok() else \
            warn("docker compose plugin missing; update Docker Desktop")
        return 0

    if shutil.which("docker") is None:           # not installed at all
        if WIN:
            if _already_tried("docker"):
                warn("Docker still isn't here. Once Docker Desktop finishes "
                     "installing, open it, then double-click 'windows start here.bat' again.")
                return UNFINISHED
            _mark_tried("docker")
            info("Docker Desktop isn't installed. Installing (needs WSL2; a "
                 "reboot may follow)...")
            if setup.offer("winget install -e --id Docker.DockerDesktop "
                           "--accept-package-agreements --accept-source-agreements",
                           "[--] Docker Desktop is required to run automatch.",
                           admin=False):
                info("Docker installed. A reboot finishes WSL2 setup.")
                return REBOOT
            warn("Install Docker Desktop yourself: "
                 "https://docs.docker.com/desktop/ then run 'windows start here.bat' again")
            return UNFINISHED
        # Linux
        if setup.offer("sudo apt install -y docker.io docker-compose-v2 && "
                       "sudo usermod -aG docker $USER",
                       "[--] Docker is required.", admin=True):
            warn("log OUT and back IN (docker group), then run 'windows start here.bat' again")
        return UNFINISHED

    # installed but the daemon is down
    if WIN:
        exe = next((p for p in DOCKER_DESKTOP if p.exists()), None)
        if exe:
            info("Docker is installed but not running. Starting Docker "
                 "Desktop and waiting for the engine...")
            _start_docker_service()      # do this FIRST, or the UI can hang
            try:
                subprocess.Popen([str(exe)])
            except Exception:
                pass
            # first run provisions the docker-desktop WSL distro -> allow more
            if _wait(_docker_up, 240, "Docker engine starting"):
                ok("Docker is running")
                return 0
            warn("Docker didn't finish starting in 4 min. Wait for the tray "
                 "whale icon to go steady, then double-click 'windows start here.bat' again.")
            return UNFINISHED
        warn("Docker is installed but I can't find 'Docker Desktop.exe'. "
             "Open it manually, then double-click 'windows start here.bat' again.")
        return UNFINISHED
    # Linux daemon down
    if setup.offer("sudo systemctl enable --now docker",
                   "[--] the docker daemon isn't running."):
        if _docker_up():
            ok("Docker is running")
            return 0
    return UNFINISHED


def ensure_ollama() -> int:
    head("Ollama (local AI)")
    if setup._ollama_models() is not None:
        ok("Ollama is running")
        return _linux_ollama_fix()

    if shutil.which("ollama") is None:           # not installed
        if WIN:
            if _already_tried("ollama"):
                warn("Ollama still isn't here. Open the Ollama app once, then "
                     "double-click 'windows start here.bat' again.")
                return UNFINISHED
            _mark_tried("ollama")
            info("Ollama isn't installed. Installing...")
            if setup.offer("winget install -e --id Ollama.Ollama "
                           "--accept-package-agreements --accept-source-agreements",
                           "[--] Ollama is required (runs the local AI judge).",
                           admin=False):
                info("Ollama installed; reopening in a fresh window so it's "
                     "on PATH...")
                return RELAUNCH
            warn("Install Ollama yourself: https://ollama.com/download then "
                 "run 'windows start here.bat' again")
            return UNFINISHED
        # Linux
        if shutil.which("curl") and setup.offer(
                "curl -fsSL https://ollama.com/install.sh | sh",
                "[--] Ollama is required."):
            if setup._ollama_models() is not None:
                ok("Ollama is running")
                return _linux_ollama_fix()
        return UNFINISHED

    # installed but not serving
    info("Ollama is installed but not serving. Starting it...")
    try:
        if WIN:
            DETACHED = 0x00000008
            subprocess.Popen(["ollama", "serve"], creationflags=DETACHED)
        else:
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception:
        pass
    if _wait(lambda: setup._ollama_models() is not None, 60, "Ollama starting"):
        ok("Ollama is running")
        return _linux_ollama_fix()
    warn("Ollama didn't start serving. Open the Ollama app (or run "
         "'ollama serve'), then double-click 'windows start here.bat' again.")
    return UNFINISHED


def _linux_ollama_fix() -> int:
    """One-time systemd override so Docker containers can reach host Ollama."""
    if WIN:
        return 0
    ov = setup.OVERRIDE
    try:
        if ov.exists() and "0.0.0.0" in ov.read_text(encoding="utf-8"):
            return 0
    except Exception:
        pass
    setup.offer(
        "sudo mkdir -p /etc/systemd/system/ollama.service.d && "
        "printf '[Service]\\nEnvironment=\"OLLAMA_HOST=0.0.0.0\"\\n' | "
        "sudo tee /etc/systemd/system/ollama.service.d/override.conf && "
        "sudo systemctl daemon-reload && sudo systemctl restart ollama",
        "[--] one-time Linux fix so Docker can reach Ollama.")
    return 0


def ensure_models() -> int:
    head("AI models")
    have = setup._ollama_models()
    if have is None:
        warn("Ollama isn't reachable; skipping models for now")
        return UNFINISHED
    judge = setup.judge_model()
    info(f"judge model: {judge}")
    if judge == "mistral-nemo":
        info("(default; ~7 GB / wants ~8 GB RAM. On a low-compute PC switch to")
        info(" a lighter judge later with the Discord bot's  !model llama3.2:3b")
        info(" -- or set 'judge:' in config/config.yaml.)")
    pending = 0
    for m in ("nomic-embed-text", judge):
        if setup._model_present(m, have):
            ok(f"model {m} ready")
            continue
        info(f"downloading {m} (one-time; can be a few GB)...")
        try:
            subprocess.run(["ollama", "pull", m], check=True)
        except Exception:
            warn(f"couldn't pull {m}; double-click 'windows start here.bat' again to resume")
            pending += 1
            continue
        have = setup._ollama_models() or have
        if setup._model_present(m, have):
            ok(f"model {m} ready")
        else:
            warn(f"{m} still missing after pull; will retry next run")
            pending += 1
    return 0 if pending == 0 else UNFINISHED


def place_files() -> int:
    head("Files")
    (HERE / "output").mkdir(exist_ok=True)
    # required = the basic/local-model pipeline's files. .env.example is an
    # advanced-only Discord token TEMPLATE: nothing in the model download or
    # the basic flow reads it (the wizard writes the real .env interactively),
    # so it must NOT gate the install. It is self-healed just below.
    shipped = ["config/config.yaml", "config/profile.example.yaml",
               "config/resume.example.txt"]
    missing = [s for s in shipped if not (HERE / s).exists()]
    if missing:
        warn("this download is missing files: " + ", ".join(missing))
        warn("re-extract the zip fresh (don't unzip into an existing automatch "
             "folder), then double-click 'windows start here.bat' again")
        return UNFINISHED
    # recreate the advanced-only Discord template if a download dropped it, so
    # the gate above can never dead-end a complete, working install again
    envex = HERE / ".env.example"
    if not envex.exists():
        envex.write_text(
            "# Template for .env (advanced Discord mode). The setup wizard\n"
            "# writes the real .env for you; .env itself is never shipped.\n"
            "DISCORD_BOT_TOKEN=paste-the-private-bot-token-here\n"
            "DISCORD_PUBLIC_KEY=paste-the-public-key-here\n"
            "DISCORD_USER_ID=987654321012345678\n"
            "# DISCORD_CHANNEL_ID=112233445566778899\n",
            encoding="utf-8")
    # leave profile.yaml / resume.txt / .env ABSENT so start.py runs the
    # wizard; clear any zero-byte generated files an accidental zip shipped
    for gen in ("config/profile.yaml", "config/resume.txt", ".env"):
        p = HERE / gen
        try:
            if p.exists() and p.stat().st_size == 0:
                p.unlink()
        except Exception:
            pass
    ok("files in place")
    return 0


def handoff() -> int:
    head("Setup wizard + first run")
    info("handing off to start.py...")
    # every dependency was just installed + verified above, so tell the wizard
    # to skip its own [2] dependencies pass (inherited on through start.py ->
    # setup.py) -- the user shouldn't see the same install checks twice.
    env = {**os.environ, "AUTOMATCH_DEPS_OK": "1"}
    return subprocess.run([sys.executable, str(HERE / "start.py")],
                          env=env).returncode


def main() -> int:
    print("automatch setup doctor\n----------------------")
    if not all((HERE / a).exists() for a in ANCHORS):
        warn("this doesn't look like a complete automatch folder.")
        warn("re-extract the zip fresh, then double-click 'windows start here.bat' again.")
        return UNFINISHED
    for stage in (ensure_wsl, ensure_docker, ensure_ollama, ensure_models,
                  place_files):
        rc = stage()
        if rc in (RELAUNCH, REBOOT, UNFINISHED):
            return rc
    try:
        STATE.unlink()                 # full success up to handoff; reset tries
    except Exception:
        pass
    return handoff()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  setup cancelled.")
        sys.exit(1)
