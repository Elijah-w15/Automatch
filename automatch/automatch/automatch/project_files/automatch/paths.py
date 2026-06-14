"""All file locations in one place. config/ and output/ are the two volume
mounts when containerized; code never writes anywhere else."""
import json
import os
from pathlib import Path


def read_jsonl(path: Path):
    """Rows of a .jsonl file, skipping torn/partial lines."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue

class PipelineLock:
    """One pipeline at a time across PROCESSES (the bot container and a
    manual start.py run share output/): prune rewrites jobs.jsonl and
    scores.jsonl, so a concurrent run's appends would land on a deleted
    file and vanish, with seen.json blocking any re-scrape."""

    def __init__(self):
        self._fd = None

    def try_acquire(self) -> bool:
        import fcntl                       # pipeline always runs on linux
        self._fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def wait(self):
        import fcntl
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def acquire(self):
        if not self.try_acquire():
            print("another automatch run is in progress; waiting for it "
                  "to finish...", flush=True)
            self.wait()

    def release(self):
        if self._fd is not None:
            os.close(self._fd)             # closing the fd drops the lock
            self._fd = None


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(os.environ.get("AUTOMATCH_CONFIG", ROOT / "config"))


def _cfg_paths() -> dict:
    """Optional `paths:` block in config.yaml, so output locations are settable
    from config instead of only env vars. Best-effort: anything wrong (no yaml,
    malformed file, no block) yields {} and we fall back to the defaults."""
    try:
        import yaml
        data = yaml.safe_load((CONFIG_DIR / "config.yaml").read_text()) or {}
        return {k: str(v) for k, v in (data.get("paths") or {}).items() if v}
    except Exception:
        return {}


_PATHS = _cfg_paths()

# Precedence for each location: env var > config.yaml `paths:` > default next
# to the program. CONFIG_DIR can't be set in config.yaml (we'd have to read it
# to find the file), so it stays env-or-default above.
OUTPUT_DIR = Path(os.environ.get("AUTOMATCH_OUTPUT")
                  or _PATHS.get("output_dir") or ROOT / "output")

CONFIG = CONFIG_DIR / "config.yaml"
PROFILE = CONFIG_DIR / "profile.yaml"
# resume + stripped match-resume follow the same env > config.yaml > default
# precedence as OUTPUT_DIR, so either can point at a file outside config/.
RESUME = Path(os.environ.get("AUTOMATCH_RESUME")
              or _PATHS.get("resume") or CONFIG_DIR / "resume.txt")
RESUME_EMBED = Path(os.environ.get("AUTOMATCH_RESUME_EMBED")  # OPTIONAL: stripped
                    or _PATHS.get("resume_embed")
                    or CONFIG_DIR / "resume_embed.txt")
RESUME_TEMPLATE = CONFIG_DIR / "resume_template.txt"   # ADVANCED: has <tag>
RESUME_TEMPLATE_DOCX = CONFIG_DIR / "resume_template.docx"  # original upload
APPROVED = CONFIG_DIR / "approvedskills.txt"   # skills you confirmed having

JOBS = OUTPUT_DIR / "jobs.jsonl"        # raw scraped postings, one per line
SCORES = OUTPUT_DIR / "scores.jsonl"    # scored postings, one per line
SEEN = OUTPUT_DIR / "seen.json"         # urls already scraped (dedupe)
LOCK = OUTPUT_DIR / ".lock"             # cross-process pipeline mutex
MATCHES_JSON = OUTPUT_DIR / "matches.json"
MATCHES_HTML = OUTPUT_DIR / "matches.html"
RESUMES = OUTPUT_DIR / "resumes"        # ADVANCED: tailored resume per job
CHOICES = OUTPUT_DIR / "resume_choices.json"  # per-job keyword decisions

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
