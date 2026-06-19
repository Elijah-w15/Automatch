#!/usr/bin/env python3
"""automatch setup wizard: interview-style first-time setup.

Builds config/profile.yaml (search + scoring rubric), gets the resume into
place, then checks dependencies. Pure stdlib on purpose: this runs BEFORE
anything is installed. Rerun anytime: python3 setup.py
"""
import sys

if sys.version_info < (3, 10):
    sys.exit("automatch needs Python 3.10+; you have "
             + sys.version.split()[0])

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

# Windows installs no python3.exe ("python3" there hits the Store stub)
PY = "python" if os.name == "nt" else "python3"

try:
    import readline  # noqa: F401; arrow-key editing + history in input()
except ImportError:
    pass


class GoBack(Exception):
    """User typed 'back'; redo the previous question."""

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "config" / "profile.yaml"
RESUME = HERE / "config" / "resume.txt"
RESUME_TEMPLATE = HERE / "config" / "resume_template.txt"
CONFIG = HERE / "config" / "config.yaml"
ENV = HERE / ".env"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
ANCHOR_STEPS = ("0.0", "0.2", "0.4", "0.6", "0.8", "1.0")
MARKER = "<tag>"
DONATE = ""        # e.g. "https://ko-fi.com/yourname"; shown after setup

# the local judge models the user picks from, lightest -> heaviest. heavier
# judges score more sharply but need more GPU/RAM; only the chosen one is
# ever downloaded. (name, tier, who-it's-for, approx download size)
JUDGE_TIERS = [
    ("llama3.1:8b",  "light",    "CPU-only / no GPU, or ~6GB VRAM",      "~4.9GB"),
    ("mistral-nemo", "balanced", "~8GB VRAM GPU (most gaming cards)",    "~7GB"),
    ("qwen2.5:14b",  "sharp",    "~12GB VRAM GPU (RTX 4070+)",           "~9GB"),
    ("qwen2.5:32b",  "max",      "24GB+ VRAM GPU (RTX 4090/3090, etc.)", "~20GB"),
]

# a ready-made metric offered during setup so users aren't forced to invent
# one cold. Anchors key on the qualification/seniority GAP so the judge can
# actually spread scores (a vague ladder makes it park everything mid-range).
BUILTIN_INTERVIEW_ODDS = (
    "interview_odds",
    "How likely am I to land an interview given my experience level?",
    {"0.0": "my background barely matches this kind of role; I am not really qualified for it",
     "0.2": "only a little of what they want overlaps my skills, and they also want more experience or seniority than I have",
     "0.4": "I match some of the skills but there is a clear experience or seniority gap; a longer-shot stretch",
     "0.6": "they want more experience than I have, but I match most of the skills they list; a stretch worth applying to",
     "0.8": "I match most of the skills and my experience is about what they ask for",
     "1.0": "I match the skills and clearly meet or exceed the experience they ask for"})


def ask(prompt: str, default: str = "", example: str = "",
        space: bool = True, note: str = "") -> str:
    """Standard block: the question on top, 'user input:' directly under it."""
    if example:                            # custom example text wins
        tag = f" (example: {example})"
    elif default:
        tag = f" (example: {default})"     # enter accepts the example as-is
    else:
        tag = ""
    if note:
        tag += f" ({note})"
    if prompt:
        text = f"  {prompt}{tag}\n    user input: "
    else:
        text = "    user input: "           # question was print()ed above
    val = input(text).strip() or default
    if val.lower() == "back":
        raise GoBack
    if space:
        print()                   # blank line: visually segment each block
    if val.startswith("[") and val.endswith("]"):   # user retyped an example
        val = val[1:-1].strip()
    return val


def ask_nb(*args, **kwargs) -> str:
    """ask() for places where there's nothing to go back to."""
    while True:
        try:
            return ask(*args, **kwargs)
        except GoBack:
            print("  (nothing to go back to here)")


def ask_secret(prompt: str) -> str:
    """Input echoed as ******: for tokens. Falls back to plain input
    when there's no real terminal (pipes/tests) or no termios (Windows)."""
    if not sys.stdin.isatty():
        return input(prompt).strip()
    try:
        import termios
        import tty
    except ImportError:
        import getpass
        return getpass.getpass(prompt).strip()
    sys.stdout.write(prompt)
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    chars = []
    shown = 0

    def redraw():
        nonlocal shown
        disp = "*" * max(0, len(chars) - 3) + "".join(chars[-3:])
        sys.stdout.write("\b" * shown + " " * shown + "\b" * shown + disp)
        shown = len(disp)
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        while True:
            c = sys.stdin.read(1)
            if c in ("\r", "\n"):
                break
            if c == "\x03":                  # ctrl-c
                raise KeyboardInterrupt
            if c == "\x7f":                  # backspace
                if chars:
                    chars.pop()
                    redraw()
            elif c >= " ":
                chars.append(c)
                redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print()
    return "".join(chars).strip()


_yn_taught = False    # first y/n prompt teaches "--> enter"; the rest show "(y/n)"


def ask_yn(prompt: str, default: str = "y") -> bool:
    global _yn_taught
    hint = "y/n --> enter" if not _yn_taught else "y/n"
    _yn_taught = True
    while True:
        v = (input(f"  {prompt} ({hint})\n    user input: ")
             .strip() or default).lower()
        if v in ("y", "yes", "n", "no"):
            print()
            return v in ("y", "yes")
        print("  please answer y or n")


def ask_int(prompt: str, default: int, note: str = "", example: str = "") -> int:
    while True:
        v = ask(prompt, str(default), example=example, note=note)
        m = re.search(r"-?\d[\d,]*", v)      # pull the number out of "25 miles"
        if m:
            try:
                return int(m.group().replace(",", ""))
            except ValueError:
                pass
        print("  enter just a number (example: 25)")


PLAIN_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.;&/()'+_-]*$")  # ; is safe unquoted


def yv(s: str) -> str:
    """Human-legible YAML value: plain text when safe, quoted only when the
    YAML parser would choke on it."""
    s = str(s).strip()
    if PLAIN_OK.match(s) and s.lower() not in ("yes", "no", "true", "false",
                                               "null", "on", "off"):
        return s
    return json.dumps(s, ensure_ascii=False)


def _listify(raw: str) -> list:
    """'a, b, and "c d"' -> ['a', 'b', 'c d']. People write lists like
    humans; surrounding quotes are theirs to type but ours to remove."""
    out = []
    for x in raw.split(","):
        x = re.sub(r"^and\s+", "", x.strip(), flags=re.I).strip("'\"").strip()
        if x:
            out.append(x)
    return out


# ---------------------------------------------------------------- search ----
def step_search() -> dict:
    print("==== [3] your job search " + "=" * 39)
    print()
    print('  Type "back" at any question to redo the previous one.')
    print()

    def q_terms():
        while True:
            terms = _listify(ask("job titles to hunt for, comma separated",
                                 example="Financial Analyst, Data Analyst"))
            if terms:
                return terms
            print("  need at least one title")

    def q_level():
        while True:
            print("  select the level of job you are looking for")
            print("  (default shows all jobs skewed to mid level, entry is "
                  "entry level only, senior is senior level only)")
            print()
            print("  options: entry, senior, or default")
            level = ask("", "default").lower().replace("only", "").strip()
            if level in ("default", "entry", "senior"):
                return level
            print("  three options: default, entry, senior")

    def q_salary():
        while True:
            sal = ask("yearly salary floor, blank = no filter",
                      example="60000 or $60k")
            if not sal:
                return ""
            sl = sal.lower().replace(",", "").replace("$", "")
            try:    # store legibly as a plain yearly number
                return str(int(float(sl.rstrip("k"))
                               * (1000 if sl.endswith("k") else 1)))
            except ValueError:
                print("  a number like 60000 or $60k; or leave blank")

    fields = [
        ("terms", q_terms),
        ("loc", lambda: ask("the city, state to search around", "Philadelphia, PA")),
        ("radius", lambda: ask_int(
            "search radius in miles (just the number)", 25)),
        ("age", lambda: ask_int(
            "ignore postings older than how many hours? (just the number)", 24)),
        ("cap", lambda: ask_int(
            "stop each run after scraping how many new jobs", 128,
            example="128 or until no more jobs listed",
            note="use less jobs if low compute")),
        ("level", q_level),
        ("salary", q_salary),
        ("excludes", lambda: _listify(ask(
            "words to BLOCK: if any shows up in a job's posting, that job is\n"
            "  dropped from your list (companies, job types, tasks, anything).\n"
            "  blank = none",
            example="amazon, construction, contract",
            space=False))),    # section ends: blank, not divider
    ]
    s, i = {}, 0
    while i < len(fields):
        key, fn = fields[i]
        try:
            s[key] = fn()
            i += 1
        except GoBack:
            if i:
                i -= 1
            else:
                print("  (this is the first question)")
    return s


# --------------------------------------------------------------- metrics ----
def collect_metric(n: int, taken: set) -> tuple[str, str, dict]:
    print(f"\n  ---- metric #{n} " + "-" * 47)
    print()

    def q_name():
        while True:
            nm = ask("name this metric", example="interview_odds")
            nm = re.sub(r"[^a-z0-9_]", "", nm.lower().replace(" ", "_"))
            if nm in ("level", "keyword_candidates"):
                print(f"  '{nm}' is reserved; pick another name")
            elif nm in taken:
                print(f"  '{nm}' is already one of your metrics; pick "
                      "another name")
            elif nm:
                return nm
            else:
                print("  one or two words (example: free_time)")

    name = question = None
    part = 0
    while part < 2:
        try:
            if part == 0:
                name = q_name()
            else:
                print(f"  the question the AI answers about every job to "
                      f"score '{name}'")
                print("  (example: How likely am I to land an interview "
                      "given my experience level?)")
                question = ask("")
                if not question:
                    print("  give a short question")
                    continue
            part += 1
        except GoBack:
            if part:
                part -= 1
            else:
                print("  (this is the first question of the metric)")

    print(f"  describe what each score looks like for '{name}'. for best "
          "results use short,")
    print("  concrete phrases.")
    print()
    print("  levels:")
    # the list builds line by line as the user types each one (the example
    # rubric is shown up in the rubric intro, so no per-score hints here)
    anchors = {}
    i = 0
    while i < len(ANCHOR_STEPS):
        step = ANCHOR_STEPS[i]
        try:
            while True:
                d = input(f"    {step}: ").strip()
                if d.lower() == "back":
                    raise GoBack
                if d:
                    break
                print("    (enter a short phrase)")
        except GoBack:
            if i == 0:
                print("    (this is the first score; nothing to undo)")
                continue
            i -= 1
            anchors.pop(ANCHOR_STEPS[i], None)
            continue
        anchors[step] = d
        i += 1
    print()
    return name, question, anchors


def collect_metrics() -> list:
    """Offer the ready-made interview_odds metric, then loop 'add your own'.
    Returns a list of (name, question, anchors) tuples. Shared by the setup
    rubric step and the standalone metric editor's 'replace all' action."""
    metrics = []
    # offer the ready-made interview_odds metric so nobody has to invent one
    # from a blank page
    print("\n  default example metric: 'interview_odds'")
    print()
    print(f"  question to the AI: {BUILTIN_INTERVIEW_ODDS[1]}")
    print()
    print("  levels:")
    for step, text in BUILTIN_INTERVIEW_ODDS[2].items():
        print(f"    {step}: {text}")
    if ask_yn("\n  add default 'interview_odds' as a metric?", "y"):
        metrics.append(BUILTIN_INTERVIEW_ODDS)
        print("  added.")
        print()
    added_own = 0
    while True:
        if metrics:
            if added_own == 0:    # first time: invite + remind they can DIY
                q = ("\n  would you like to add any of your own custom "
                     "metrics? (commute, adhd friendly, interesting ...)")
            else:                 # already added one: offer one more, repeatable
                q = "\n  add another metric?"
            if not ask_yn(q, "n"):
                break
        metrics.append(collect_metric(len(metrics) + 1,
                                      {m[0] for m in metrics}))
        added_own += 1
    return metrics


def step_metrics() -> tuple:
    print("\n==== [4] your scoring rubric " + "=" * 35)
    print()
    print("  Each job gets scored on the things you care about. A 'metric' is")
    print("  one thing, scored 0.0 to 1.0, where you write a short example of")
    print("  what each score looks like (your 0.0, your 0.6, your 1.0...).")
    print()
    print("  For best results, keep each metric to a single idea so the AI can")
    print("  score it cleanly. e.g. make 'commute distance' one metric and")
    print("  'free time' a separate one.")
    print()
    print("  the bot averages your metrics and only jobs above your")
    print("  threshold make the list.")
    metrics = collect_metrics()
    while True:
        th = ask_nb("minimum average score a job needs to make the list, "
                    "0 to 1\n  blank = show everything, ranked",
                    example="0.6")
        if not th:
            th = "0"
            break
        try:
            if 0 <= float(th) <= 1:
                break
        except ValueError:
            pass
        print("  a number between 0 and 1 (example: 0.6); or blank")
    print("  add a WILD CARD? one extra pick per run (shown as W): the job")
    print("  that best matches a description YOU define, even if it scored")
    print("  below your top matches")
    wc = ""
    if ask_yn("  include a wild card?", "n"):
        print("  1. default: a job with a high match to my skills but")
        print("     asking for more experience than I have")
        print("  2. startup: a fast-paced startup wanting several of my")
        print("     skills but more experience")
        print("  3. write your own")
        pick = ask_nb("pick 1, 2 or 3", example="1")
        if pick == "2":
            wc = ("a fast-paced startup looking for someone with several "
                  "of my skills but with more experience")
        elif pick == "3":
            wc = ask_nb("describe the wild-card job in one sentence",
                        example="a remote-first company hiring my exact "
                                "skills in a different industry")
        else:
            wc = ("a job with a high similarity match to my skills but "
                  "asking for more experience than I have")
    return metrics, th, wc


# -------------------------------------------------------------- spellfix ----
def _fix_one(model: str, text: str, rule: str, context: str) -> str:
    """One focused correction call per string; the model is far better at
    'fix this phrase' than at fixing 17 strings inside one big JSON."""
    prompt = ("You fix typos for a job-search app. Correct the spelling of "
              "this text, inferring the intended English words even from "
              f"heavy typos. {context} Do not rephrase or change the "
              f"meaning. {rule} "
              'Reply with ONLY this JSON: {"fixed": "<corrected text>"}\n'
              f'Text: "{text}"')
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                         "format": "json", "options": {"temperature": 0}}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        fixed = str(json.loads(json.load(r).get("response", "{}"))
                    .get("fixed", "")).strip()
    return fixed or text


def spellfix(s: dict, metrics: list, quiet: bool = False,
             report: list | None = None) -> tuple[dict, list]:
    """Fix spelling per-string with the local model when ollama is already up
    ('deloite' -> 'Deloitte'; exclusions match by exact substring, so typos
    there genuinely break filtering). No-op when ollama isn't installed yet.

    quiet=True suppresses prints (it runs in a background thread while the
    user does later steps) and appends its result lines to `report` for the
    caller to print after the thread is joined."""
    model = judge_model()
    changes = []

    def say(msg: str) -> None:
        if quiet:
            if report is not None:
                report.append(msg)
        else:
            print(msg)

    def fix(text, rule, context):
        if len(text.strip()) < 4:    # too short to spellcheck; the model
            return text              # invents content for fragments
        try:
            new = _fix_one(model, text, rule, context)
        except (OSError, ValueError):
            return text
        if new and new != text:
            changes.append(f"{text} -> {new}")
            return new
        return text

    have = _ollama_models()    # reachable AND the judge model is pulled?
    if have is None or not _model_present(model, have):
        say("\n  (spell-check skipped: ollama or the judge model isn't "
            "installed yet)")
        return s, metrics
    if not quiet:
        print("\n  spell-checking your answers with the local AI "
              "(first time can take a minute)...")
    s["loc"] = fix(s["loc"], "Reply in the form 'City, ST' with the 2-letter "
                             "state abbreviation (so 'philadelphia' becomes "
                             "'Philadelphia, PA').",
                   "It is the US city the user wants to find jobs in.")
    s["terms"] = [fix(t, "Use Title Case. Do not add or drop words.",
                      "It is a job title to search for.") for t in s["terms"]]
    s["excludes"] = [fix(x, "Use its normal spelling and capitalization. Do "
                            "not add or drop words.",
                         "It is a company name or job-category word the user "
                         "wants to avoid.") for x in s["excludes"]]
    fixed_metrics = []
    for n, q, a in metrics:
        q = fix(q, "Sentence case, ending with a question mark. Do not add "
                   "or drop words.",
                "It is a question the user wants answered about every job "
                "posting.")
        a = {step: fix(text, "Keep it lowercase except proper nouns. Drop "
                             "stray typo fragments that are not real words.",
                       f'It is an example answer to the question "{q}" '
                       "about a job.")
             for step, text in a.items()}
        fixed_metrics.append((n, q, a))
    if changes:
        say("  spell-check fixed:")
        for c in changes:
            say(f"    {c}")
    else:
        say("  spell-check: no fixes needed")
    return s, fixed_metrics


# --------------------------------------------------------------- profile ----
def write_profile(s: dict, metrics: list) -> None:
    out = ["# ====== YOUR JOB SEARCH (written by setup.py) ======",
           f"# Edit by hand anytime, or rerun:  {PY} setup.py", "",
           "search_terms:"]
    out += [f"  - {yv(t)}" for t in s["terms"]]
    out += ["", f"location: {yv(s['loc'])}",
            f"radius_miles: {s['radius']}",
            f"max_listing_age_hours: {s['age']}    # ignore postings older than this",
            f"max_jobs: {s['cap']}                # stop each run after this many NEW jobs", "",
            "# level: three options:",
            "#   default  rewards entry/mid jobs, sinks executive ones",
            "#   entry    show ONLY entry-level jobs",
            "#   senior   show ONLY senior jobs",
            f"level: {s['level']}", "",
            "# Yearly pay floor: drops jobs that LIST pay below this; postings",
            "# with no listed pay are kept. Blank = no salary filter.",
            f"salary_min: {s['salary']}", "",
            "# Drop a job if any of these words appear in its title, company",
            "# name, or company industry (job classes or specific companies).",
            "exclude:" if s["excludes"] else "exclude: []"]
    out += [f"  - {yv(x)}" for x in s["excludes"]]
    out += ["", "# Minimum weighted-average score a job needs to appear in",
            "# matches.html. 0 = show everything, ranked. Change it and the",
            "# next run re-ranks instantly (no AI calls).",
            f"threshold: {s.get('threshold') or 0}"]
    out += ["", "# WILD CARD: one extra pick per run (W slot): the job best",
            "# matching this description, even if it scored below your top",
            "# matches. Blank = off.",
            f"wildcard: {yv(s.get('wildcard')) if s.get('wildcard') else ''}"]
    out += ["", "# ====== YOUR SCORING METRICS ======",
            "# question = what the AI answers; anchors = YOUR examples of",
            "# each score. weight: edit the number to make a metric count",
            "# more or less (1 = normal, 0.5 = half, 2 = double).",
            "vectors:"]
    for name, question, anchors in metrics:
        out += [f"  {name}:",
                "    weight: 1",
                f"    question: {yv(question)}",
                "    anchors:"]
        out += [f"      {step}: {yv(text)}" for step, text in anchors.items()]
        out += [""]
    PROFILE.write_text("\n".join(out))
    print("\n  profile complete.")


# ---------------------------------------------------------------- resume ----
def step_resume(advanced: bool = False) -> None:
    print("\n==== [5] your resume " + "=" * 43)
    print()
    if advanced:
        _resume_template_upload()
    else:
        _resume_upload()


def _read_resume(src: Path) -> str:
    """Plain text from .txt/.md; or .docx, converted with stdlib only
    (a .docx is a zip; the text lives in word/document.xml)."""
    if src.suffix.lower() == ".docx":
        import html as html_
        import zipfile
        with zipfile.ZipFile(src) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = (xml.replace("</w:p>", "\n").replace("<w:tab/>", "\t")
                  .replace("<w:br/>", "\n"))
        return html_.unescape(re.sub(r"<[^>]+>", "", xml))
    return src.read_text()


def _strip_tag(text: str) -> str:
    return (text.replace(f", {MARKER}", "").replace(f",{MARKER}", "")
                .replace(f"{MARKER}, ", "").replace(f"{MARKER},", "")
                .replace(MARKER, ""))


def _resume_template_upload() -> None:
    """ADVANCED: one upload (the <tag>'d resume). The plain scoring copy
    (config/resume.txt) is auto-made from it with the tag stripped."""
    print("  HOW TO MODIFY YOUR RESUME TO WORK WITH THE DISCORD BOT")
    print()
    print(f"  in your resume, add a {MARKER} in the skills section")
    print(f"  the discord bot will replace {MARKER} with each job's keywords")
    print(f"  example:  skills: microsoft suite, {MARKER}, python, etc.")
    print("  (a working example resume: docs/resume_tag.example.docx)")
    print("  save it as .docx (Word/Google Docs) or .txt; both work")
    print()
    print("  once you add the tag, tell me where the file is:")
    while True:
        p = ask_nb("path to your tagged resume .txt\n  I'll copy it into place",
                   example="/home/you/Documents/resume.txt")
        src = Path(p.strip("'\"")).expanduser() if p else RESUME_TEMPLATE
        if not src.exists():
            if not p and ask_yn("  config/resume_template.txt still missing\n  "
                                "continue anyway?", "n"):
                print("  ok. the bot refuses to start until it exists.")
                return
            if p:
                print(f"  can't find {src}")
            continue
        if src.suffix.lower() not in (".txt", ".md", ".docx", ""):
            print(f"  {src.suffix} won't work; use .txt or .docx")
            continue
        try:
            text = _read_resume(src)
        except Exception:
            print("  couldn't read that file. is it a normal .docx or .txt?")
            continue
        if MARKER not in text:
            print(f"  no {MARKER} found in that file; add it to the skills "
                  "line like this:")
            print(f"      skills: microsoft suite, {MARKER}, python, etc.")
            print("  (a working example: docs/resume_tag.example.docx)")
            print("  save, then give me the path again")
            continue
        RESUME_TEMPLATE.write_text(text)
        RESUME.write_text(_strip_tag(text))
        # keep the ORIGINAL docx too: per-job resumes can then be real
        # .docx files with the user's formatting intact. Only touched on a
        # NEW upload; keeping the existing template must not delete it.
        docx_keep = HERE / "config" / "resume_template.docx"
        if src.suffix.lower() == ".docx":
            if src.resolve() != docx_keep.resolve():
                docx_keep.unlink(missing_ok=True)
                shutil.copy(src, docx_keep)
        elif src.resolve() != RESUME_TEMPLATE.resolve():
            docx_keep.unlink(missing_ok=True)   # new txt replaces old docx
        (HERE / "config" / "approvedskills.txt").touch(exist_ok=True)
        print()
        print(f"  {MARKER} recognized")
        return


def _resume_upload() -> None:
    if RESUME.exists():
        print(f"  found config/resume.txt ({RESUME.stat().st_size} bytes)")
        if not ask_yn("  replace it with a different resume?", "n"):
            return
    print("  your resume can be .docx (Word/Google Docs) or plain .txt")
    while True:
        p = ask_nb("path to your resume .txt\n  I'll copy it into place",
                   example="/home/you/Documents/resume.txt")
        if not p:
            if RESUME.exists():
                print("  found it. good.")
                return
            if ask_yn("  config/resume.txt still missing\n  continue anyway?", "n"):
                print("  ok. the app will remind you before its first run.")
                return
            continue
        src = Path(p.strip("'\"")).expanduser()
        if not src.exists():
            print(f"  can't find {src}")
            continue
        if src.suffix.lower() not in (".txt", ".md", ".docx", ""):
            print(f"  {src.suffix} won't work; use .txt or .docx")
            continue
        try:
            RESUME.write_text(_read_resume(src))
        except Exception:
            print("  couldn't read that file. is it a normal .docx or .txt?")
            continue
        conv = " (converted from .docx)" if src.suffix.lower() == ".docx" else ""
        print(f"  copied to {RESUME}{conv}. good.")
        return


# ------------------------------------------------------------------ deps ----
def judge_model() -> str:
    try:
        for line in CONFIG.read_text().splitlines():
            if line.strip().startswith("judge:"):
                return line.split(":", 1)[1].split("#")[0].strip().strip("\"'")
    except OSError:
        pass
    return "mistral-nemo"


def _set_judge(model: str) -> None:
    """Persist the chosen judge model to config.yaml's `judge:` line so
    judge_model() and the scorer pick it up. Leaves the rest untouched."""
    try:
        lines = CONFIG.read_text().splitlines()
    except OSError:
        return
    for i, line in enumerate(lines):
        if line.strip().startswith("judge:"):
            indent = line[:len(line) - len(line.lstrip())]
            lines[i] = f'{indent}judge: "{model}"'
            CONFIG.write_text("\n".join(lines) + "\n")
            return


def _gpu_info() -> tuple[str, int] | None:
    """(name, total VRAM in MB) for the largest NVIDIA GPU, or None when
    there's no nvidia-smi (CPU-only, or a non-NVIDIA / Apple GPU we can't
    size)."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    best = None
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[-1].isdigit():
            mb = int(parts[-1])
            if best is None or mb > best[1]:
                best = (",".join(parts[:-1]).strip(), mb)
    return best


def _recommended_tier() -> int:
    """Index into JUDGE_TIERS suggested by detected VRAM. Undetectable
    hardware falls back to 'balanced' (nemo): a safe middle the user can
    override either way."""
    info = _gpu_info()
    if info is None:
        return 1
    vram = info[1]
    if vram >= 22000:
        return 3
    if vram >= 11000:
        return 2
    if vram >= 6000:
        return 1
    return 0


def step_model(have: list) -> None:
    """Pick the local judge model (the AI that scores every job), sized to
    the user's hardware. Writes the choice to config.yaml; the pull step
    then downloads ONLY that model (plus the embedder), never all four."""
    info = _gpu_info()
    rec = _recommended_tier()
    print("\n  ---- scoring model " + "-" * 45)
    print("  the local AI that judges every job against your rubric. heavier")
    print("  = sharper, more decisive scores, but needs more GPU/RAM and runs")
    print("  slower per job. pick one; change it any time with !model 1-4 on")
    print("  Discord, or by editing config.yaml.")
    print()
    if info:
        print(f"  Detected: {info[0]}, {round(info[1] / 1024)}GB VRAM")
    else:
        print("  No GPU detected")
    while True:
        print()
        for i, (model, tier, who, size) in enumerate(JUDGE_TIERS, 1):
            owned = "  [already downloaded]" if _model_present(model, have) else ""
            star = "  <- recommended for detected hardware" if i - 1 == rec else ""
            print(f"   {i}) {model:<13} {tier:<9} {who} ({size}){owned}{star}")
        print()
        raw = input(f"  pick 1-{len(JUDGE_TIERS)} (enter = {rec + 1}): ").strip()
        idx = (int(raw) - 1 if raw.isdigit()
               and 1 <= int(raw) <= len(JUDGE_TIERS) else rec)
        chosen = JUDGE_TIERS[idx][0]
        if ask_yn(f"  confirm chosen model: {chosen}?", "y"):
            break
    _set_judge(chosen)
    print(f"  {chosen} set. change it anytime with !model 1-4 on Discord, "
          "or in config.yaml")


OVERRIDE = Path("/etc/systemd/system/ollama.service.d/override.conf")


def offer(cmd: str, why: str, admin: bool = True) -> bool:
    """Show the exact command, offer to run it right here. Returns True if
    it ran and succeeded; the command stays on screen for copy-paste if not."""
    print(f"\n  {why}")
    priv = " with administrative privileges" if admin else ""
    print(f"  run this command{priv} to continue:")
    print(f"\n      {cmd}\n")
    if ask_yn(f"  run {'the permission command' if admin else 'that command'} "
              "now?", "y"):
        ok = subprocess.run(cmd, shell=True).returncode == 0
        print("  done." if ok else
              f"  that command failed; fix it, then rerun: {PY} setup.py")
        return ok
    return False


def _model_present(model: str, have: list) -> bool:
    """Exact match when a tag like :3b is given; base match when untagged."""
    return any(h == model or (":" not in model and h.split(":")[0] == model)
               for h in have)


def _ollama_models() -> list | None:
    """Installed model names (with tags) if ollama is reachable, else None."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except OSError:
        return None


def _human(n: float) -> str:
    """Bytes as a short human size, e.g. 1.9GB."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _draw_bar(label: str, done: int, total: int, width: int = 26) -> None:
    """Redraw an in-place percent bar on the current line. With no byte
    total (manifest / verify / write phases) it shows the phase, no number."""
    if total > 0:
        frac = max(0.0, min(1.0, done / total))
        filled = int(frac * width)
        bar = "#" * filled + "." * (width - filled)
        line = (f"  {label:<20.20} [{bar}] {frac * 100:5.1f}%  "
                f"{_human(done)}/{_human(total)}")
    else:
        line = f"  {label:<20.20} [{'.' * width}]    ...   "
    # trailing pad clears leftovers when a line gets shorter
    sys.stdout.write("\r" + line + "        ")
    sys.stdout.flush()


def _pull_model(model: str) -> bool:
    """Download an ollama model through the streaming /api/pull endpoint,
    drawing our OWN percent-done bar from the byte counts it streams. The
    ollama CLI's spinner can hide the percentage in some terminals; this
    always shows how far along (and how big) the download is. Sums every
    layer so the bar reflects the whole model, not just the current layer."""
    print(f"\n  downloading {model} ...")
    req = urllib.request.Request(
        f"{OLLAMA}/api/pull",
        data=json.dumps({"model": model, "stream": True}).encode(),
        headers={"Content-Type": "application/json"})
    layers: dict[str, tuple[int, int]] = {}   # digest -> (completed, total)
    try:
        with urllib.request.urlopen(req, timeout=None) as r:
            for raw in r:
                raw = raw.strip()
                if not raw:
                    continue
                msg = json.loads(raw)
                if msg.get("error"):
                    sys.stdout.write("\n")
                    print(f"  [--] download failed: {msg['error']}")
                    return False
                status = msg.get("status", "")
                digest, total = msg.get("digest"), msg.get("total")
                if digest and total:
                    layers[digest] = (msg.get("completed", 0), total)
                    done = sum(c for c, _ in layers.values())
                    tot = sum(t for _, t in layers.values())
                    _draw_bar("downloading", done, tot)
                else:
                    _draw_bar(status or "working", 0, 0)
        sys.stdout.write("\n")
        print(f"  [ok] {model} downloaded successfully")
        return True
    except OSError as e:
        sys.stdout.write("\n")
        print(f"  [--] download failed: {e}")
        print(f"       you can retry by hand:  ollama pull {model}")
        return False


def _run_with_bar(label: str, cmd: list, cwd: str | None = None) -> int:
    """Run a command that gives no parseable percentage (e.g. an image
    build) while animating an indeterminate progress bar, so the step never
    looks frozen. Returns the process return code; output stays captured."""
    result: dict = {}

    def _go():
        result["proc"] = subprocess.run(cmd, cwd=cwd,
                                         capture_output=True, text=True)

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    width, pos, span = 26, 0, 2 * (26 - 4)
    while t.is_alive():
        block = pos % span
        if block > width - 4:        # bounce the lit block back across the bar
            block = span - block
        bar = ["."] * width
        for i in range(4):
            bar[block + i] = "#"
        sys.stdout.write(f"\r  {label:<18.18} [{''.join(bar)}]   working ...   ")
        sys.stdout.flush()
        pos += 1
        time.sleep(0.15)
    t.join()
    sys.stdout.write("\r" + " " * (width + 44) + "\r")   # clear the line
    sys.stdout.flush()
    return result["proc"].returncode if result.get("proc") else 1


def _reboot_pending() -> bool:
    """Windows: True if a reboot is queued (e.g. the Docker install just enabled
    the WSL2 / Virtual Machine Platform features). Checks the standard pending-
    reboot markers; returns False on any error or non-Windows, so we never block
    setup on a bad read (Docker Desktop also prompts on first launch if needed)."""
    checks = [
        ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion"
         r"\Component Based Servicing\RebootPending"],
        ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion"
         r"\WindowsUpdate\Auto Update\RebootRequired"],
        ["reg", "query", r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager",
         "/v", "PendingFileRenameOperations"],
    ]
    for cmd in checks:
        try:
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                return True
        except OSError:
            return False        # no `reg` (non-Windows): not our concern here
    return False


def _offer_restart() -> None:
    """Windows: Docker's WSL2 setup needs a reboot. Ask whether to reboot now
    (shutdown /r, cancellable with `shutdown /a`) or let the user do it. Either
    way setup STOPS here -- it can't continue until WSL2 is active. The next
    launch of WINDOWS_START_HERE.cmd resumes (everything is saved)."""
    print()
    if ask_yn("  reboot now to finish the install?", "y"):
        subprocess.run(["shutdown", "/r", "/t", "10"])
        print("\n  rebooting in 10 seconds (cancel with: shutdown /a). when "
              "you're back,")
        print("  double-click WINDOWS_START_HERE.cmd again to finish. your "
              "progress is saved.")
    else:
        print("\n  ok. reboot your PC yourself, then double-click "
              "WINDOWS_START_HERE.cmd")
        print("  again to finish. your progress is saved.")
        print("  (setup stops here: it can't continue until WSL2 is active "
              "after the reboot.)")
    # exit 2 (not 0): start.py then prints "finish later" instead of firing a
    # doomed `docker compose run` (docker isn't usable until the reboot). the
    # relaunch of WINDOWS_START_HERE.cmd after the reboot resumes.
    sys.exit(2)


def step_deps(advanced: bool = False) -> bool:
    print("==== [2] dependencies " + "=" * 42)
    print()
    linux = sys.platform.startswith("linux")
    if linux and not (shutil.which("apt") or shutil.which("apt-get")):
        linux = False        # non-Debian distro: show links, not apt commands
        print("  (no apt here; install docker + ollama with your distro's")
        print("   package manager: docs.docker.com/engine/install, ollama.com)")
    # Windows can auto-install via winget (curl ships with Win10+; not needed)
    windows = sys.platform.startswith("win")
    has_winget = windows and bool(shutil.which("winget"))
    ready = True

    docker_bin = shutil.which("docker")
    info = subprocess.run(["docker", "info"], capture_output=True,
                          text=True) if docker_bin else None
    daemon = info is not None and info.returncode == 0
    if daemon:
        print(f'  [ok] docker detected at "{docker_bin}"')
        if subprocess.run(["docker", "compose", "version"],
                          capture_output=True).returncode == 0:
            print("  [ok] docker compose available")
        else:
            ready = False
            if linux:
                offer("sudo apt install -y docker-compose-v2",
                      "[--] the docker compose plugin is missing.")
            else:
                print("  [--] 'docker compose' missing; update Docker Desktop")
    elif docker_bin and info and "permission denied" in info.stderr.lower():
        ready = False
        if offer("sudo usermod -aG docker $USER",
                 "[--] docker is installed but your user isn't in the "
                 "docker group."):
            print("  IMPORTANT: log out of your computer and back in so the")
            print("  docker group takes effect, then open LINUX_START_HERE.sh "
                  "again.")
    elif docker_bin and linux:
        ready = False
        offer("sudo systemctl enable --now docker",
              "[--] docker is installed but the daemon isn't running.")
    else:
        ready = False
        if linux:
            if offer("sudo apt install -y docker.io docker-compose-v2 "
                     "&& sudo usermod -aG docker $USER",
                     "[--] docker is missing."):
                print("  IMPORTANT: log out of your computer and back in so the")
                print("  docker group takes effect, then open "
                      "LINUX_START_HERE.sh again.")
        elif docker_bin:
            print("  [--] docker is installed but not running; open the")
            print("       Docker Desktop app, wait for it to start, rerun")
        elif has_winget:
            if offer("winget install -e --id Docker.DockerDesktop "
                     "--accept-package-agreements --accept-source-agreements",
                     "[--] docker is missing.", admin=False):
                print("  docker installed successfully. (Docker Desktop makes")
                print("  you sign in the first time: a free Docker account or")
                print("  'Continue with Google' works.)")
                if _reboot_pending():
                    print("  a reboot is required to finish enabling WSL2.")
                    _offer_restart()         # offers reboot, then stops setup
                else:
                    print("  no reboot needed: open Docker Desktop, sign in, and")
                    print("  setup keeps going (rerun WINDOWS_START_HERE.cmd "
                          "anytime).")
        else:
            print("  [--] docker is missing; install Docker Desktop:")
            print("       https://docs.docker.com/desktop/")
            print("       (it makes you sign in the first time: a free")
            print("        Docker account or 'Continue with Google' works)")

    have = _ollama_models()
    ollama_was_present = have is not None
    if have is None:
        if linux:
            if not shutil.which("curl"):
                offer("sudo apt install -y curl",
                      "[--] curl is missing (needed to download ollama).")
            if shutil.which("curl") and offer(
                    "curl -fsSL https://ollama.com/install.sh | sh",
                    "[--] ollama is not installed (or not running)."):
                time.sleep(3)
                have = _ollama_models()
        elif has_winget:
            if offer("winget install -e --id Ollama.Ollama "
                     "--accept-package-agreements --accept-source-agreements",
                     "[--] ollama is not installed.", admin=False):
                time.sleep(3)
                have = _ollama_models()
            else:
                print("  or download it yourself: https://ollama.com/download")
        else:
            print("  [--] ollama is not installed; download it:")
            print("       https://ollama.com/download")

    # advanced mode runs the bot inside a discord.py-equipped image; build it
    # here (BEFORE the model blobs) so it doesn't stall on first launch. the
    # Discord ACCOUNT was already confirmed back in step_choose.
    if advanced and daemon:
        # auto-build (no gate): the user already opted into advanced + has an
        # account, so just do it and report status like the other deps
        print("  [ok] discord is installing")
        rc = _run_with_bar("discord", ["docker", "compose", "--profile",
                                       "advanced", "build", "bot"],
                           cwd=str(HERE))
        if rc == 0:
            print("  [ok] discord installed successfully")
        else:
            ready = False
            print("  [--] bot build failed; it'll retry on first run")

    if have is None:
        ready = False
        judge = judge_model()
        print(f"  once ollama is up:  ollama pull nomic-embed-text "
              f"&& ollama pull {judge}")
        print("  (no GPU? see the 'No GPU?' note in docs/manual-setup.md)")
    else:
        if ollama_was_present:
            ob = shutil.which("ollama")
            print(f'  [ok] ollama detected at "{ob}"' if ob
                  else "  [ok] ollama detected (running)")
        else:
            print("  [ok] ollama installed successfully")
        step_model(have)            # AI-blob section: choose the judge, then
        judge = judge_model()       # download ONLY it + the embedder below
        # no gate here: confirming the model above IS the consent. just pull
        # the embedder + the chosen judge, nothing else
        for model in ("nomic-embed-text", judge):
            # exact match when a tag like :3b is specified; base match
            # only for untagged names (resolves to :latest)
            if _model_present(model, have):
                print(f"  [ok] {model} already downloaded")
            elif not _pull_model(model):
                ready = False
        if linux:
            if OVERRIDE.exists() and "0.0.0.0" in OVERRIDE.read_text():
                print("  [ok] Docker-to-ollama networking fix already applied")
            else:
                fix = ("sudo mkdir -p /etc/systemd/system/ollama.service.d && "
                       "printf '[Service]\\nEnvironment=\"OLLAMA_HOST=0.0.0.0\"\\n'"
                       " | sudo tee /etc/systemd/system/ollama.service.d/override.conf"
                       " && sudo systemctl daemon-reload && sudo systemctl restart ollama")
                if not offer(fix, "[--] one-time Linux fix so Docker containers "
                                  "can reach ollama."):
                    ready = False
                    print("  (not using systemd? start ollama with: "
                          "OLLAMA_HOST=0.0.0.0 ollama serve)")
    return ready


# -------------------------------------------------------------- advanced ----
def _check_token(tok: str) -> str | None:
    """Bot username if the token authenticates, else None."""
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {tok}", "User-Agent": "automatch-setup"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r).get("username")
    except OSError:
        return None


def _invite_url(token: str) -> str | None:
    """One-click invite URL built from the token's application id, with
    exactly the perms the bot uses: Send Messages (0x800) + Attach Files
    (0x8000) + Read Message History (0x10000) = 100352. Saves the user from
    hand-navigating the dev portal's OAuth2 URL Generator."""
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}", "User-Agent": "automatch-setup"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            app_id = json.load(r).get("id")
    except OSError:
        return None
    if not app_id:
        return None
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            "&scope=bot&permissions=100352")


def _install_cli() -> bool:
    """Install a global `automatch` command that starts the bot from
    anywhere. Generated per-OS. Returns True when it's ready to use."""
    try:
        if os.name == "nt":      # native Windows: a .bat on the default PATH
            bindir = (Path(os.environ.get("LOCALAPPDATA", ""))
                      / "Microsoft" / "WindowsApps")
            if not bindir.is_dir():
                return False
            (bindir / "automatch.bat").write_text(
                "@echo off\r\n"
                f'cd /d "{HERE}"\r\n'
                "docker compose --profile advanced run --build --rm bot\r\n")
            return True
        bindir = Path.home() / ".local" / "bin"   # linux / mac / WSL
        bindir.mkdir(parents=True, exist_ok=True)
        cli = bindir / "automatch"
        cli.write_text(
            "#!/bin/sh\n"
            f'cd "{HERE}" || exit 1\n'
            'exec env UID="$(id -u)" GID="$(id -g)" '
            "docker compose --profile advanced run --build --rm bot\n")
        cli.chmod(0o755)
        return str(bindir) in os.environ.get("PATH", "")
    except OSError:
        return False


def _hello_ping(token: str, uid: str, cid: str, start_hint: str) -> None:
    """One REST message so the user SEES the bot is alive and learns where
    the conversation happens; no discord.py needed on the host."""
    msg = ("I'm alive! this chat is where I'll talk to you.\n"
           f"start me by {start_hint},\n"
           "then DM me `!match` to start scraping!")
    hdr = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
           "User-Agent": "automatch-setup"}
    try:
        if not cid:
            req = urllib.request.Request(
                "https://discord.com/api/v10/users/@me/channels",
                data=json.dumps({"recipient_id": uid}).encode(), headers=hdr)
            with urllib.request.urlopen(req, timeout=8) as r:
                cid = json.load(r)["id"]
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{cid}/messages",
            data=json.dumps({"content": msg}).encode(), headers=hdr)
        urllib.request.urlopen(req, timeout=8)
        print("  the bot messaged you on Discord")
    except OSError:
        print("  couldn't message you yet; recheck steps 4 and 5 (shared")
        print("  server + DMs allowed); the bot will try again when started")


def _write_env(updates: dict) -> None:
    """Update .env keeping every non-DISCORD line that's already there."""
    lines = []
    if ENV.exists():
        lines = [l for l in ENV.read_text().splitlines()
                 if not l.startswith("DISCORD_")]
    lines += [f"{k}={v}" for k, v in updates.items() if v]
    ENV.write_text("\n".join(lines) + "\n")


def step_choose() -> bool:
    print("==== [1] setup type " + "=" * 44)
    print()
    print("  basic:    scrape jobs, score them your way, outputs a ranked")
    print("            matches.html")
    print()
    print("  advanced: also builds a custom resume for each job, sent to you")
    print("            by a Discord bot (more involved setup, more capable.)")
    print()
    if not ask_yn("use the advanced setup?\n ", "n"):
        return False
    # advanced REQUIRES a Discord account; a script can't create one, so just
    # make sure they have it (open the signup page) before going further
    print("  advanced needs a free Discord account.")
    print("  don't have one? make one here: https://discord.com/register")
    print()
    while not ask_yn("  do you have a Discord account?", "y"):
        print("  make one first (free, ~1 min): https://discord.com/register")
    return True


def step_discord() -> bool:
    """[6/6] ADVANCED ONLY: bot credentials walkthrough -> .env."""
    print("\n==== [6] discord bot " + "=" * 43)
    print()
    (HERE / "output" / "resumes").mkdir(parents=True, exist_ok=True)
    print("  the bot talks to you over Discord DMs; these exact settings")
    print("  make DMs work. one step at a time; paste what a step asks for,")
    print("  or just press enter to continue:")

    print()
    print("   1. create the bot: https://discord.com/developers/applications")
    print("      -> New Application (any name)")
    input("      --> enter ")

    print()
    print("   2. copy the PUBLIC KEY from your app's General Information")
    print("      page (https://discord.com/developers/applications ->")
    print("      click your app)")
    pub = ""
    while not pub:
        pub = input("      user input (paste the PUBLIC key, then enter for "
                    "more instructions): ").strip()

    print()
    print("   3. left menu 'Bot' page, two things:")
    print("      - turn ON 'MESSAGE CONTENT INTENT' under Privileged Gateway")
    print("        Intents (REQUIRED: the bot cannot start without it)")
    print("      - Reset Token -> copy it (the PRIVATE key; paste it only")
    print("        here, never share it)")
    token = ""
    while not token:
        token = ask_secret("      user input (paste the PRIVATE token, "
                           "then enter): ")
        if token and ask_yn("    show the token to double-check it?", "n"):
            print(f"      token: {token}")
        if token:
            who = _check_token(token)
            if who:
                print(f"      token works; bot is '{who}'")
            elif not ask_yn("    token didn't authenticate\n  save it anyway?",
                            "n"):
                token = ""

    print()
    print("   4. add the bot to a server so it can DM you. (it talks to you in")
    print("      DMs; a server is just how Discord links you and the bot.)")
    invite = _invite_url(token)
    if invite:
        print("      - no server yet? in Discord, click the + on the left ->")
        print("        'Create My Own' -> 'For me and my friends' (10 seconds)")
        print("      - then open THIS link to add your bot to that server:")
        print(f"        {invite}")
        print("      - pick your server -> Authorize")
    else:
        print("      couldn't auto-build the invite link (token/network?); in")
        print("      the dev portal: your app -> OAuth2 -> URL Generator ->")
        print("      scope 'bot' + perms Send Messages / Attach Files / Read")
        print("      Message History -> open the URL -> pick your server")
    input("      --> enter ")

    print()
    print("   5. allow the DMs: open https://discord.com/channels/@me")
    print("      -> gear icon (User Settings) -> Privacy & Safety ->")
    print("      'Allow direct messages from server members' must be ON")
    input("      --> enter ")

    print()
    print("   6. get your user id: open https://discord.com/channels/@me")
    print("      -> gear icon (User Settings) -> Advanced -> Developer Mode")
    print("      ON, then right-click your own name -> Copy User ID")
    uid = input("      user input (your USER ID; blank to use a channel "
                "instead): ").strip()
    cid = "" if uid else ask_nb("channel ID for the bot to post in",
                                example="112233445566778899")
    _write_env({"DISCORD_BOT_TOKEN": token, "DISCORD_PUBLIC_KEY": pub,
                "DISCORD_USER_ID": uid, "DISCORD_CHANNEL_ID": cid})
    cli_ok = _install_cli()
    hint = ("typing `automatch` in your terminal" if cli_ok else
            "running `docker compose --profile advanced run --rm bot` "
            "in the project folder")
    print()
    _hello_ping(token, uid, cid, hint)
    return cli_ok


def main() -> None:
    # user-owned BEFORE docker can create it as root on a fresh clone
    (HERE / "output").mkdir(exist_ok=True)
    # .env is gitignored, so a fresh clone ships without one; create it up
    # front so nothing downstream (compose, the bot build) trips on it missing
    if not ENV.exists():
        ENV.write_text("")
    print("=" * 66)
    print("  automatch setup: answer a few questions, then it just runs")
    print("=" * 66)
    print()
    advanced = step_choose()
    ready = step_deps(advanced)     # installs first: the questionnaire's
    print()                         # spellcheck needs ollama running
    redo = not PROFILE.exists() or ask_yn(
        "config/profile.yaml already exists\n  redo it from scratch?", "n")
    spell = None                    # background spell-check handle
    spell_out, spell_report = {}, []
    if redo:
        s = step_search()
        metrics, s["threshold"], s["wildcard"] = step_metrics()
        # spell-check is the slow part on a weak CPU (one model call per
        # answer). Run it in the background while the user does the resume +
        # Discord steps, then join it just before writing the profile, so a
        # slow machine never stalls the questionnaire.
        def _spell():
            try:
                spell_out["s"], spell_out["metrics"] = spellfix(
                    s, metrics, quiet=True, report=spell_report)
            except Exception:       # never lose the answers to a spell-fail
                spell_out["s"], spell_out["metrics"] = s, metrics
        spell = threading.Thread(target=_spell, daemon=True)
        spell.start()
    else:
        print("  keeping your existing profile.")
    step_resume(advanced)
    cli_ok = True
    if advanced:
        cli_ok = step_discord()
    if spell is not None:
        if spell.is_alive():
            print("\n  finishing spell-check of your answers...")
        spell.join()
        for line in spell_report:
            print(line)
        write_profile(spell_out["s"], spell_out["metrics"])
    print()
    if not (ready and RESUME.exists()):
        print("  (finish the [--] items flagged above first)")
        print()
    if advanced:
        start = ('"automatch"' if cli_ok else
                 '"docker compose --profile advanced run --rm bot"')
        print(f"  to start the bot: run {start} in the terminal and keep it")
        print("  open. then in Discord, reply to the bot's DM with !match to")
        print("  start the program.")
        print()
        print(f"  to re-run later (no setup needed): run {start} again and send")
        print("  commands to the bot's DMs. !commands lists them all.")
    else:
        print(f"  to run it (anytime, no setup needed): {PY} start.py")
    if DONATE:
        print()
        print("  thank you for using my software! if you find it useful and")
        print(f"  can spare a dollar, the donation link is: {DONATE}")
    sys.exit(0 if ready else 2)    # 2 -> start.py knows not to run docker yet


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  setup cancelled; nothing else was changed.")
        sys.exit(1)
