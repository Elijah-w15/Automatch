"""rank.py: sort scored jobs, write matches.json + a clickable matches.html."""
from __future__ import annotations

import html
import json
import math
from datetime import datetime, timedelta

from . import paths
from .paths import read_jsonl
from .scrape import dedupe_key, desc_fingerprint, too_old, word_match
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
    """url -> (yearly pay bounds or None, 'title company industry' text,
    full-description fingerprint or None) from the raw scrape; rank-time
    filters and dedupe always see fresh raw-posting data."""
    idx = {}
    for j in read_jsonl(paths.JOBS):
        text = " ".join(str(j.get(k) or "") for k in
                        ("title", "company", "company_industry",
                         "description")).lower()
        idx[j.get("job_url") or ""] = (_salary_bounds(j), text,
                                       desc_fingerprint(j.get("description")))
    return idx


def _fmt_pay(b) -> str:
    """(110700.0, 218300.0) -> '$110,700–$218,300' for the html table."""
    if not b:
        return "-"
    lo, hi = (int(round(x)) for x in b)
    return f"${lo:,}" if lo == hi else f"${lo:,}–${hi:,}"


# --- cross-day dedupe ---------------------------------------------------------
# seen.json remembers SCRAPED urls (so a job is never re-fetched); shown.json
# remembers SHOWN jobs (so a job that already headlined an earlier day's top
# list isn't re-ranked to the top while it lingers in the hours_old window).

def _shown_cutoff(today: str, hours_old: int) -> str:
    """Oldest day worth remembering: the listing-age window + 2 days of grace,
    so a posting still young enough to rank is never forgotten early (and thus
    allowed to reappear). 30 days when no window is set, just to bound the file."""
    keep_days = math.ceil(hours_old / 24) + 2 if hours_old else 30
    return (datetime.strptime(today, "%Y-%m-%d")
            - timedelta(days=keep_days)).strftime("%Y-%m-%d")


def _shown_records() -> list:
    """Raw shown.json rows (each {date,url,key,fp}); [] if missing/garbled."""
    if not paths.SHOWN.exists():
        return []
    try:
        data = json.loads(paths.SHOWN.read_text())
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def _shown_before(today: str, hours_old: int):
    """(urls, loose-keys, fingerprints) shown on a day STRICTLY BEFORE today,
    within the remembered window. Strictly-before-today is what makes re-running
    rank the same day idempotent: today's own just-shown jobs aren't suppressed,
    so tweaking weights and re-ranking never makes the list shrink to nothing."""
    cutoff = _shown_cutoff(today, hours_old)
    urls, keys, fps = set(), set(), set()
    for rec in _shown_records():
        d = str(rec.get("date") or "")
        if not d or d < cutoff or d >= today:
            continue
        if rec.get("url"):
            urls.add(rec["url"])
        if rec.get("key"):
            keys.add(tuple(rec["key"]))
        if rec.get("fp"):
            fps.add(rec["fp"])
    return urls, keys, fps


def _record_shown(top: list, idx: dict, today: str, hours_old: int) -> None:
    """Persist today's shown identities, replacing any earlier-today record (so
    repeated re-ranks the same day don't compound) and dropping rows that have
    aged past the remembered window. Atomic write, like prune()."""
    cutoff = _shown_cutoff(today, hours_old)
    kept = [rec for rec in _shown_records()
            if (d := str(rec.get("date") or "")) >= cutoff and d != today]
    for r in top:
        fp = idx.get(r["url"], (None, "", None))[2]
        kept.append({"date": today, "url": r.get("url") or "",
                     "key": list(dedupe_key(r)), "fp": fp})
    tmp = paths.SHOWN.with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, ensure_ascii=False))
    tmp.replace(paths.SHOWN)


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
            word_match(x, idx.get(r["url"], (None, "", None))[1]
                       or f"{r.get('title', '')} {r.get('company', '')}")
            for x in excl)]
        if len(kept) < len(rows):
            print(f"  exclude filter: dropped {len(rows) - len(kept)} jobs",
                  flush=True)
        rows = kept

    smin = scfg.get("salary_min")
    if smin is not None:
        kept = [r for r in rows
                if (b := idx.get(r["url"], (None, "", None))[0]) is None
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
    seen_keys, seen_fps, deduped = set(), set(), []
    for r in rows:
        key = dedupe_key(r)
        # the same posting re-listed under a different title shares no title
        # key but has an identical body; collapse on the full-description
        # fingerprint too (None for bodyless jobs, which never collapse)
        fp = idx.get(r["url"], (None, "", None))[2]
        if key in seen_keys or (fp is not None and fp in seen_fps):
            continue
        seen_keys.add(key)
        if fp is not None:
            seen_fps.add(fp)
        deduped.append(r)
    if len(deduped) < len(rows):
        print(f"  dedupe: collapsed {len(rows) - len(deduped)} duplicate "
              "postings (cross-board, or same body under a different title)",
              flush=True)
    rows = deduped

    # cross-DAY dedupe: postings live in jobs.jsonl/scores.jsonl for the whole
    # hours_old window, so without this the same jobs headline the top list for
    # days. Hide anything already shown on an EARLIER day (same url / title+
    # company / body identity as the in-run dedupe above). Done before the
    # per-company cap and top_n slice so freed slots fill with fresh postings.
    today = datetime.now().strftime("%Y-%m-%d")
    hide_shown = bool(scfg.get("hide_shown", True))
    if hide_shown:
        s_urls, s_keys, s_fps = _shown_before(today, hours_old)
        if s_urls or s_keys or s_fps:
            fresh = []
            for r in rows:
                fp = idx.get(r["url"], (None, "", None))[2]
                if ((r.get("url") or "") in s_urls
                        or dedupe_key(r) in s_keys
                        or (fp is not None and fp in s_fps)):
                    continue
                fresh.append(r)
            if len(fresh) < len(rows):
                print(f"  cross-day dedupe: hid {len(rows) - len(fresh)} "
                      "postings already shown on an earlier day "
                      "(score.hide_shown: false keeps them)", flush=True)
            rows = fresh

    # per-company cap: one employer posting several near-identical roles (e.g.
    # the same job at 4 seniority levels) shouldn't eat the whole list. rows are
    # already score-sorted, so keep each company's best N. Blank-company rows are
    # never capped (can't tell distinct postings from twins).
    maxpc = int(scfg.get("max_per_company", 0) or 0)
    if maxpc > 0:
        per_co, capped = {}, []
        for r in rows:
            co = (r.get("company") or "").strip().lower()
            if co:
                if per_co.get(co, 0) >= maxpc:
                    continue
                per_co[co] = per_co.get(co, 0) + 1
            capped.append(r)
        if len(capped) < len(rows):
            print(f"  per-company cap: dropped {len(rows) - len(capped)} "
                  f"postings beyond {maxpc} per company", flush=True)
        rows = capped

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

    # remember what we showed so tomorrow's run won't repeat it
    if hide_shown:
        _record_shown(top, idx, today, hours_old)

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
            f"<td>{_fmt_pay(idx.get(r['url'], (None, '', None))[0])}</td>"
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
