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
RESUME_EMBED = HERE / "config" / "resume_embed.txt"   # OPTIONAL: stripped, embed-only
CONFIG = HERE / "config" / "config.yaml"
ENV = HERE / ".env"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
ANCHOR_STEPS = ("0.0", "0.2", "0.4", "0.6", "0.8", "1.0")
MARKER = "<tag>"
# example resume path in the OS's own style (Windows users shouldn't see /home)
RESUME_EG = (r"C:\Users\You\Documents\resume.docx" if os.name == "nt"
             else "/home/you/Documents/resume.txt")


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
        secret = getpass.getpass(prompt).strip()
        if secret:                       # getpass shows nothing as you type, so
            print(f"      (got {len(secret)} characters)")   # confirm it landed
        return secret
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
            elif c == "\x1b":                # escape seq: arrow keys, and the
                seq = sys.stdin.read(1)      # \e[200~ / \e[201~ wrappers that
                if seq in ("[", "O"):        # terminals put around a PASTE --
                    while True:              # consume the whole sequence so the
                        b = sys.stdin.read(1)        # "200~"/"201~" digits never
                        if not b or "@" <= b <= "~":  # land in the token and
                            break                     # corrupt it
            elif c >= " ":
                chars.append(c)
                redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print()
    result = "".join(chars).strip()
    if result:
        print(f"      (got {len(result)} characters)")
    return result


def ask_yn(prompt: str, default: str = "y") -> bool:
    while True:
        v = (input(f"  {prompt} (y/n --> enter)\n    user input: ")
             .strip() or default).lower()
        if v in ("y", "yes", "n", "no"):
            print()
            return v in ("y", "yes")
        print("  please answer y or n")


def ask_int(prompt: str, default: int, note: str = "", example: str = "") -> int:
    while True:
        v = ask(prompt, str(default), example=example, note=note)
        try:
            return int(v.replace(",", ""))
        except ValueError:
            print("  plain number please (example: 25)")


PLAIN_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.&/()'+_-]*$")


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


# Section banners are numbered as they're SHOWN, not by a fixed list: when the
# bootstrap flow skips [2] dependencies, the counter just doesn't tick for it,
# so the user sees [1] -> [2] -> ... with no gap. Standalone setup.py still
# shows dependencies, and there it's [2] like before.
_step_no = 0


def head(title: str, gap: bool = False) -> None:
    global _step_no
    _step_no += 1
    banner = f"==== [{_step_no}] {title} "
    print(("\n" if gap else "") + banner + "=" * max(0, 64 - len(banner)))


# ---------------------------------------------------------------- search ----
def step_search() -> dict:
    head("your job search")
    print()
    print("  type back at any question to redo the previous one")
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
        ("radius", lambda: ask_int("search radius in miles", 25)),
        ("age", lambda: ask_int("ignore postings older than (hours)", 24)),
        ("cap", lambda: ask_int(
            "stop each run after scraping how many new jobs", 128,
            example="128 or until no more jobs listed",
            note="use less jobs if low compute")),
        ("level", q_level),
        ("salary", q_salary),
        ("excludes", lambda: _listify(ask(
            "words to EXCLUDE\n  job types or companies, blank = none",
            example="construction, amazon",
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
                print("  (example: How likely am I to get an interview?)")
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

    print(f"  describe what each score looks like for '{name}': short")
    print("  concrete phrases; the examples shown are for an interview-odds "
          "metric.")
    print()
    anchors = {}
    anchor_ex = {"0.0": "completely underqualified, zero overlapping skills",
                 "0.2": "some overlapping skills but they want a PhD I don't have",
                 "0.4": "a stretch, but a few overlapping skills",
                 "0.6": "decent chance, several overlapping skills",
                 "0.8": "good fit, experience and skillset wise",
                 "1.0": "this job was made for you"}

    def vlines(s: str) -> int:
        """How many terminal lines a printed string actually occupies;
        long answers wrap, and the collapse must erase every wrapped line."""
        cols = shutil.get_terminal_size().columns
        return max(1, -(-len(s) // cols))

    i = 0
    while i < len(ANCHOR_STEPS):
        step = ANCHOR_STEPS[i]
        q_render = f"    {step} looks like (example: {anchor_ex[step]})"
        try:
            while True:
                d = ask(f"  {step} looks like", example=anchor_ex[step],
                        space=False)
                if d:
                    break
                print("    give a short example phrase")
        except GoBack:
            if i == 0:
                print("    (this is the first anchor)")
                continue
            i -= 1
            if sys.stdout.isatty():        # un-collapse the previous anchor
                prev = f"    {ANCHOR_STEPS[i]}: {anchors[ANCHOR_STEPS[i]]}"
                up = (vlines(q_render) + vlines("    user input: back")
                      + 1 + vlines(prev))
                sys.stdout.write(f"\033[{up}A\033[0J")
                if i > 0:
                    print()
            continue
        anchors[step] = d
        if sys.stdout.isatty():            # collapse Q+A into one tidy line,
            up = (vlines(q_render) + vlines("    user input: " + d)
                  + (0 if i == 0 else 1))  # + the blank above, after the 1st
            sys.stdout.write(f"\033[{up}A\033[0J")
            print(f"    {step}: {d}")
            print()
        i += 1
    print()
    return name, question, anchors


def step_metrics() -> tuple:
    head("your scoring rubric", gap=True)
    print()
    print("  What should a job be EVALUATED on? Each metric becomes a 0-1")
    print("  score judged by the local AI against your own example ladder:")
    print()
    print("    0.0  what a terrible fit looks like, in your words")
    print("    0.2  ...")
    print("    1.0  what a perfect fit looks like")
    print()
    print("  the bot averages the scores across your metrics (weighted) and")
    print("  only jobs above your threshold make the list")
    metrics = []
    while True:
        metrics.append(collect_metric(len(metrics) + 1,
                                      {m[0] for m in metrics}))
        if not ask_yn("\n  add another metric?", "n"):
            break
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


def spellfix(s: dict, metrics: list) -> tuple[dict, list]:
    """Fix spelling per-string with the local model when ollama is already up
    ('deloite' -> 'Deloitte'; exclusions match by exact substring, so typos
    there genuinely break filtering). No-op when ollama isn't installed yet."""
    model = judge_model()
    changes = []

    def fix(text, rule, context):
        if len(text.strip()) < 4:    # too short to spellcheck; the model
            return text              # invents content for fragments
        print(".", end="", flush=True)   # a dot per field: shows it's alive,
        try:                             # not frozen, during the slow AI calls
            new = _fix_one(model, text, rule, context)
        except (OSError, ValueError):
            return text
        if new and new != text:
            changes.append(f"{text} -> {new}")
            return new
        return text

    have = _ollama_models()    # reachable AND the judge model is pulled?
    if have is None or not _model_present(model, have):
        print("\n  (spell-check skipped: ollama or the judge model isn't "
              "installed yet)")
        return s, metrics
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
    print()                            # close the line of progress dots
    if changes:
        print("  fixed:")
        for c in changes:
            print(f"    {c}")
    else:
        print("  no spelling fixes needed")
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
    PROFILE.write_text("\n".join(out), encoding="utf-8")
    print(f"\n  wrote {PROFILE}")


# ---------------------------------------------------------------- resume ----
def step_resume(advanced: bool = False) -> None:
    head("your resume", gap=True)
    print()
    if advanced:
        _resume_template_upload()
    else:
        _resume_upload()
    if RESUME.exists():               # offer the optional stripped match-resume
        _embed_resume_upload()


def _read_text_any(p: Path) -> str:
    """Read a user-supplied text file robustly. Resumes get saved as UTF-8
    (Word/Google Docs export, modern editors) or legacy Windows-1252 ('ANSI'
    in old Notepad). Reading with the platform default (cp1252 on Windows)
    crashes on a normal UTF-8 file with smart quotes -- so decode explicitly,
    UTF-8 first, then fall back. Returns text the Docker side reads as UTF-8."""
    data = p.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


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
    return _read_text_any(src)


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
    print("  (a working example resume: config/resume_template.example.docx)")
    print("  save it as .docx (Word/Google Docs) or .txt; both work")
    print()
    print("  once you add the tag, tell me where the file is:")
    while True:
        p = ask_nb("path to your tagged resume .txt\n  I'll copy it into place",
                   example=RESUME_EG)
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
            print("  (a working example: config/resume_template.example.docx)")
            print("  save, then give me the path again")
            continue
        RESUME_TEMPLATE.write_text(text, encoding="utf-8")
        RESUME.write_text(_strip_tag(text), encoding="utf-8")
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
                   example=RESUME_EG)
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
            RESUME.write_text(_read_resume(src), encoding="utf-8")
        except Exception:
            print("  couldn't read that file. is it a normal .docx or .txt?")
            continue
        conv = " (converted from .docx)" if src.suffix.lower() == ".docx" else ""
        print(f"  copied to {RESUME}{conv}. good.")
        return


def _embed_resume_upload() -> None:
    """OPTIONAL (asked once, during first-startup setup): a stripped-down resume
    used ONLY for the job-matching (embedding) step. Removing the name, contact
    info, education and section titles ('Skills', 'Job History', ...) leaves
    just the real substance, so the matcher references real experience instead
    of boilerplate -- sharper results. The full config/resume.txt is still what
    the AI judge and the resume builder read."""
    if RESUME_EMBED.exists():
        print()
        print(f"  found a stripped match-resume (config/resume_embed.txt, "
              f"{RESUME_EMBED.stat().st_size} bytes)")
        if not ask_yn("  replace it?", "n"):
            return
    else:
        print()
        print("  OPTIONAL: a stripped resume for sharper job matching")
        print("  automatch can match jobs against a stripped-down copy of your")
        print("  resume -- just the real substance, with the filler removed:")
        print("    - your name and contact info")
        print("    - education")
        print("    - section titles like 'Skills' or 'Job History'")
        print("  this points the matching model at your real experience instead")
        print("  of headings, for better results. (Your full resume is still")
        print("  used for the AI judging and the resume builder.)")
        print("  example to copy the format: config/resume_stripped.example.txt")
        print()
        if not ask_yn("  provide a stripped version now?", "n"):
            print("  no problem -- matching will auto-strip your resume's name")
            print("  and contact info for you instead.")
            return
    while True:
        p = ask_nb("path to your stripped resume (.docx or .txt)\n"
                   "  I'll copy it into place", example=RESUME_EG)
        if not p:
            print("  skipped; the matcher will auto-strip your resume instead.")
            return
        src = Path(p.strip("'\"")).expanduser()
        if not src.exists():
            print(f"  can't find {src}")
            continue
        if src.suffix.lower() not in (".txt", ".md", ".docx", ""):
            print(f"  {src.suffix} won't work; use .txt or .docx")
            continue
        try:
            RESUME_EMBED.write_text(_read_resume(src), encoding="utf-8")
        except Exception:
            print("  couldn't read that file. is it a normal .docx or .txt?")
            continue
        conv = " (converted from .docx)" if src.suffix.lower() == ".docx" else ""
        print(f"  copied to config/resume_embed.txt{conv}. matching will use")
        print("  this stripped copy; judging still uses your full resume.")
        return


# ------------------------------------------------------------------ deps ----
def judge_model() -> str:
    try:
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("judge:"):
                return line.split(":", 1)[1].split("#")[0].strip().strip("\"'")
    except OSError:
        pass
    return "mistral-nemo"


OVERRIDE = Path("/etc/systemd/system/ollama.service.d/override.conf")


def offer(cmd: str, why: str, admin: bool = True) -> bool:
    """Show the exact command, offer to run it right here. Returns True if
    it ran and succeeded; the command stays on screen for copy-paste if not."""
    print(f"\n  {why}")
    priv = " with administrative privileges" if admin else ""
    print(f"  run this command{priv} to continue:")
    print(f"\n      {cmd}\n")
    if ask_yn("  run it now?", "y"):
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


def step_deps() -> bool:
    head("dependencies")
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
        print("  [ok] docker is installed and running")
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
            print("  IMPORTANT: log OUT and back IN so the docker group")
            print("  takes effect, then rerun: python3 setup.py")
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
                print("  IMPORTANT: log OUT and back IN so the docker group")
                print("  takes effect, then rerun: python3 setup.py")
        elif docker_bin:
            print("  [--] docker is installed but not running; open the")
            print("       Docker Desktop app, wait for it to start, rerun")
        elif has_winget:
            if offer("winget install -e --id Docker.DockerDesktop "
                     "--accept-package-agreements --accept-source-agreements",
                     "[--] docker is missing.", admin=False):
                print("  installed. Docker Desktop needs WSL2 and a reboot:")
                print("  reboot, launch Docker Desktop once so the engine")
                print("  starts, then rerun: python start.py")
        else:
            print("  [--] docker is missing; install Docker Desktop:")
            print("       https://docs.docker.com/desktop/")

    have = _ollama_models()
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
    if judge_model() == "mistral-nemo":
        print("  NOTE: the default judge model 'mistral-nemo' is a ~7 GB")
        print("  download and wants ~8 GB+ of free RAM (or a GPU). It still")
        print("  runs on a low-RAM PC, just slowly. For a much lighter judge,")
        print("  set 'judge:' in config/config.yaml to a small model such as")
        print("  llama3.2:3b (~2 GB) -- or switch it later from the Discord bot")
        print("  with !model. On a slow PC, also lower 'max_jobs'.")
        print()
    if have is None:
        ready = False
        judge = judge_model()
        print(f"  once ollama is up:  ollama pull nomic-embed-text "
              f"&& ollama pull {judge}")
        print("  (no GPU? see the 'No GPU?' note in docs/manual-setup.md)")
    else:
        print("  [ok] ollama is running")
        judge = judge_model()
        for model in ("nomic-embed-text", judge):
            # exact match when a tag like :3b is specified; base match
            # only for untagged names (resolves to :latest)
            if _model_present(model, have):
                print(f"  [ok] model {model} downloaded")
            elif not offer(f"ollama pull {model}",
                           f"[--] model {model} is missing (big download).",
                           admin=False):
                ready = False
        if linux:
            if OVERRIDE.exists() and "0.0.0.0" in OVERRIDE.read_text(
                    encoding="utf-8"):
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


def _install_cli() -> bool:
    """Install a global `automatch` command that starts the bot from anywhere.
    It just RUNS the bot -- no --build -- because the container was already
    downloaded during setup (start.py's 'ready to begin?' step); compose still
    builds on its own if the image is somehow missing. Generated per-OS.
    Returns True when it's ready to use."""
    try:
        if os.name == "nt":      # native Windows: a .bat on the default PATH
            bindir = (Path(os.environ.get("LOCALAPPDATA", ""))
                      / "Microsoft" / "WindowsApps")
            if not bindir.is_dir():
                return False
            (bindir / "automatch.bat").write_text(
                "@echo off\r\n"
                f'cd /d "{HERE}"\r\n'
                "docker compose --profile advanced run --rm bot\r\n",
                encoding="utf-8")
            return True
        bindir = Path.home() / ".local" / "bin"   # linux / mac / WSL
        bindir.mkdir(parents=True, exist_ok=True)
        cli = bindir / "automatch"
        cli.write_text(
            "#!/bin/sh\n"
            f'cd "{HERE}" || exit 1\n'
            'exec env UID="$(id -u)" GID="$(id -g)" '
            "docker compose --profile advanced run --rm bot\n",
            encoding="utf-8")
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
        lines = [l for l in ENV.read_text(encoding="utf-8").splitlines()
                 if not l.startswith("DISCORD_")]
    lines += [f"{k}={v}" for k, v in updates.items() if v]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote {ENV} (keep this file secret; it's git/docker-ignored)")


def step_choose() -> bool:
    head("setup type")
    print()
    print("  would you like to set up the ADVANCED setup?")
    print()
    print("  advanced: adds beat-the-ATS custom resumes. a free Discord bot")
    print("  (https://discord.com/developers/applications) asks which skills")
    print(f"  each job wants and swaps your answer into the {MARKER} in your")
    print("  resume, saved to output/resumes/. more setup, more capable.")
    print()
    print("  basic: scrape jobs -> score them YOUR way -> ranked matches.html.")
    print("  fill one file, run one command.")
    print()
    return ask_yn("go with the ADVANCED setup?", "n")


def step_discord() -> None:
    """ADVANCED ONLY: bot credentials walkthrough -> .env."""
    head("discord bot", gap=True)
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
    print()
    print("      HEADS UP: the box below is INVISIBLE on purpose (it hides")
    print("      your secret token). Your PASTE still works even though you")
    print("      see nothing -- right-click, or Ctrl+V / Ctrl+Shift+V. Just")
    print("      paste once and press ENTER to continue; it'll confirm how")
    print("      many characters it got.")
    token = ""
    while not token:
        token = ask_secret("      user input (paste the PRIVATE token, "
                           "then press ENTER -- nothing will show): ")
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
    print("   4. let the bot DM you: when setup finishes, the bot starts")
    print("      the conversation in your DMs. discord only delivers a")
    print("      bot's DMs if you share a server with it, so add it to")
    print("      one (a private server with just you works fine):")
    print("      page: https://discord.com/developers/applications")
    print("      -> your app -> OAuth2 -> URL Generator, then:")
    print("        a) in the SCOPES list, tick the 'bot' box (just that one)")
    print("        b) a 'BOT PERMISSIONS' box now appears below -- in it tick:")
    print("           'Send Messages', 'Attach Files', 'Read Message History'")
    print("        c) scroll to the bottom, click 'Copy' on the GENERATED URL")
    print("        d) paste that URL in your browser -> pick your server")
    print("           (the private one with just you) -> Authorize")
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
    # The "how to start" instructions are printed once, at the very end of the
    # run (start.py's closing banner, or GO.bat's terminal landing), so don't
    # repeat them here -- the user would otherwise see the same thing 2-3 times.


def main() -> None:
    # user-owned BEFORE docker can create it as root on a fresh clone
    (HERE / "output").mkdir(exist_ok=True)
    print("=" * 66)
    print("  automatch setup: answer a few questions, then it just runs")
    print("=" * 66)
    print()
    advanced = step_choose()
    # bootstrap.py (the setup doctor / first stage) already installs and
    # verifies every dependency, then sets AUTOMATCH_DEPS_OK before handing
    # off. Honour it so we don't run the whole [2] dependencies pass again and
    # make the user sit through the same install checks a second time.
    if os.environ.get("AUTOMATCH_DEPS_OK") == "1":
        ready = True
    else:
        ready = step_deps()         # installs first: the questionnaire's
        print()                     # spellcheck needs ollama running
    redo = not PROFILE.exists() or ask_yn(
        "config/profile.yaml already exists\n  redo it from scratch?", "n")
    if redo:
        s = step_search()
        metrics, s["threshold"], s["wildcard"] = step_metrics()
        s, metrics = spellfix(s, metrics)
        write_profile(s, metrics)
    else:
        print("  keeping your existing profile.")
    step_resume(advanced)
    if advanced:
        step_discord()
    # When start.py launched us it prints the first-run next-steps itself (with
    # the build prompt), so skip this banner to avoid two closing screens.
    # Standalone `python setup.py` still shows it -- it's the only ending then.
    if os.environ.get("AUTOMATCH_FROM_START") == "1":
        sys.exit(0 if ready else 2)
    print("\n" + "=" * 66)
    if ready and RESUME.exists():
        print("  everything is ready. run it:")
    else:
        print("  finish the [--] items above, then run it:")
    if os.name == "nt":
        print('    docker compose run --rm automatch run -r 5   # small test')
    else:
        print('    env UID=$(id -u) GID=$(id -g) docker compose run --rm \\')
        print('        automatch run -r 5                       # small test')
    print(f"    {PY} start.py                             # the real thing")
    if advanced:
        print("    automatch                            # ATS bot; DM it `!match`")
    print("=" * 66)
    sys.exit(0 if ready else 2)    # 2 -> start.py knows not to run docker yet


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  setup cancelled; nothing else was changed.")
        sys.exit(1)
