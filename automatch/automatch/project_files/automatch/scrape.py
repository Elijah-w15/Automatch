"""scrape.py: pull job postings via JobSpy into output/jobs.jsonl.

Deliberately simple: one JSONL file of raw postings + a seen.json so
re-runs only add NEW jobs.
"""
from __future__ import annotations

import json
import math
import os
import re
import time

from jobspy import scrape_jobs
import pandas as pd

from . import paths


def word_match(needle: str, hay: str) -> bool:
    """Word-boundary, case-insensitive: 'int' must NOT match 'Maintenance'.
    Multi-word needles match as a TOGETHER phrase: 'project manager' hits
    'Senior Project-Manager' but not 'Project Coordinator ... Manager'."""
    words = needle.split()
    if not words:
        return False
    pat = (r"(?<![a-z0-9])" + r"[\s\-/]+".join(re.escape(w) for w in words)
           + r"(?![a-z0-9])")
    return re.search(f"(?i){pat}", hay) is not None


def too_old(job: dict, hours_old: int) -> bool:
    """Boards leak stale postings past their own date filter; re-check.
    Day granularity, counted from 23:59 of the posted day (lenient).
    Postings with no date age from the day WE first scraped them, so
    undated jobs can't live in the rankings forever."""
    dp = job.get("date_posted") or job.get("scraped_at")
    if not dp:
        return False
    try:
        posted_eod = time.mktime(time.strptime(str(dp)[:10], "%Y-%m-%d")) + 86399
    except ValueError:
        return False
    return (time.time() - posted_eod) > hours_old * 3600


def dedupe_key(row: dict, *, loose: bool = True) -> tuple[str, str]:
    """The same posting listed on several boards has different URLs;
    normalized title + company identifies the content. rank uses the
    LOOSE form (company first word, so 'Acme' == 'Acme Corp') to collapse
    display duplicates, keeping the best-scoring copy. score uses the
    STRICT form (full company) so a coincidental title match between two
    real companies ('Engineer' at Apex Staffing vs Apex Energy) never
    blocks a judgment."""
    comp = str(row.get("company") or "").lower()
    if loose:
        return (re.sub(r"[^a-z0-9]+", "", str(row.get("title") or "").lower()),
                (comp.split() or [""])[0])
    city = str(row.get("location") or "").split(",")[0]
    return (re.sub(r"[^a-z0-9]+", "", str(row.get("title") or "").lower()),
            re.sub(r"[^a-z0-9]+", "", comp),
            re.sub(r"[^a-z0-9]+", "", city.lower()))


def prune(hours_old: int) -> None:
    """Each new scrape clears rows older than the user's listing-age
    window from jobs.jsonl and scores.jsonl so they never grow forever
    (they couldn't rank anymore anyway). seen.json is deliberately KEPT:
    a cleared URL must never be re-scraped (and re-scored) if a board
    serves it again."""
    keep_h = int(hours_old or 0)
    if keep_h <= 0:        # no window configured: never wipe everything
        return
    for path in (paths.JOBS, paths.SCORES):
        if not path.exists():
            continue
        rows = list(paths.read_jsonl(path))
        kept = [r for r in rows if not too_old(r, keep_h)]
        if len(kept) == len(rows):
            continue
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            for r in kept:
                f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        print(f"  cleared {len(rows) - len(kept)} rows older than {keep_h}h "
              f"from {path.name}", flush=True)


def run(cfg: dict, results_override: int | None = None) -> int:
    scfg = cfg["scrape"]
    cap = int(scfg.get("max_jobs", 250))
    hours_old = scfg.get("hours_old", 72)
    prune(int(hours_old or 0))
    seen = set(json.loads(paths.SEEN.read_text())) if paths.SEEN.exists() else set()
    excludes = [str(x).lower() for x in scfg.get("exclude") or []]
    terms = scfg["search_terms"]
    written = 0

    # heal a torn last line: a hard kill mid-write can leave jobs.jsonl without
    # a trailing newline; appending would then fuse our first row onto that
    # fragment on one physical line, and read_jsonl would drop BOTH. A one-byte
    # peek + separator newline isolates the unparseable fragment on its own line.
    torn = False
    if paths.JOBS.exists() and paths.JOBS.stat().st_size:
        with paths.JOBS.open("rb") as chk:
            chk.seek(-1, os.SEEK_END)
            torn = chk.read(1) != b"\n"

    with paths.JOBS.open("a") as out:
        if torn:
            out.write("\n")
        for i, term in enumerate(terms):
            if written >= cap:
                print(f"=== max_jobs cap ({cap}) reached; stopping ===", flush=True)
                break
            # each term requests its fair share of what's still needed:
            # JobSpy's results_wanted is PER SITE, so divide by site count;
            # boards returning less than asked is normal ("until none left")
            n_sites = max(1, len(scfg.get("sites") or [1]))
            results = results_override or max(1, math.ceil(
                (cap - written) / ((len(terms) - i) * n_sites)))
            if i and scfg.get("request_delay_seconds"):
                time.sleep(scfg["request_delay_seconds"])
            print(f"=== scraping {i + 1}/{len(terms)}: {term} ===", flush=True)
            try:
                df = scrape_jobs(
                    site_name=scfg.get("sites", ["indeed"]),
                    search_term=term,
                    location=scfg.get("location", ""),
                    distance=scfg.get("radius_miles", 25),
                    hours_old=hours_old,
                    results_wanted=results,
                    country_indeed=scfg.get("country_indeed", "USA"),
                    linkedin_fetch_description=scfg.get("linkedin_fetch_description", False),
                )
            except Exception as e:
                print(f"  scrape failed for '{term}': {e}", flush=True)
                continue
            if df is None or len(df) == 0:
                continue
            df = df.where(pd.notnull(df), None)
            for _, row in df.iterrows():
                if written >= cap:
                    break
                job = {k: (None if isinstance(v, float) and math.isnan(v)
                           else v) for k, v in row.to_dict().items()}
                job["scraped_at"] = time.strftime("%Y-%m-%d")
                url = job.get("job_url") or ""
                # exclusions match job class OR employer: title + company +
                # industry ("construction" kills a construction firm's
                # Project Engineer posting, "amazon" kills all of Amazon's)
                blob = " ".join(str(job.get(k) or "") for k in
                                ("title", "company", "company_industry"))
                if not url or url in seen:
                    continue
                if any(word_match(x, blob) for x in excludes):
                    continue
                if too_old(job, hours_old):
                    continue
                seen.add(url)
                out.write(json.dumps(job, default=str, ensure_ascii=False) + "\n")
                written += 1
                print(f"  + {job.get('title', '?')[:50]} @ {str(job.get('company', ''))[:28]}",
                      flush=True)
            # persist after EVERY term: a crash mid-run must not make the
            # next run re-scrape (and later double-score) finished terms.
            # jobs.jsonl MUST hit disk first: if seen.json survived a hard
            # kill but the buffered job lines didn't, those jobs would be
            # skipped forever without ever being scored
            out.flush()
            os.fsync(out.fileno())
            paths.SEEN.write_text(json.dumps(sorted(seen)))
    print(f"DONE: {written} new jobs -> output/jobs.jsonl", flush=True)
    return written
