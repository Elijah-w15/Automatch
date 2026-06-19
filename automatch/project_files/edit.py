#!/usr/bin/env python3
"""edit.py - change your automatch config after setup, no full rerun:

    python3 edit.py

  1) scoring metrics    (add / remove / edit / replace, incl. weights)
  2) scoring settings   (threshold, wildcard, judge model)
  3) job search         (titles, location, radius, filters)
  4) Discord login      (token / public key / user id)
  5) resume             (tagged resume + matching/embedding resume)

Edits config/profile.yaml, config/resume*.txt and .env IN PLACE, keeping
everything you don't touch. The bot hot-reloads profile.yaml each !scrape, so
metric/search edits need NO restart; a Discord-login change DOES need a restart.
"""
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup            # noqa: E402  shared wizard + helpers (stdlib + yaml only)
import edit_metrics     # noqa: E402  the metrics editor

CONFIG_DIR = setup.HERE / "config"
RESUME_EMBED = CONFIG_DIR / "resume_embed.txt"   # matches core/paths.py
TAG = setup.MARKER                               # "<tag>"


# ============================================================ input helpers ==
def _prompt_path(label: str):
    """Ask for a file path (drag-drop pastes one). Returns Path, or None to
    cancel. Loops on a bad path so a typo never silently proceeds."""
    while True:
        p = input(f"  {label}\n  (drag the file here or type its path; "
                  "blank to cancel): ").strip().strip("'\"")
        if not p:
            return None
        src = Path(p).expanduser()
        if src.exists():
            return src
        print(f"  can't find {src}; try again, or blank to cancel")


def yn_more(question: str, more_text: str, default=None) -> bool:
    """y/n prompt that also takes !more (prints more_text, re-asks).
    default (True/False) is used on a blank answer; None = must answer."""
    while True:
        v = input(f"  {question} (y/n or !more): ").strip().lower()
        if v in ("!more", "more"):
            print(more_text)
            continue
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False
        if v == "" and default is not None:
            return default
        print("  please answer y, n, or !more")


# ===== strip preview: a faithful MIRROR of core/score.py (which can't be
# imported here -- it pulls in jobspy, a Docker-only dep). Keep in sync. =====
_CONTACT = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+"
    r"|(?:https?://|www\.)\S+"
    r"|\b(?:github|linkedin)\.com/\S+"
    r"|(?:\+?\d{1,3}[-. ]*)?\(?\d{3}\)?[-. ]*\d{3}[-. ]*\d{4}"
    r"|•", re.I)


def _clean_text(s: str) -> str:
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    return re.sub(r"\s+", " ", s).strip()


def _strip_contact(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    name = ""
    if lines and 0 < len(lines[0].split()) <= 5:
        name = lines[0].strip()
        lines = lines[1:]
    body = "\n".join(lines)
    if name:
        body = re.sub(re.escape(name), " ", body, flags=re.I)
        for w in name.split():
            if len(w) >= 3:
                body = re.sub(rf"(?i)(?<![a-z']){re.escape(w)}(?![a-z'])",
                              " ", body)
    return _CONTACT.sub(" ", body)


# ====================================================== 2) JOB SEARCH ========
_METRICS_MARKER = "# ====== YOUR SCORING METRICS"


def _set_scalar(lines: list, key: str, value) -> None:
    """Replace `key: ...` in place, preserving any trailing inline # comment.
    If the key is absent (e.g. a profile written without a threshold line),
    insert it just before the metrics block, else append it."""
    for i, l in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", l):
            m = re.search(r"(\s+#.*)$", l)
            lines[i] = f"{key}: {value}{m.group(1) if m else ''}"
            return
    for i, l in enumerate(lines):              # not found: insert before metrics
        if l.strip().startswith(_METRICS_MARKER):
            lines.insert(i, f"{key}: {value}")
            lines.insert(i + 1, "")
            return
    lines.append(f"{key}: {value}")


def _set_list(lines: list, key: str, items: list) -> None:
    """Replace `key:` + its `- ` children in place (or `key: []` when empty).
    If the key is absent, insert it before the metrics block, else append, so a
    hand-edited profile missing the key never silently no-ops."""
    children = [f"  - {setup.yv(x)}" for x in items]
    header = f"{key}:" if items else f"{key}: []"
    for i, l in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", l):
            j = i + 1
            while j < len(lines) and re.match(r"^\s+-\s", lines[j]):
                j += 1
            lines[i:j] = [header] + children
            return
    for i, l in enumerate(lines):              # not found: insert before metrics
        if l.strip().startswith(_METRICS_MARKER):
            lines[i:i] = [header] + children + [""]
            return
    lines.extend([header] + children)


def edit_search() -> None:
    s = setup.step_search()        # reuse the wizard (prints its own header)
    lines = setup.PROFILE.read_text().split("\n")
    _set_list(lines, "search_terms", s["terms"])
    _set_scalar(lines, "location", setup.yv(s["loc"]))
    _set_scalar(lines, "radius_miles", s["radius"])
    _set_scalar(lines, "max_listing_age_hours", s["age"])
    _set_scalar(lines, "max_jobs", s["cap"])
    _set_scalar(lines, "level", s["level"])
    _set_scalar(lines, "salary_min", s["salary"])
    _set_list(lines, "exclude", s["excludes"])
    setup.PROFILE.write_text("\n".join(lines))
    print("\n  job search updated. applies on your next !scrape (no restart).")


# ============================================ 2b) THRESHOLD & WILDCARD =======
def edit_scoring() -> None:
    import yaml
    lines = setup.PROFILE.read_text().split("\n")
    data = yaml.safe_load("\n".join(lines)) or {}
    print("\n  ==== match threshold & wildcard " + "=" * 33)
    print(f"  current threshold: {data.get('threshold', 0)}   (min average score "
          "0-1 a job needs; 0 = show all)")
    print(f"  current wildcard:  {data.get('wildcard') or '(off)'}")
    th = input("\n  new threshold 0-1 (blank = keep): ").strip()
    if th:
        try:
            if 0 <= float(th) <= 1:
                _set_scalar(lines, "threshold", th)
                print(f"  threshold set to {th}.")
            else:
                print("  must be between 0 and 1; threshold unchanged.")
        except ValueError:
            print("  not a number; threshold unchanged.")
    print("\n  the wildcard is one extra pick per run: the job that best matches")
    print("  a description you define, even if it scores below your top matches.")
    wc = input("  new wildcard description (blank = keep, 'off' = disable): ").strip()
    if wc.lower() == "off":
        _set_scalar(lines, "wildcard", "")
        print("  wildcard disabled.")
    elif wc:
        _set_scalar(lines, "wildcard", setup.yv(wc))
        print("  wildcard updated.")
    setup.PROFILE.write_text("\n".join(lines))
    print("\n  saved. applies on your next !scrape (threshold re-ranks instantly;")
    print("  a wildcard change re-scores).")


# ============================================ 2c) SCORING MODEL ==============
def edit_model() -> None:
    have = setup._ollama_models()
    if have is None:
        print("\n  ollama isn't reachable from here, so I can't download/switch the")
        print("  model. start ollama first, or switch from Discord with `!model 1-4`")
        print("  (it downloads + switches in one step, inside the bot).")
        return
    setup.step_model(have)              # picker + writes the judge to config.yaml
    judge = setup.judge_model()
    if not setup._model_present(judge, have):
        setup._pull_model(judge)        # download the chosen judge now
    print("  saved. the bot picks it up on your next !scrape (no restart).")


# ============================================ 2) SCORING SETTINGS (group) ====
def edit_scoring_settings() -> None:
    while True:
        print("\n  scoring settings:")
        print("   1) match threshold & wildcard")
        print("   2) judge model (which local AI scores jobs)")
        print("   3) back")
        c = input("  pick 1-3: ").strip().lower()
        if c in ("1", "threshold", "wildcard"):
            edit_scoring()
        elif c in ("2", "model"):
            edit_model()
        elif c in ("3", "back", "b", "q", ""):
            return
        else:
            print("  pick 1-3.")


# ====================================================== 3) DISCORD LOGIN =====
def _read_env() -> dict:
    out = {}
    if setup.ENV.exists():
        for line in setup.ENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def edit_discord() -> None:
    cur = _read_env()

    def keep(label, key, secret=False):
        have = "set" if cur.get(key) else "not set"
        prompt = f"  {label} (currently {have}; blank = keep)"
        v = (setup.ask_secret(prompt) if secret else input(prompt + ": ").strip())
        return v or cur.get(key, "")

    print("\n  ==== Discord login (.env) " + "=" * 39)
    print("  blank keeps the current value. find these in the Discord developer")
    print("  portal -> your app -> Bot (token) / General Information (public key).")
    token = keep("bot TOKEN (private)", "DISCORD_BOT_TOKEN", secret=True)
    pub = keep("PUBLIC key", "DISCORD_PUBLIC_KEY")
    uid = keep("your USER ID", "DISCORD_USER_ID")
    cid = keep("channel ID (blank if you DM the bot directly)",
               "DISCORD_CHANNEL_ID")
    setup._write_env({"DISCORD_BOT_TOKEN": token, "DISCORD_PUBLIC_KEY": pub,
                      "DISCORD_USER_ID": uid, "DISCORD_CHANNEL_ID": cid})
    print("\n  saved to .env. RESTART the bot (run `automatch` again) so it logs")
    print("  in with the new credentials -- this one does NOT hot-reload.")


# ====================================================== 4) RESUME ============
TAG_MORE = (
    f"\n  the {TAG} is a placeholder you put on your resume's skills line. for\n"
    f"  every job, the bot swaps {TAG} for that job's keywords so your resume\n"
    "  clears ATS keyword filters. example skills line:\n"
    f"      skills: microsoft office, {TAG}, python, sql\n"
    "  a working example ships at docs/resume_tag.example.docx\n")

TAG_FORMAT_MORE = (
    f"\n  put the literal text {TAG} (angle brackets included) in your skills\n"
    "  line, then save as .txt or .docx. example:\n"
    f"      skills: excel, {TAG}, sql, tableau\n"
    "  see docs/resume_tag.example.docx for a full one.\n")

TAGLESS_MORE = (
    "\n  without a <tag>, scraping, scoring and matching all still work -- you\n"
    "  just don't get the per-job tailored resume the bot DMs you (it has no\n"
    "  place to inject each job's keywords). you can add a tagged resume later\n"
    "  by running this editor again.\n")

EMBED_MORE = (
    "\n  for MATCHING, the bot turns your resume into an embedding and compares\n"
    "  it to each posting. names, contact info and headers are noise.\n"
    "   - auto-strip (default): the bot removes your name + contact lines and\n"
    "     embeds the rest. nothing for you to do.\n"
    "   - custom: you upload a hand-trimmed resume (skills/experience only, no\n"
    "     name/contact/fluff). sometimes matches better. it is used ONLY for\n"
    "     matching; your real/tailored resume is untouched.\n")


def _upload_tagged() -> bool:
    while True:
        src = _prompt_path("path to your tagged resume (.txt or .docx)")
        if src is None:
            return False
        if src.suffix.lower() not in (".txt", ".md", ".docx", ""):
            print(f"  {src.suffix} won't work; use .txt or .docx")
            continue
        try:
            text = setup._read_resume(src)
        except Exception:
            print("  couldn't read that file. is it a normal .docx or .txt?")
            continue
        if TAG not in text:
            print(f"  no {TAG} recognized in that file.")
            if not yn_more("  try a different file?", TAG_FORMAT_MORE,
                           default=True):
                return False
            continue
        setup.RESUME_TEMPLATE.write_text(text)
        setup.RESUME.write_text(setup._strip_tag(text))
        docx_keep = CONFIG_DIR / "resume_template.docx"
        if src.suffix.lower() == ".docx" and src.resolve() != docx_keep.resolve():
            docx_keep.unlink(missing_ok=True)
            shutil.copy(src, docx_keep)
        (CONFIG_DIR / "approvedskills.txt").touch(exist_ok=True)
        print(f"  {TAG} recognized. saved.")
        return True


def _upload_plain(dest: Path, kind: str) -> bool:
    while True:
        src = _prompt_path(f"path to your {kind} resume (.txt or .docx)")
        if src is None:
            return False
        if src.suffix.lower() not in (".txt", ".md", ".docx", ""):
            print(f"  {src.suffix} won't work; use .txt or .docx")
            continue
        try:
            dest.write_text(setup._read_resume(src))
        except Exception:
            print("  couldn't read that file. is it a normal .docx or .txt?")
            continue
        return True


def _embed_preview() -> str:
    if not setup.RESUME.exists():
        return ""
    stripped = _clean_text(_strip_contact(setup.RESUME.read_text()))
    shown = stripped[:600] + (" ..." if len(stripped) > 600 else "")
    return ("\n  --- your resume AUTO-STRIPPED (what gets embedded today) ---\n  "
            + shown + "\n  "
            + "-" * 58 + "\n")


def edit_resume() -> None:
    print("\n  ==== resume " + "=" * 53)
    # ---- Q1: tagged or tagless ----
    saved = False
    while not saved:
        v = input("\n  use a <tag> resume? (y / n / !more): ").strip().lower()
        if v in ("!more", "more"):
            print(TAG_MORE)
        elif v in ("y", "yes"):
            if _upload_tagged():
                saved = True
            else:
                print("  cancelled; resume unchanged.")
                return
        elif v in ("n", "no"):
            if yn_more("run tagless? you lose the bot's per-job resume builder "
                       "(matching + scoring still work)", TAGLESS_MORE,
                       default=False):
                if _upload_plain(setup.RESUME, "tagless"):
                    print("  saved (tagless).")
                    saved = True
                else:
                    print("  cancelled; resume unchanged.")
                    return
            # else: loop back to the tag question
        else:
            print("  please answer y, n, or !more")
    # ---- Q2: embedding/matching resume ----
    while True:
        v = input("\n  matching resume: (a)uto-strip [default], (c)ustom "
                  "upload, or !more: ").strip().lower()
        if v in ("!more", "more"):
            print(EMBED_MORE + _embed_preview())
        elif v in ("a", "auto", "autostrip", "", "default"):
            if RESUME_EMBED.exists():
                RESUME_EMBED.unlink()
            print("  using auto-strip for matching. done.")
            return
        elif v in ("c", "custom", "s", "stripped"):
            if _upload_plain(RESUME_EMBED, "custom matching"):
                print("  saved. matching uses it; re-scores on next !scrape.")
            return
        else:
            print("  please answer a, c, or !more")


# ============================================================ menu ===========
def main() -> None:
    if not setup.PROFILE.exists():
        print("  no config/profile.yaml yet. run setup first:  python3 setup.py")
        sys.exit(1)
    setup._yn_taught = True            # not a first-timer; plain (y/n) prompts
    print("=" * 66)
    print("  automatch editor: change your config without redoing setup")
    print("=" * 66)
    while True:
        print("\n  what do you want to change?")
        print("   1) scoring metrics    (add / remove / edit / replace, incl. weights)")
        print("   2) scoring settings   (threshold, wildcard, judge model)")
        print("   3) job search         (titles, location, radius, filters)")
        print("   4) Discord login      (token / public key / user id)")
        print("   5) resume             (tagged resume + matching resume)")
        print("   6) quit")
        choice = input("\n  pick 1-6: ").strip().lower()
        if choice in ("1", "metrics"):
            edit_metrics.run_editor()
        elif choice in ("2", "scoring", "threshold", "wildcard", "model"):
            edit_scoring_settings()
        elif choice in ("3", "search"):
            edit_search()
        elif choice in ("4", "discord"):
            edit_discord()
        elif choice in ("5", "resume"):
            edit_resume()
        elif choice in ("6", "q", "quit", "exit", ""):
            break
        else:
            print("  pick a number 1-6.")
    print("\n  done.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  cancelled; no further changes written.")
        sys.exit(1)
