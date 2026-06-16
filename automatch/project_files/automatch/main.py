"""automatch CLI.

  python -m automatch run            scrape -> score -> rank
  python -m automatch run -r 5       quick test (5 results per search term;
                                     -r only affects the scrape step)
  python -m automatch score-only     score unscored jobs, then rank
  python -m automatch bot            the Discord resume-builder (advanced)
  python -m automatch rank-only      re-rank what's already scored (instant;
                                     use after changing level prefs/threshold)
"""
from __future__ import annotations

import argparse
import re

import yaml

from . import paths, rank, score, scrape


def load_yaml(p):
    return yaml.safe_load(p.read_text())


PROFILE_FIELDS = {"search_terms", "location", "radius_miles", "max_jobs",
                  "max_listing_age_hours", "level", "salary_min", "exclude",
                  "threshold", "wildcard", "vectors"}


def _die(msg: str):
    raise SystemExit(f"config/profile.yaml problem: {msg}")


def _money(v, key):
    """Forgive human salary spellings: 60000, '60,000', '$60k' -> float."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(",", "").replace("$", "")
    mult = 1000 if s.endswith("k") else 1
    try:
        return float(s.rstrip("k")) * mult
    except ValueError:
        _die(f"{key}: '{v}'; use a plain yearly number like 60000 (or $60k)")


def apply_profile(cfg: dict) -> dict:
    """Overlay config/profile.yaml (the one file users edit) onto cfg.
    Blank/missing fields keep config.yaml defaults; human-typed values are
    normalized; anything unusable dies HERE with a plain message instead of
    crashing (or silently misfiltering) deep in the pipeline."""
    if not paths.PROFILE.exists():
        return cfg
    try:
        prof = load_yaml(paths.PROFILE)
    except yaml.YAMLError as e:
        _die(f"not valid YAML. {e}")
    if prof is None:
        return cfg
    if not isinstance(prof, dict):
        _die("expected 'field: value' lines, not a list or bare text")
    for k in prof:
        if k not in PROFILE_FIELDS:
            print(f"  ! ignoring unknown profile.yaml field '{k}'", flush=True)

    terms = prof.get("search_terms")
    if isinstance(terms, str):              # forgot the list dash -> one term
        terms = [terms]
    if terms:
        cfg["scrape"]["search_terms"] = [str(t) for t in terms]
    if prof.get("location") not in (None, ""):
        cfg["scrape"]["location"] = str(prof["location"])
    for pk, ck in (("radius_miles", "radius_miles"), ("max_jobs", "max_jobs"),
                   ("max_listing_age_hours", "hours_old")):
        if prof.get(pk) not in (None, ""):
            try:
                cfg["scrape"][ck] = int(prof[pk])
            except (TypeError, ValueError):
                _die(f"{pk}: '{prof[pk]}'; use a plain number")

    level = prof.get("level")
    if isinstance(level, list):
        if len(level) != 1:
            _die("level: pick ONE: entry, senior, or blank for the default")
        level = level[0]
    if level not in (None, ""):
        lv = str(level).lower().replace("only", "").strip(" .")
        if lv == "default":
            pass                            # same as leaving it blank
        elif lv in ("entry", "senior"):
            cfg["score"]["level_filter"] = [lv]
        else:
            _die(f"level: '{level}'. three options: entry (entry only), "
                 "senior (senior only), or leave blank for the default mix")

    val = _money(prof.get("salary_min"), "salary_min")
    if val is not None:
        cfg["score"]["salary_min"] = val

    wc = prof.get("wildcard")
    if wc not in (None, ""):
        cfg["score"]["wildcard"] = str(wc).strip()

    th = prof.get("threshold")
    if th not in (None, ""):
        try:
            th = float(str(th).rstrip("%"))
            if th > 1:          # someone wrote 60 meaning 60%
                th = th / 100
            assert 0 <= th <= 1
        except (TypeError, ValueError, AssertionError):
            _die(f"threshold: '{prof.get('threshold')}': a number between "
                 "0 and 1, like 0.6")
        cfg["score"]["threshold"] = th

    excl = prof.get("exclude")
    if isinstance(excl, str):               # 'construction, gambling'
        excl = excl.split(",")
    if excl:
        excl = [re.sub(r"^and\s+", "", str(x).strip(), flags=re.I)
                .strip("'\"").strip().lower() for x in excl]
        cfg["scrape"]["exclude"] = [x for x in excl if x]

    vecs = prof.get("vectors")
    if vecs is not None:
        if not isinstance(vecs, dict) or not vecs:
            _die("vectors: should be named blocks, each with weight, "
                 "question and anchors")
        for name, v in vecs.items():
            name = str(name)
            if not re.fullmatch(r"[a-z0-9_]+", name) \
                    or name in ("level", "keyword_candidates"):
                _die(f"vectors.{name}: names are lowercase letters, numbers "
                     "and _ only; they can't be 'level' or "
                     "'keyword_candidates' (reserved)")
            if not isinstance(v, dict) or "question" not in v \
                    or not isinstance(v.get("anchors"), dict) \
                    or not v.get("anchors"):
                _die(f"vectors.{name}: needs a question and anchors lines")
            for k in v["anchors"]:
                try:
                    float(k)
                except (TypeError, ValueError):
                    _die(f"vectors.{name}: anchor levels must be numbers "
                         f"like 0.0–1.0, got '{k}'")
            try:
                w_ok = float(v.get("weight", 1.0)) > 0
            except (TypeError, ValueError):
                w_ok = False
            if not w_ok:
                _die(f"vectors.{name}: weight must be a number above 0 "
                     f"(got '{v.get('weight')}')")
        cfg["vectors"] = vecs
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(prog="automatch")
    ap.add_argument("command", choices=["run", "score-only", "rank-only", "bot"])
    ap.add_argument("-r", "--results", type=int, default=None,
                    help="override results_wanted per search term (quick tests)")
    args = ap.parse_args()

    cfg = apply_profile(load_yaml(paths.CONFIG))
    vectors = cfg.pop("vectors", None)
    if vectors is None and paths.VECTORS.exists():      # legacy fallback
        vectors = (load_yaml(paths.VECTORS) or {}).get("vectors")
    if not vectors:
        raise SystemExit(
            "No scoring vectors. Add a 'vectors:' section to config/profile.yaml")

    if args.command == "bot":
        from . import bot       # discord.py lives only in the advanced image
        bot.run(cfg, vectors)
        return

    if args.command in ("run", "score-only") and not paths.RESUME.exists():
        raise SystemExit(
            "No resume found. Put your resume as plain text in config/resume.txt")

    lock = paths.PipelineLock()
    try:
        lock.acquire()
        if args.command == "run":
            scrape.run(cfg, args.results)
        if args.command in ("run", "score-only"):
            score.run(cfg, vectors)
        rank.run(cfg, vectors)
    finally:
        lock.release()
