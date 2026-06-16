#!/usr/bin/env python3
"""Edit the scoring metrics in config/profile.yaml WITHOUT redoing setup.

    python3 edit_metrics.py

Lists your current metrics (name, question, levels) and lets you:
  add      one new metric          (the same wizard setup.py uses)
  remove   one metric              (by name, with confirmation)
  edit     one metric              (its name, its question, or one 0.0-1.0 level)
  replace  ALL metrics             (wipes them, runs the full build loop)

Only the `vectors:` block of profile.yaml is rewritten; every other setting
(search, threshold, wildcard, comments, and each metric's weight) is preserved.
Changes take effect on the next `!scrape` / run.
"""
import os
import re
import sys

import yaml

# import setup.py from the same folder for the shared metric wizard + helpers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup  # noqa: E402  (collect_metric, collect_metrics, ask_yn, yv, PROFILE)

ANCHOR_STEPS = setup.ANCHOR_STEPS
MARKER = "# ====== YOUR SCORING METRICS ======"


def _norm_step(k) -> str:
    """YAML reads `0.0:` as the float 0.0; normalise back to '0.0' .. '1.0'."""
    try:
        return f"{float(k):.1f}"
    except (ValueError, TypeError):
        return str(k)


def _clean_name(raw: str) -> str:
    """Same rule setup.py uses for metric names: lower, spaces -> _, a-z0-9_."""
    return re.sub(r"[^a-z0-9_]", "", raw.lower().replace(" ", "_"))


def _split_profile(lines: list):
    """Return (head_lines, tail_lines) bracketing the vectors block, so we can
    rewrite ONLY that block and keep everything before AND after it verbatim
    (some profiles put wildcard/other keys after vectors). (None, None) if
    there is no `vectors:` mapping."""
    vec = next((i for i, l in enumerate(lines) if l.rstrip() == "vectors:"), None)
    if vec is None:
        return None, None
    start = vec
    for i in range(vec - 1, -1, -1):           # absorb the marker + its comments
        s = lines[i].strip()
        if s.startswith("# ====== YOUR SCORING METRICS"):
            start = i
            break
        if s and not s.startswith("#"):
            break                              # hit a real setting above; stop
    end = len(lines)
    for j in range(vec + 1, len(lines)):       # mapping ends at the next col-0 line
        if lines[j].strip() == "":
            continue
        if not lines[j][:1].isspace():
            end = j
            break
    return lines[:start], lines[end:]


def read_profile():
    """Return (head_lines, tail_lines, metrics) or (None, None, None).
    metrics is a list of {name, weight, question, anchors{step: text}}."""
    text = setup.PROFILE.read_text()
    head, tail = _split_profile(text.split("\n"))
    if head is None:
        print("\n  couldn't find a 'vectors:' section in config/profile.yaml.")
        print("  it may be hand-edited; re-run `python3 setup.py` to rebuild it.")
        return None, None, None
    data = yaml.safe_load(text) or {}
    metrics = []
    for name, body in (data.get("vectors") or {}).items():
        body = body or {}
        anchors = {_norm_step(k): str(v)
                   for k, v in (body.get("anchors") or {}).items()}
        metrics.append({"name": str(name),
                        "weight": body.get("weight", 1),
                        "question": str(body.get("question", "")),
                        "anchors": anchors})
    return head, tail, metrics


def write_metrics(head_lines: list, tail_lines: list, metrics: list) -> None:
    """Rewrite ONLY the vectors block; keep head and tail (every other setting,
    in its original place, plus weights) verbatim."""
    block = [MARKER,
             "# question = what the AI answers; anchors = YOUR examples of",
             "# each score. weight: edit the number to make a metric count",
             "# more or less (1 = normal, 0.5 = half, 2 = double).",
             "vectors:"]
    for m in metrics:
        block += [f"  {m['name']}:",
                  f"    weight: {m['weight']}",
                  f"    question: {setup.yv(m['question'])}",
                  "    anchors:"]
        for step in ANCHOR_STEPS:
            if step in m["anchors"]:
                block.append(f"      {step}: {setup.yv(m['anchors'][step])}")
        block.append("")
    setup.PROFILE.write_text("\n".join(head_lines + block + tail_lines))


def show_metrics(metrics: list) -> None:
    if not metrics:
        print("\n  (you have no metrics yet)")
        return
    print("\n  ==== your metrics " + "=" * 44)
    for i, m in enumerate(metrics, 1):
        wt = "" if str(m["weight"]) == "1" else f"   (weight {m['weight']})"
        print(f"\n  {i}) {m['name']}{wt}")
        print(f"     question: {m['question']}")
        for step in ANCHOR_STEPS:
            if step in m["anchors"]:
                print(f"       {step}: {m['anchors'][step]}")
    print()


def _find(metrics, name):
    return next((m for m in metrics if m["name"] == name), None)


def do_add(metrics: list) -> list:
    name, question, anchors = setup.collect_metric(
        len(metrics) + 1, {m["name"] for m in metrics})
    metrics.append({"name": name, "weight": 1,
                    "question": question, "anchors": dict(anchors)})
    print(f"\n  added '{name}'.")
    return metrics


def do_remove(metrics: list) -> list:
    if not metrics:
        print("  nothing to remove.")
        return metrics
    name = input("\n  name of the metric to remove (blank to cancel): ").strip()
    if not name:
        return metrics
    if not _find(metrics, name):
        print(f"  no metric named '{name}' (names are case-sensitive).")
        return metrics
    if setup.ask_yn(f"  remove '{name}'?", "n"):
        metrics = [m for m in metrics if m["name"] != name]
        print(f"  removed '{name}'.")
    else:
        print("  kept it.")
    return metrics


def do_edit(metrics: list) -> list:
    if not metrics:
        print("  nothing to edit.")
        return metrics
    name = input("\n  name of the metric to edit (blank to cancel): ").strip()
    if not name:
        return metrics
    m = _find(metrics, name)
    if not m:
        print(f"  no metric named '{name}'.")
        return metrics
    print(f"\n  editing '{m['name']}'. what do you want to change?")
    print("   1) the name")
    print("   2) the question")
    print("   3) one score level (0.0 0.2 0.4 0.6 0.8 1.0)")
    print("   4) the weight (how much it counts: 1 = normal, 0.5 = half, "
          "2 = double)")
    choice = input("  pick 1-4 (blank to cancel): ").strip()
    if choice == "1":
        new = _clean_name(input(f"  new name (was '{m['name']}'): ").strip())
        if not new:
            print("  no change.")
        elif new != m["name"] and _find(metrics, new):
            print(f"  '{new}' is already a metric; no change.")
        elif new in ("level", "keyword_candidates"):
            print(f"  '{new}' is reserved; no change.")
        else:
            m["name"] = new
            print(f"  renamed to '{new}'.")
    elif choice == "2":
        print(f"  current question: {m['question']}")
        new = input("  new question (blank to keep): ").strip()
        if new:
            m["question"] = new
            print("  question updated.")
        else:
            print("  no change.")
    elif choice == "3":
        step = _norm_step(input("  which level? (0.0 0.2 0.4 0.6 0.8 1.0): ")
                          .strip())
        if step not in ANCHOR_STEPS:
            print("  not one of 0.0 0.2 0.4 0.6 0.8 1.0; no change.")
        else:
            print(f"  current {step}: {m['anchors'].get(step, '(none)')}")
            new = input(f"  new wording for {step} (blank to keep): ").strip()
            if new:
                m["anchors"][step] = new
                print(f"  {step} updated.")
            else:
                print("  no change.")
    elif choice == "4":
        print(f"  current weight: {m['weight']}")
        new = input("  new weight (number > 0, blank to keep): ").strip()
        if not new:
            print("  no change.")
        else:
            try:
                w = float(new)
                if w > 0:
                    m["weight"] = int(w) if w == int(w) else w
                    print(f"  weight set to {m['weight']} (re-ranks, no re-score).")
                else:
                    print("  must be greater than 0; no change.")
            except ValueError:
                print("  not a number; no change.")
    else:
        print("  cancelled.")
    return metrics


def do_replace(metrics: list) -> list:
    n = len(metrics)
    if not setup.ask_yn(f"  this REMOVES all {n} current metric(s) and starts "
                        "from scratch. proceed?", "n"):
        print("  kept your current metrics.")
        return metrics
    print("\n  building a fresh set of metrics:")
    fresh = setup.collect_metrics()        # interview_odds offer + add loop
    return [{"name": nm, "weight": 1, "question": q, "anchors": dict(a)}
            for nm, q, a in fresh]


def run_editor() -> None:
    """The add/remove/edit/replace menu loop. Returns when the user quits.
    Importable so edit.py can run it as its 'metrics' section."""
    if not setup.PROFILE.exists():
        print("  no config/profile.yaml yet. run setup first:  python3 setup.py")
        return
    head, tail, metrics = read_profile()
    if head is None:
        return
    setup._yn_taught = True   # editor users aren't first-timers; plain (y/n)
    print("=" * 66)
    print("  edit your scoring metrics")
    print("=" * 66)
    while True:
        show_metrics(metrics)
        print("  what would you like to do?")
        print("   1) add a metric")
        print("   2) remove a metric")
        print("   3) edit a metric")
        print("   4) replace ALL metrics")
        print("   5) save and quit")
        choice = input("\n  pick 1-5: ").strip().lower()
        if choice in ("1", "add"):
            metrics = do_add(metrics)
        elif choice in ("2", "remove"):
            metrics = do_remove(metrics)
        elif choice in ("3", "edit"):
            metrics = do_edit(metrics)
        elif choice in ("4", "replace"):
            metrics = do_replace(metrics)
        elif choice in ("5", "q", "quit", "exit", ""):
            break
        else:
            print("  pick a number 1-5.")
            continue
        write_metrics(head, tail, metrics)  # propagate every change immediately
        print("  (saved to config/profile.yaml)")
    print("\n  done. changes take effect on your next !scrape / run")
    print("  (the bot re-reads this file each run; no restart needed).")


def main() -> None:
    run_editor()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  cancelled; profile.yaml left as it was.")
        sys.exit(1)
