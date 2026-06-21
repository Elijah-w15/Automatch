"""score.py: match each job to the resume.

Two signals, combined by configurable weights:
  1. cosine: embedding similarity between resume text and job description.
  2. YOUR vectors: every vector defined in config/profile.yaml gets judged by
     the LLM against YOUR 0->1 anchor examples; one model call per job
     covers all vectors at once, temperature 0 so scores are reproducible.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata

import requests

from . import paths
from .paths import read_jsonl
from .scrape import dedupe_key, too_old
from .tailor import approved_raw as _approved_raw, on_resume

EMBED_MAX = 6000     # nomic 500s above ~8k chars
DESC_MAX = 12000


def ollama_host(cfg: dict) -> str:
    # env var wins: inside a container this points at the HOST's ollama
    return os.environ.get("OLLAMA_HOST", cfg["models"].get("host", "http://localhost:11434"))


def clean_text(s: str) -> str:
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    return re.sub(r"\s+", " ", s).strip()


# markup that only adds noise to the EMBEDDING (cosine). The judge still sees the
# full raw description; this only cleans the text we embed, so similarity reflects
# the role's words, not the posting's formatting. Skill tokens like C++ / C# / A+
# survive: markers are stripped only when space-bounded, never mid-token.
_EMB_ESCAPED = re.compile(r"\\([^\w\s])")                 # JobSpy \- \& \. \+ \| -> bare char
_EMB_BOLD = re.compile(r"\*{1,3}([^*]+?)\*{1,3}")         # **bold** / *italic* -> inner text
_EMB_MARKER = re.compile(r"(?:(?<=\s)|^)[•|*+]+(?=\s)")  # bullet/pipe/nav markers
_EMB_SMART = str.maketrans({"’": "'", "‘": "'", "“": '"',
                            "”": '"', "–": "-", "—": "-",
                            "…": "..."})


def strip_for_embed(text: str) -> str:
    """Clean a job description (or resume) for EMBEDDING ONLY. Removes markdown
    emphasis, bullet/pipe/nav markers, JobSpy's backslash escapes (\\- \\& \\+),
    and smart punctuation so cosine matches on the role, not formatting. The
    judge is unaffected: it always receives the full raw description. Skill
    tokens (C++, C#, A+) are preserved because markers are stripped only when
    space-bounded, never inside a word."""
    t = _EMB_ESCAPED.sub(r"\1", text or "")
    t = t.translate(_EMB_SMART)
    t = _EMB_BOLD.sub(r"\1", t)
    t = _EMB_MARKER.sub(" ", t)
    return clean_text(t)


# A scraped "description" that is actually the site's navigation/chrome (login,
# sign-up, "post a job" menus) rather than the posting; some boards leak their
# page shell when the real body fails to load. Detected so it can be blanked:
# the job, title and company are kept (it falls back to title-only), but the junk
# never reaches the embedder or the judge, where it produced a meaningless cosine
# and an inflated interview_odds (audit 2026-06-21).
_NAV_PHRASES = ("log in", "sign up", "sign in", "post a job", "post your",
                "my account", "search resumes", "upload my resume",
                "create account", "client referrals", "popular packages",
                "what we offer", "job seekers", "forgot password",
                "more information", "contact us", "privacy policy",
                "terms of service", "register now", "back to search")
_JOB_WORDS = ("responsib", "qualif", "experience", "you will", "looking for",
              "requirement", "skills", "degree", " years", "develop", "design",
              "team", "role", "position", "duties", "ability to", "work with",
              "support", "manage", "build")


def is_page_chrome(text: str) -> bool:
    """True if a scraped description is site navigation/chrome instead of a real
    posting. Conservative: needs several nav phrases, NO job-content words, AND
    either dense list markers or a short body, so genuine postings (which always
    carry job-content words) are never flagged."""
    t = clean_text(text).lower()
    words = t.split()
    if not words:
        return False
    nav_hits = sum(1 for p in _NAV_PHRASES if p in t)
    has_job = any(p in t for p in _JOB_WORDS)
    markers = sum(t.count(c) for c in "•|*+")
    dense = markers >= 8 and markers / len(words) > 0.12
    return nav_hits >= 3 and not has_job and (dense or len(words) < 150)


_CONTACT = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+"                       # emails
    r"|(?:https?://|www\.)\S+"                       # urls
    r"|\b(?:github|linkedin)\.com/\S+"               # handles without scheme
    r"|(?:\+?\d{1,3}[-. ]*)?\(?\d{3}\)?[-. ]*\d{3}[-. ]*\d{4}"  # phones
    r"|•", re.I)


def strip_contact(text: str) -> str:
    """Resume minus contact boilerplate: EMBEDDING ONLY (the judge and
    the <tag> resume builder still see the full text). The first line is
    taken as the candidate's NAME, and every instance of that name is
    stripped. So company names elsewhere are never touched."""
    lines = text.splitlines()
    while lines and not lines[0].strip():            # docx exports often
        lines.pop(0)                                 # start with blank lines
    name = ""
    if lines and 0 < len(lines[0].split()) <= 5:     # 'John Doe' header line
        name = lines[0].strip()
        lines = lines[1:]
    body = "\n".join(lines)
    if name:
        body = re.sub(re.escape(name), " ", body, flags=re.I)
        for w in name.split():                       # first/last name alone
            if len(w) >= 3:
                body = re.sub(rf"(?i)(?<![a-z']){re.escape(w)}(?![a-z'])",
                              " ", body)
    return _CONTACT.sub(" ", body)


def _post(host: str, path: str, payload: dict) -> dict:
    # generous timeout: CPU-only machines can take minutes per judgment
    r = requests.post(f"{host}{path}", json=payload, timeout=600)
    r.raise_for_status()
    return r.json()


def embed(host: str, model: str, text: str) -> list[float]:
    data = _post(host, "/api/embeddings", {"model": model, "prompt": text[:EMBED_MAX]})
    emb = data.get("embedding") or []
    if not emb:
        raise RuntimeError("empty embedding from ollama")
    return emb


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


PROMPT_V = "v8-dejunk-desc"  # bump when the judging method itself changes
EMBED_V = "v1-strip"  # bump when the EMBEDDING text-prep (strip_for_embed) changes


def embed_source(raw_resume: str) -> str:
    """Text embedded for job-match similarity: a user-provided curated resume
    (config/resume_embed.txt) when present, else the auto-stripped RESUME
    (name + contact lines removed). Editing the curated file changes matches,
    so it is folded into the rub below to force a re-score."""
    if paths.RESUME_EMBED.exists():
        return paths.RESUME_EMBED.read_text()
    return strip_contact(raw_resume)


def rubric_hash(vectors: dict, resume: str, judge: str,
                wildcard: str = "", embed_custom: str = "") -> str:
    """Identifies WHAT scores were judged/matched against (questions + anchors +
    resume + judge model + prompt version, plus a CUSTOM embedding resume if
    one is set). Weights are excluded on purpose: they're applied at rank time,
    so re-weighting never re-judges. embed_custom is appended only when set, so
    users on the default auto-strip keep their existing rub (no forced re-score)."""
    core = {n: {"q": (v or {}).get("question"), "a": (v or {}).get("anchors")}
            for n, v in vectors.items()}
    blob = (json.dumps(core, sort_keys=True, default=str)
            + "\0" + resume + "\0" + judge + "\0" + wildcard
            + "\0" + PROMPT_V + "\0" + EMBED_V)
    if embed_custom:
        blob += "\0EMBED\0" + embed_custom
    return hashlib.md5(blob.encode()).hexdigest()[:10]


def current_rub(cfg: dict, vectors: dict) -> str | None:
    """The rub for the CURRENT config: THE one derivation, shared by
    score and rank so they can never drift apart."""
    if not paths.RESUME.exists():
        return None
    embed_custom = (clean_text(paths.RESUME_EMBED.read_text())
                    if paths.RESUME_EMBED.exists() else "")
    return rubric_hash(vectors, clean_text(paths.RESUME.read_text()),
                       str(cfg["models"].get("judge", "")),
                       str(cfg["score"].get("wildcard") or "").strip(),
                       embed_custom)


def generate_json(host: str, model: str, prompt: str) -> dict:
    # num_ctx sized to the prompt: ollama's default context silently
    # truncates big rubrics (10+ vectors), cascading into garbage scores
    ctx = max(4096, min(32768, len(prompt) // 3 + 512))
    data = _post(host, "/api/generate",
                 {"model": model, "prompt": prompt, "stream": False,
                  "format": "json",
                  "options": {"temperature": 0, "num_ctx": ctx}})
    raw = data.get("response", "")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        i, j = raw.find("{"), raw.rfind("}")
        if i != -1 and j > i:
            try:
                return json.loads(raw[i:j + 1])
            except json.JSONDecodeError:
                pass
        return {}


LEVELS = ("entry", "mid", "senior", "executive")


def vector_prompt(vectors: dict, title: str, company: str, desc: str,
                  resume: str, approved: set, wildcard: str = "") -> str:
    """Build the judging prompt FROM the user's own anchor examples."""
    lines = ["You are scoring a job posting for a specific candidate. "
             "Respond with ONLY a JSON object.\n"
             'FIRST write "day_to_day": 2-3 plain sentences describing what '
             "this person would ACTUALLY be doing all day in this role, "
             "inferred from the concrete duties in the posting. Job postings "
             "exaggerate; ignore marketing language like 'dynamic', "
             "'fast-paced', 'rockstar', 'self-starter culture' and reason "
             "from the real tasks.\n"
             "THEN score each metric as a WHOLE NUMBER from 0 to 100, "
             "judging the day-to-day reality you just described against "
             "that metric's question and its anchor rubric (the anchors sit "
             "at 0, 20, 40, 60, 80, 100), NOT the posting's vibe. Score at "
             "FINE resolution: place the job at the exact point in the "
             "0-100 range, within the spread of the anchors, where it truly "
             "falls, adjusting by single points when needed to achieve "
             "granularity rather than jumping in fives or tens, unless a "
             "five-point gap reflects the difference between two similar "
             "jobs more accurately than a four-point one. Two jobs are "
             "rarely equal; if one "
             "fits this metric even slightly better than another it must get "
             "a slightly higher number, not the same one. Small real "
             "differences should produce small score differences.\n"]
    for name, v in vectors.items():
        lines.append(f'METRIC "{name}": {v.get("question", "")}')
        for k in sorted(v.get("anchors", {}), key=float):
            # anchors are authored 0-1 but shown to the judge on a 0-100 scale
            lines.append(f"  {int(round(float(k) * 100))} = {v['anchors'][k]}")
        lines.append("")
    lines.append("Score DECISIVELY across the full 0-100 range; values "
                 "between anchors are expected. If a job clearly fails a "
                 "metric's high anchors, score it LOW (0-30); do not "
                 "default to the middle of the scale.\n")
    lines.append('Also classify "level": the role\'s seniority, exactly one of '
                 '"entry" (0-2 years / new grad), "mid" (2-5 years), '
                 '"senior" (5+ years / senior IC), "executive" '
                 "(staff/principal/director/VP or managing people).\n")
    lines.append('Also "keyword_candidates": up to 3 short ATS skill phrases '
                 '(e.g. "Docker", "CI/CD") that this job clearly wants AND '
                 "the candidate plausibly has, but that are NOT already "
                 "written on their resume. We want NEW keywords to ADD; do "
                 "not repeat skills the resume already lists."
                 + (f" Already approved (exclude these too): "
                    f"{sorted(approved)}\n" if approved else "\n"))
    if wildcard:
        lines.append('Also "wildcard": a WHOLE NUMBER 0-100 score for how '
                     f'well this job matches this description: "{wildcard}"\n')
    keys = ", ".join(f'"{n}": <integer 0-100>' for n in vectors)
    lines.append('Return EXACTLY: {"day_to_day": "<2-3 sentences>", '
                 f'{keys}, '
                 '"level": "<entry|mid|senior|executive>", '
                 '"keyword_candidates": ["..."]}\n')
    lines.append(f"=== JOB ===\n{title} @ {company}\n{desc}\n")
    lines.append(f"=== CANDIDATE RESUME ===\n{resume}\n")
    return "\n".join(lines)


REFUSALISH = re.compile(r"(?i)\b(i cannot|i can't|i am unable|i'm sorry|"
                        r"as an ai|cannot assist)\b")


def filter_candidates(raw, resume: str, approved: set) -> list[str]:
    """Keep up to 3 clean, NEW keyword suggestions."""
    if not isinstance(raw, list):   # a stray string would iterate per-char
        return []
    out = []
    for c in raw:
        if not isinstance(c, str):
            continue
        c = c.strip()
        if (c and len(c) < 60 and not REFUSALISH.search(c)
                and c.lower() not in approved
                and not on_resume(c, resume)
                and c.lower() not in (x.lower() for x in out)):
            out.append(c)
    return out[:3]


def clamp01(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def to01(v) -> float:
    """The judge scores on a 0-100 integer scale (more granular than 0-1,
    which LLMs collapse onto the anchor values); normalize to the 0-1 we
    store and display. A stray 0-1 float (judge ignoring the scale) is taken
    as already normalized so it isn't shrunk to ~0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f > 1.0:
        f /= 100.0
    return max(0.0, min(1.0, f))


def run(cfg: dict, vectors: dict, should_cancel=None) -> int:
    if not paths.JOBS.exists():
        print("no jobs scraped yet; run the full pipeline first "
              "(python3 start.py)", flush=True)
        return 0
    host = ollama_host(cfg)
    raw_resume = paths.RESUME.read_text()
    resume = clean_text(raw_resume)
    # cosine is matched on skills/experience content only: a curated
    # config/resume_embed.txt if the user provided one, else the auto-strip
    # (name header + contact lines removed)
    resume_emb = embed(host, cfg["models"]["embed"],
                       strip_for_embed(embed_source(raw_resume)))

    rub = current_rub(cfg, vectors)
    hours = int(cfg["scrape"].get("hours_old", 0) or 0)
    scored_urls = set()
    done_keys = set()      # postings holding a WRITTEN fresh same-rub score
    # rows judged under a DIFFERENT rubric/resume don't count as scored;
    # they get re-judged so rankings never mix rubrics
    for row in read_jsonl(paths.SCORES):
        if row.get("rub") == rub:
            scored_urls.add(row.get("url", ""))
            if not too_old(row, hours):
                done_keys.add(dedupe_key(row, loose=False))

    todo = []
    for job in read_jsonl(paths.JOBS):
        url = job.get("job_url") or ""
        if not url or url in scored_urls:
            continue
        # skip postings already too old to rank; without this, a rubric
        # edit months in would re-judge the entire dead backlog
        if too_old(job, hours):
            continue
        scored_urls.add(url)          # in-batch dedupe too
        todo.append(job)

    print(f"scoring {len(todo)} jobs", flush=True)
    approved_names = _approved_raw()
    approved = {a.lower() for a in approved_names}
    wildcard = str(cfg["score"].get("wildcard") or "").strip()
    cw = float(cfg["score"].get("cosine_weight", 1.0))
    vweights = {n: float(v.get("weight", 1.0)) for n, v in vectors.items()}
    total_w = cw + sum(vweights.values())

    n = dupes = chrome = 0
    with paths.SCORES.open("a") as out:
        for job in todo:
            # cooperative pause (Discord !pause): each job's score is written
            # as it finishes, so the ones already done are safe on disk
            if should_cancel and should_cancel():
                print("  paused; jobs scored so far are saved "
                      "(unscored ones retry next run)", flush=True)
                break
            # the same posting on another board (or a same-day repost):
            # a fresh twin's score already represents it and rank
            # collapses them anyway, so judging this copy would only
            # burn model time. Checked against WRITTEN rows only: if the
            # twin's judging failed, this copy still gets its turn
            key = dedupe_key(job, loose=False)
            # no company listed: can't tell twins from distinct postings,
            # so always judge rather than silently drop one
            if key[1] and key in done_keys:
                dupes += 1
                continue
            title = job.get("title") or ""
            company = str(job.get("company") or "")
            desc = clean_text(job.get("description") or "")[:DESC_MAX]
            if desc and is_page_chrome(desc):
                desc = ""          # scraped site chrome, not a real posting
                chrome += 1
            desc = desc or f"(no description provided; title only: {title})"
            try:
                # embed the STRIPPED desc (markup removed); the judge below still
                # gets the full `desc`. The fallback title-only string has nothing
                # to strip, so this is a no-op for description-less postings.
                cos = cosine(resume_emb,
                             embed(host, cfg["models"]["embed"], strip_for_embed(desc)))
                prompt = vector_prompt(vectors, title, company, desc,
                                       resume, approved, wildcard)
                judged = generate_json(host, cfg["models"]["judge"], prompt)
                if any(judged.get(n) is None for n in vectors):
                    judged = generate_json(host, cfg["models"]["judge"],
                                           prompt)        # one retry
                missing = [n for n in vectors if judged.get(n) is None]
                if missing:
                    # better unscored (retried next run) than silently 0.0
                    print(f"  ! judge omitted {missing} for {title[:40]}; "
                          "skipping (will retry next run)", flush=True)
                    continue
            except (requests.RequestException, RuntimeError) as e:
                # don't lose the whole batch to one bad call; this job
                # stays unscored and gets retried on the next run
                print(f"  ! scoring failed for {title[:40]}: {e}; skipping "
                      f"(is the model pulled? ollama pull "
                      f"{cfg['models']['judge']})", flush=True)
                continue
            vscores = {name: to01(judged.get(name)) for name in vectors}
            level = str(judged.get("level") or "").strip().lower()
            if level not in LEVELS:
                level = "mid"          # unclassifiable -> neutral middle
            # the stored score is RAW (no level preference baked in); level
            # prefs are applied at rank time, so changing them in config
            # re-ranks instantly without any model re-runs
            final = (cw * cos + sum(vweights[k] * v for k, v in vscores.items())) / total_w
            out.write(json.dumps({
                "url": job.get("job_url") or "", "title": title, "company": company,
                "location": job.get("location") or "",
                "date_posted": str(job.get("date_posted") or ""),
                "scraped_at": str(job.get("scraped_at") or ""),
                "rub": rub,
                "day_to_day": str(judged.get("day_to_day") or "")[:500],
                "wild": to01(judged.get("wildcard")) if wildcard else None,
                "cosine": round(cos, 4), "vectors": {k: round(v, 4) for k, v in vscores.items()},
                "level": level,
                "keywords": filter_candidates(judged.get("keyword_candidates"),
                                              resume, approved),
                # an ALREADY-confirmed skill this job wants -> the bot
                # auto-applies it without asking (candidates exclude approved
                # skills by design, so this is recorded separately)
                "approved_hit": next((a for a in approved_names
                                      if on_resume(a, desc)), None),
                "score": round(final, 4),
            }, ensure_ascii=False) + "\n")
            if key[1]:
                done_keys.add(key)
            n += 1
            vs = " ".join(f"{k}={v:.2f}" for k, v in vscores.items())
            print(f"  [{n}/{len(todo)}] {final:.3f} ({level} cos {cos:.2f} {vs}) "
                  f"{title[:40]}", flush=True)
    if dupes:
        print(f"  {dupes} copies of already-scored postings skipped "
              "(same job, other board)", flush=True)
    if chrome:
        print(f"  {chrome} postings had site-chrome (nav menu) instead of a "
              "description; scored on title only", flush=True)
    print(f"DONE: {n} scored -> output/scores.jsonl", flush=True)
    return n
