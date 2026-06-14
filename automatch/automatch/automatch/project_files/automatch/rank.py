"""rank.py: sort scored jobs, write matches.json + a clickable matches.html."""
from __future__ import annotations

import html
import json
import math
from datetime import datetime

from . import paths
from .paths import read_jsonl
from .scrape import dedupe_key, too_old, word_match
from .score import current_rub

# pay periods -> multiplier to a yearly figure (40h weeks for hourly)
PER_YEAR = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": 260, "hourly": 2080}


def _salary_bounds(job: dict) -> tuple[float, float] | None:
    """A posting's listed pay as yearly (low, high); None when the posting
    lists nothing usable (no amounts, $0 placeholders, unknown pay period):
    when in doubt the job is NOT salary-filtered."""
    vals = []
    for v in (job.get("min_amount"), job.get("max_amount")):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isnan(f) and f > 0:
            vals.append(f)
    if not vals:
        return None
    mult = PER_YEAR.get(str(job.get("interval") or "").lower())
    if mult is None:
        return None
    return min(vals) * mult, max(vals) * mult


def _job_index() -> dict:
    """url -> (yearly pay bounds or None, 'title company industry' text) from
    the raw scrape; rank-time filters always see fresh raw-posting data."""
    idx = {}
    for j in read_jsonl(paths.JOBS):
        text = " ".join(str(j.get(k) or "") for k in
                        ("title", "company", "company_industry")).lower()
        idx[j.get("job_url") or ""] = (_salary_bounds(j), text)
    return idx


def _fmt_pay(b) -> str:
    """(110700.0, 218300.0) -> '$110,700–$218,300' for the html table."""
    if not b:
        return "-"
    lo, hi = (int(round(x)) for x in b)
    return f"${lo:,}" if lo == hi else f"${lo:,}–${hi:,}"


def run(cfg: dict, vectors: dict) -> int:
    rows = list(read_jsonl(paths.SCORES))
    scfg = cfg["score"]

    # only rank rows judged under the CURRENT rubric + resume; rows from
    # an older rubric are invisible here and re-judged by score.py, so a
    # ranking can never silently mix incomparable scores
    rub = current_rub(cfg, vectors)
    if rub is not None:
        before = len(rows)
        rows = [r for r in rows if r.get("rub") == rub]
        if len(rows) < before:
            print(f"  rubric filter: {before - len(rows)} rows from an older "
                  "rubric/resume excluded (they re-score on the next run)",
                  flush=True)

    # weights are applied HERE, not at score time; editing a weight or
    # cosine_weight re-ranks instantly with zero model calls
    cw = float(scfg.get("cosine_weight", 1.0))
    weights = {n: float((v or {}).get("weight", 1.0)) for n, v in vectors.items()}
    total_w = cw + sum(weights.values())
    for r in rows:
        vs = r.get("vectors") or {}
        try:
            r["score"] = round((cw * float(r.get("cosine") or 0.0)
                                + sum(w * float(vs.get(n) or 0.0)
                                      for n, w in weights.items()))
                               / total_w, 4)
        except (TypeError, ValueError):
            pass            # keep the stored score if a row is malformed

    # honor max_listing_age_hours in the OUTPUT too, not just at scrape time;
    # otherwise stale postings scraped under an older/wider setting linger
    hours_old = int(cfg.get("scrape", {}).get("hours_old", 0) or 0)
    if hours_old:
        fresh = [r for r in rows if not too_old(r, hours_old)]
        if len(fresh) < len(rows):
            print(f"  freshness: dropped {len(rows) - len(fresh)} postings "
                  f"older than {hours_old}h", flush=True)
        rows = fresh

    # level preference, applied at RANK time (instant to change in config):
    # level_filter set  -> HARD mode: only those levels survive at all
    # otherwise         -> SOFT mode: level_adjust nudges the score
    lfilter = [str(x).lower() for x in scfg.get("level_filter") or []]
    ladj = scfg.get("level_adjust") or {}
    if lfilter:
        rows = [r for r in rows if r.get("level") in lfilter]
    else:
        for r in rows:
            r["score"] = round(max(0.0, min(1.0,
                r["score"] + float(ladj.get(r.get("level"), 0.0)))), 4)

    idx = _job_index()

    excl = [str(x).lower() for x in cfg.get("scrape", {}).get("exclude") or []]
    if excl:
        kept = [r for r in rows if not any(
            word_match(x, idx.get(r["url"], (None, ""))[1]
                       or f"{r.get('title', '')} {r.get('company', '')}")
            for x in excl)]
        if len(kept) < len(rows):
            print(f"  exclude filter: dropped {len(rows) - len(kept)} jobs",
                  flush=True)
        rows = kept

    smin = scfg.get("salary_min")
    if smin is not None:
        kept = [r for r in rows
                if (b := idx.get(r["url"], (None, ""))[0]) is None
                or b[1] >= float(smin)]
        if len(kept) < len(rows):
            print(f"  salary filter: dropped {len(rows) - len(kept)} "
                  f"listed-pay jobs under ${int(float(smin)):,}", flush=True)
        rows = kept

    threshold = float(scfg.get("threshold", 0.0))
    rows = [r for r in rows if r["score"] >= threshold]
    rows.sort(key=lambda r: r["score"], reverse=True)

    # collapse cross-board duplicates: the same posting listed on several
    # boards has different URLs; match on title + company first word and
    # keep the best-scoring copy so top_n slots aren't wasted
    seen_keys, deduped = set(), []
    for r in rows:
        key = dedupe_key(r)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(r)
    if len(deduped) < len(rows):
        print(f"  dedupe: collapsed {len(rows) - len(deduped)} cross-board "
              "duplicate postings", flush=True)
    rows = deduped
    topn = int(scfg.get("top_n", 25))
    top = rows[:topn]
    if str(scfg.get("wildcard") or "").strip():
        pool = [r for r in rows[topn:] if r.get("wild") is not None]
        if pool:
            w = max(pool, key=lambda r: r["wild"])
            w["wildcard"] = True
            top = top + [w]
            print(f"  wild card: {w['title'][:50]} (wild {w['wild']})",
                  flush=True)

    now = datetime.now()
    paths.MATCHES_JSON.write_text(json.dumps(
        {"generated": now.isoformat(timespec="seconds"), "matches": top},
        indent=2, ensure_ascii=False))

    vec_heads = "".join(f"<th>{html.escape(n)}</th>" for n in vectors)
    trs = []
    for i, r in enumerate(top, 1):
        vec_tds = "".join(f"<td>{r['vectors'].get(n, '-')}</td>" for n in vectors)
        label = "W" if r.get("wildcard") else str(i)
        trs.append(
            f"<tr><td>{label}</td><td>{r['score']}</td>"
            f"<td><a href='{html.escape(r['url'])}' target='_blank' "
            f"title=\"{html.escape(r.get('day_to_day') or '')}\">"
            f"{html.escape(r['title'])}</a></td>"
            f"<td>{html.escape(r['company'])}</td><td>{html.escape(r['location'])}</td>"
            f"<td>{html.escape(r.get('level', '-'))}</td>"
            f"<td>{_fmt_pay(idx.get(r['url'], (None, ''))[0])}</td>"
            f"<td>{r['cosine']}</td>{vec_tds}</tr>")
    paths.MATCHES_HTML.write_text(f"""<!doctype html><meta charset="utf-8">
<title>automatch: {now:%Y-%m-%d}</title><style>
body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left}}
th{{background:#3b82f6;color:#fff}}tr:nth-child(even){{background:#f4f7fb}}
</style><h2>automatch: {len(top)} matches ({now:%Y-%m-%d %H:%M})</h2>
<table><tr><th>#</th><th>score</th><th>job</th><th>company</th><th>location</th>
<th>level</th><th>salary/yr</th><th>cosine</th>{vec_heads}</tr>{''.join(trs)}</table>""")
    print(f"DONE: {len(top)} matches -> output/matches.html", flush=True)
    return len(top)
