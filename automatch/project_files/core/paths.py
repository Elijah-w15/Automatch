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
OUTPUT_DIR = Path(os.environ.get("AUTOMATCH_OUTPUT", ROOT / "output"))

CONFIG = CONFIG_DIR / "config.yaml"
PROFILE = CONFIG_DIR / "profile.yaml"
VECTORS = CONFIG_DIR / "vectors.yaml"
RESUME = CONFIG_DIR / "resume.txt"
# OPTIONAL hand-curated resume used ONLY for embedding/match similarity; when
# absent, score.py auto-strips RESUME (name + contact lines) instead.
RESUME_EMBED = CONFIG_DIR / "resume_embed.txt"
RESUME_TEMPLATE = CONFIG_DIR / "resume_template.txt"   # ADVANCED: has <tag>
RESUME_TEMPLATE_DOCX = CONFIG_DIR / "resume_template.docx"  # original upload
APPROVED = CONFIG_DIR / "approvedskills.txt"   # skills you confirmed having

JOBS = OUTPUT_DIR / "jobs.jsonl"        # raw scraped postings, one per line
SCORES = OUTPUT_DIR / "scores.jsonl"    # scored postings, one per line
SEEN = OUTPUT_DIR / "seen.json"         # urls already scraped (dedupe)
SHOWN = OUTPUT_DIR / "shown.json"       # identities shown in a prior day's top list (cross-day dedupe)
LOCK = OUTPUT_DIR / ".lock"             # cross-process pipeline mutex
MATCHES_JSON = OUTPUT_DIR / "matches.json"
MATCHES_HTML = OUTPUT_DIR / "matches.html"
RESUMES = OUTPUT_DIR / "resumes"        # ADVANCED: tailored resume per job
CHOICES = OUTPUT_DIR / "resume_choices.json"  # per-job keyword decisions

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
