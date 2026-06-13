"""bot.py: Discord bot (ADVANCED), the beat-the-ATS resume builder.

Commands (owner-only):
  !match     scrape + score + rank, then the resume builder
  !scrape    scrape + score + rank only; posts the top list
  !build     resume builder on the ALREADY scraped + rated jobs:
             per job, click the skill button you have, or Skip
  !tag       show / add / remove approved skills
  !model     show / switch the judge model stack (downloads + saves it)
  !jobs      show / cap how many jobs each run scores (lower on a slow PC)
  !commands  help

Decisions persist: answered jobs are never re-asked (output/
resume_choices.json), confirmed skills live in config/approvedskills.txt
and auto-apply when future jobs want them.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import discord
import requests

from . import paths, rank, scrape, score, tailor

TOP_N = 10
REPLY_TIMEOUT = 600          # seconds to wait for a button click

# --- switchable judge stack: change the scoring model live from Discord ----
# Tradeoffs are about the HOST machine running ollama: a bigger judge scores
# the rubric more sharply but needs more RAM/VRAM and runs slower. On a weak
# PC, pick a small judge AND cap jobs per run with !jobs (scoring is minutes
# per job there). Any exact ollama tag also works; this is just the curated
# shortlist shown by !model.
JUDGE_MODELS = [
    ("llama3.2:3b",  "~2 GB. Runs on almost any PC (4-8 GB RAM, no GPU) and "
                     "stays fast. Judgment is a little blunter. Best for "
                     "old/slow machines."),
    ("qwen2.5:7b",   "~5 GB. Wants ~8 GB RAM. Strong reasoning for its size; "
                     "a solid middle ground."),
    ("mistral-nemo", "~7 GB. Wants ~8 GB+ RAM or a GPU. Best scoring quality "
                     "(the default). Slow on weak machines."),
]


def _yaml_set(key: str, value: str, path=None) -> int:
    """Replace `key: value` on its line in a yaml file, in place, keeping
    indentation and any trailing comment. Used for the flat, unique keys
    `judge` and `max_jobs` (each appears once in the file). Returns how many
    lines were replaced (0 if the key wasn't present)."""
    path = path or paths.CONFIG
    if not path.exists():
        return 0
    text = path.read_text()

    def repl(m: re.Match) -> str:
        comment = (m.group("comment") or "").rstrip()
        tail = "  " + comment if comment else ""
        return f"{m.group('indent')}{key}: {value}{tail}"

    new, n = re.subn(
        rf"(?m)^(?P<indent>\s*){re.escape(key)}:(?P<val>[^#\n]*)"
        r"(?P<comment>#[^\n]*)?$", repl, text, count=1)
    if n:
        path.write_text(new)
    return n


def _pull_model(host: str, model: str) -> None:
    """Download a model into ollama, blocking until done. ollama returns 200
    even for an unknown model (with an 'error' field), so check for that."""
    r = requests.post(f"{host}/api/pull",
                      json={"name": model, "stream": False}, timeout=3600)
    r.raise_for_status()
    body = r.json() if r.content else {}
    if body.get("error"):
        raise RuntimeError(body["error"])


def _top_jobs() -> list[dict]:
    """Top matches joined with their full descriptions from the raw scrape."""
    if not paths.MATCHES_JSON.exists():
        return []
    data = json.loads(paths.MATCHES_JSON.read_text())["matches"]
    rows = data[:TOP_N] + [r for r in data[TOP_N:] if r.get("wildcard")]
    desc = {j.get("job_url") or "": j.get("description") or ""
            for j in paths.read_jsonl(paths.JOBS)}
    for r in rows:
        r["description"] = desc.get(r["url"], "")
    return rows


def _choices() -> dict:
    """Per-job decisions: url -> keyword string ('' = skipped)."""
    try:
        return json.loads(paths.CHOICES.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _choices_save(c: dict) -> None:
    paths.CHOICES.write_text(json.dumps(c, indent=1, ensure_ascii=False))


async def _send(ch, text: str):
    """Discord caps messages at 2000 chars; chunk long ones."""
    while text:
        await ch.send(text[:1900])
        text = text[1900:]


class KeywordView(discord.ui.View):
    """Numbered keyword buttons + Skip: click instead of typing."""

    def __init__(self, cands: list[str], owner_id):
        super().__init__(timeout=REPLY_TIMEOUT)
        self.choice = None          # keyword, "" for skip, None = timeout
        self.owner_id = owner_id
        self.message = None
        for n, kw in enumerate(cands, 1):
            self._add(f"{n}: {kw[:74]}", kw, discord.ButtonStyle.primary)
        self._add("Skip (none of these)", "", discord.ButtonStyle.secondary)

    def _add(self, label: str, value: str, style):
        btn = discord.ui.Button(label=label[:80], style=style)

        async def cb(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.defer()    # not your session
                return
            self.choice = value
            for c in self.children:
                c.disabled = True
            await inter.response.edit_message(view=self)
            self.stop()

        btn.callback = cb
        self.add_item(btn)

    async def on_timeout(self):
        if self.message:
            for c in self.children:
                c.disabled = True
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


HELP = ("**commands**\n"
        "`!match`: scrape + score + rank, then the resume builder\n"
        "`!scrape`: scrape + score + rank only; posts the top list\n"
        "`!build`: resume builder on the already scraped + rated jobs "
        "(do-you-have-this-skill, one at a time)\n"
        "`!tag`: show your approved skills\n"
        "`!tag <skill>`: add a skill (auto-applies, never asked again)\n"
        "`!tag remove <skill>`: forget one\n"
        "`!model`: show the judge-model stack and what each is good for\n"
        "`!model <name>`: switch the scoring model (downloads it, saves it)\n"
        "`!switchmodel <name>` (alias `!switch`): same as `!model <name>`\n"
        "`!jobs`: show the per-run job cap\n"
        "`!jobs <n>`: cap jobs per run (lower on a slow PC)\n"
        "`!commands`: this list")


def run(cfg: dict, vectors: dict) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN missing from .env; rerun python3 setup.py")
    tailor.template()            # fail fast if the <tag> template isn't ready

    intents = discord.Intents.default()
    intents.message_content = True    # privileged: enable it in the dev portal
    client = discord.Client(intents=intents)
    busy = {"on": False}
    env_uid = os.environ.get("DISCORD_USER_ID", "").strip()
    owner = {"id": int(env_uid) if env_uid.isdigit() else None}

    async def _target():
        cid = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
        if cid:
            return await client.fetch_channel(int(cid))
        uid = os.environ.get("DISCORD_USER_ID", "").strip()
        if uid:
            return await (await client.fetch_user(int(uid))).create_dm()
        raise SystemExit("set DISCORD_USER_ID or DISCORD_CHANNEL_ID in .env")

    async def refresh(ch):
        """scrape -> score -> rank; returns the top jobs (or [])."""
        await ch.send("on it: scraping fresh postings...")
        lock = paths.PipelineLock()
        try:
            if not await asyncio.to_thread(lock.try_acquire):
                await ch.send("another automatch run is in progress; "
                              "waiting for it to finish...")
                await asyncio.to_thread(lock.wait)
            new = await asyncio.to_thread(scrape.run, cfg, None)
            await ch.send(f"{new} new postings. scoring against your rubric "
                          "(the slow part)...")
            await asyncio.to_thread(score.run, cfg, vectors)
            await asyncio.to_thread(rank.run, cfg, vectors)
        finally:
            lock.release()
        jobs = _top_jobs()
        if not jobs:
            await ch.send("no matches survived your filters; widen the "
                          "profile and try again")
            return []
        await _send(ch, f"**top {len(jobs)} matches**\n" + "\n".join(
            f"`{i}.` [{r['score']}] {r['title'][:80]} @ {str(r['company'])[:40]}"
            for i, r in enumerate(jobs, 1)))
        return jobs

    async def walkthrough(ch, jobs):
        """The do-you-have-this-skill loop: one job at a time, answers
        remembered, approved skills auto-applied."""
        tmpl = tailor.template()
        choices = _choices()
        asked = 0

        async def finish(job, i, choice, note):
            slot = "W1" if job.get("wildcard") else f"J{i:02d}"
            p = await asyncio.to_thread(tailor.save, job, choice, slot)
            choices[job.get("url") or ""] = choice or ""
            _choices_save(choices)
            await ch.send(f"{note}: `output/resumes/{slot}/{p.name}`",
                          file=discord.File(str(p)))
        for i, job in enumerate(jobs, 1):
            url = job.get("url") or ""
            tag = "🃏 WILD CARD" if job.get("wildcard") else f"#{i}"
            score_bits = [f"exp={job.get('level') or '?'}",
                          f"cos={float(job.get('cosine') or 0):.2f}"]
            score_bits += [f"{k}={float(v):.2f}"
                           for k, v in (job.get("vectors") or {}).items()]
            head = (f"---\n**{tag}: {job['title'][:80]} @ "
                    f"{str(job['company'])[:40]}**\n{job['url']}\n"
                    f"score **{job.get('score')}**  ·  {', '.join(score_bits)}")
            if job.get("day_to_day"):
                head += f"\n_{str(job['day_to_day'])[:300]}_"
            if url in choices:               # answered in an earlier round
                kw = choices[url] or None
                label = f"✅ **{kw}**" if kw else "⏭️ skipped"
                await ch.send(f"{head}\n{label} (already answered); resume "
                              "is in output/resumes/")
                continue
            approved = tailor.approved_list()
            cands = [c for c in (job.get("keywords") or [])
                     if isinstance(c, str) and c
                     and not tailor.on_resume(c, tmpl)]
            hit = (job.get("approved_hit") or "").strip()
            auto = (hit if hit and hit.lower() in approved else
                    next((c for c in cands if c.lower() in approved), None))
            cands = [c for c in cands if c.lower() not in approved]
            if auto:        # you already confirmed this skill once
                await ch.send(head)
                await finish(job, i, auto,
                             f"✅ **{auto}** (already approved); resume built")
                continue
            if not cands:
                await ch.send(head)
                await finish(job, i, None,
                             "✅ keyword: **null** (none to add; tag removed)")
                continue
            asked += 1
            view = KeywordView(cands, owner["id"])
            view.message = await ch.send(
                head + "\n**do you have experience with:**", view=view)
            await view.wait()
            if view.choice is None:          # timed out
                await ch.send("timed out. progress is saved; `!build` "
                              "to continue where you left off")
                return
            if view.choice == "":
                await finish(job, i, None, "⏭️ skipped; clean resume saved")
            else:
                kw = view.choice
                tailor.approved_add(kw)
                await finish(job, i, kw,
                             f"✅ **{kw}** stored + resume rebuilt with it")
        await ch.send("**done**: everything is in `output/resumes/`. "
                      "review before you apply; nothing is sent anywhere."
                      + ("" if asked else " (no questions this round: all "
                         "jobs were already answered or auto-resolved)"))

    async def guarded(ch, coro):
        if busy["on"]:
            await ch.send("already on a run; hang tight")
            return
        busy["on"] = True
        try:
            await coro
        except Exception as e:          # one bad send must not kill the bot
            try:
                await ch.send(f"⚠️ run failed: {type(e).__name__}: "
                              f"{str(e)[:300]}")
            except Exception:
                pass
        finally:
            busy["on"] = False

    async def do_match(ch):
        jobs = await refresh(ch)
        if jobs:
            await walkthrough(ch, jobs)

    async def do_build(ch):
        jobs = _top_jobs()
        if not jobs:
            await ch.send("nothing scraped + rated yet; `!scrape` (or "
                          "`!match`) first")
            return
        await walkthrough(ch, jobs)

    async def do_scrape(ch):
        jobs = await refresh(ch)
        if jobs:
            await ch.send("`!build` to start the resume builder on these")

    async def do_tag(ch, arg: str):
        if not arg:
            skills = tailor.approved_raw()
            await _send(ch, "**approved skills:**\n"
                        + "\n".join(f"- {s}" for s in skills) if skills else
                        "no approved skills yet; pick some during `!build` "
                        "or add one: `!tag <skill>`")
        elif arg.lower().startswith("remove "):
            kw = arg[7:].strip()
            ok = tailor.approved_remove(kw)
            await ch.send(f"removed **{kw}**" if ok
                          else f"**{kw}** wasn't on the list")
        else:
            tailor.approved_add(arg)
            await ch.send(f"✅ **{arg}** added; jobs wanting it will get it "
                          "auto-applied, and you won't be asked about it")

    async def do_model(ch, arg: str):
        host = score.ollama_host(cfg)
        cur = cfg["models"].get("judge", "")
        if not arg:
            lines = [f"**judge model** — the host LLM that scores every job. "
                     f"current: **{cur}**", "",
                     "bigger = sharper scoring, but more RAM/VRAM and slower. "
                     "switch with `!model <name>`:", ""]
            for name, blurb in JUDGE_MODELS:
                mark = "  ← current" if name == cur else ""
                lines.append(f"**{name}**{mark}\n  {blurb}")
            lines.append("\non a slow PC, also shrink the workload: `!jobs 5`")
            await _send(ch, "\n".join(lines))
            return
        model = arg.split()[0]
        known = [n for n, _ in JUDGE_MODELS]
        if model not in known and ":" not in model and "/" not in model:
            await ch.send(f"unknown model `{model}`. pick one of "
                          + ", ".join(f"`{n}`" for n in known)
                          + ", or pass any exact ollama tag (e.g. `llama3.1:8b`).")
            return
        await ch.send(f"pulling **{model}** into ollama — the first time this "
                      "can take a few minutes (big download)...")
        try:
            await asyncio.to_thread(_pull_model, host, model)
        except Exception as e:
            await ch.send(f"⚠️ couldn't pull `{model}`: {type(e).__name__}: "
                          f"{str(e)[:200]}\njudge unchanged (still `{cur}`). "
                          "is ollama running on the host?")
            return
        _yaml_set("judge", f'"{model}"')
        cfg["models"]["judge"] = model        # also applies this session
        await ch.send(f"✅ judge model is now **{model}** (saved to config). "
                      "your next `!match` or `!scrape` uses it.")

    async def do_jobs(ch, arg: str):
        cur = cfg.get("scrape", {}).get("max_jobs", 250)
        if not arg:
            await _send(ch, f"**jobs per run:** {cur}\n"
                        "this caps how many fresh postings each run scrapes and "
                        "scores. scoring is the slow part (minutes per job on a "
                        "CPU), so lower it on a weak PC. change: `!jobs <number>` "
                        "(e.g. `!jobs 5`).")
            return
        if not arg.strip().isdigit() or int(arg.strip()) < 1:
            await ch.send("give a whole number ≥ 1, e.g. `!jobs 5`")
            return
        n = int(arg.strip())
        # profile.yaml's max_jobs OVERRIDES config.yaml's on every load
        # (main.apply_profile), so writing only config.yaml would silently
        # revert on the next restart. Persist to profile.yaml when it carries
        # the key (the wizard writes it there by default); keep config.yaml in
        # sync as the fallback for a profile that doesn't set it.
        _yaml_set("max_jobs", str(n))
        _yaml_set("max_jobs", str(n), path=paths.PROFILE)
        cfg.setdefault("scrape", {})["max_jobs"] = n
        await ch.send(f"✅ jobs per run capped at **{n}**. lower = faster runs on "
                      "a weak PC; raise it for more coverage.")

    @client.event
    async def on_ready():
        ch = await _target()
        await ch.send("automatch bot online: `!match` to scrape, rate and "
                      "match fresh jobs, `!build` to build resumes for the "
                      "current matches, `!commands` for everything")

    @client.event
    async def on_message(msg):
        if msg.author.bot:               # never take orders from other bots
            return
        if owner["id"] is None:          # first human becomes the owner
            owner["id"] = msg.author.id
        elif msg.author.id != owner["id"]:
            return
        text = msg.content.strip()
        low = text.lower()
        if low == "!match":
            await guarded(msg.channel, do_match(msg.channel))
        elif low == "!scrape":
            await guarded(msg.channel, do_scrape(msg.channel))
        elif low == "!build":
            await guarded(msg.channel, do_build(msg.channel))
        elif low in ("!commands", "!comands", "!help"):
            await _send(msg.channel, HELP)
        elif low == "!tag" or low.startswith("!tag "):
            await do_tag(msg.channel, text[4:].strip())
        elif low == "!model" or low.startswith("!model "):
            await do_model(msg.channel, text[6:].strip())
        elif low in ("!switchmodel", "!switch") or low.startswith(
                ("!switchmodel ", "!switch ")):
            await do_model(msg.channel, text.split(None, 1)[1].strip()
                           if " " in text else "")
        elif low == "!jobs" or low.startswith("!jobs "):
            await do_jobs(msg.channel, text[5:].strip())

    try:
        client.run(token)
    except discord.LoginFailure:
        raise SystemExit(
            "discord rejected the token; rerun python3 setup.py and paste a "
            "fresh one (developer portal -> Bot page -> Reset Token)")
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "MESSAGE CONTENT INTENT is turned off; enable it on the Bot page "
            "of the developer portal (Privileged Gateway Intents), then rerun")
