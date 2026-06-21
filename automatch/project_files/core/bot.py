"""bot.py: Discord bot (ADVANCED), the beat-the-ATS resume builder.

Commands (owner-only):
  !match     scrape + score + rank, then the resume builder
  !scrape    scrape + score + rank only; posts the top list
  !score     re-score the ALREADY scraped jobs (no scrape) + rank
  !build     resume builder on the ALREADY scraped + rated jobs:
             per job, click the skill button you have, or Skip
  !model     show / switch the local scoring model (1-4)
  !tag       show / add / remove approved skills
  !pause     stop the current run; progress is saved
  !kill      shut down the bot (every instance on this token); restart with automatch
  !edit      how to change config (metrics, search, resume, ...) in the terminal
  !commands  help

Decisions persist: answered jobs are never re-asked (output/
resume_choices.json), confirmed skills live in config/approvedskills.txt
and auto-apply when future jobs want them.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

import discord
import requests

from . import paths, rank, scrape, score, tailor

TOP_N = 10
REPLY_TIMEOUT = 600          # seconds to wait for a button click

# the judge models setup's picker offers; !model <n> switches between them
JUDGE_TIERS = ["llama3.1:8b", "mistral-nemo", "qwen2.5:14b", "qwen2.5:32b"]


def _write_judge(model: str) -> None:
    """Persist the chosen judge to config.yaml so it survives a bot restart."""
    try:
        lines = paths.CONFIG.read_text().splitlines()
    except OSError:
        return
    for i, line in enumerate(lines):
        if line.strip().startswith("judge:"):
            indent = line[:len(line) - len(line.lstrip())]
            lines[i] = f'{indent}judge: "{model}"'
            paths.CONFIG.write_text("\n".join(lines) + "\n")
            return


def _model_pulled(host: str, model: str) -> bool:
    try:
        tags = requests.get(f"{host}/api/tags",
                            timeout=10).json().get("models", [])
    except (requests.RequestException, ValueError):
        return False
    base = model.split(":")[0]
    return any(m.get("name") == model or m.get("name", "").split(":")[0] == base
               for m in tags)


def _pull(host: str, model: str) -> None:
    # stream=False: one blocking call that returns when the pull completes
    requests.post(f"{host}/api/pull", json={"model": model, "stream": False},
                  timeout=None).raise_for_status()


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


class ConfirmView(discord.ui.View):
    """Owner-only yes/no confirm for a destructive action (e.g. !flush)."""

    def __init__(self, owner_id):
        super().__init__(timeout=60)
        self.confirmed = False      # True only when the owner clicks the danger button
        self.owner_id = owner_id
        self.message = None
        self._add("Flush everything", True, discord.ButtonStyle.danger)
        self._add("Cancel", False, discord.ButtonStyle.secondary)

    def _add(self, label: str, value: bool, style):
        btn = discord.ui.Button(label=label, style=style)

        async def cb(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.defer()    # not your prompt
                return
            self.confirmed = value
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
        "`!score`: re-score the already-scraped jobs (no new scrape) + rank; "
        "use after changing your metrics or resume\n"
        "`!build`: resume builder on the already scraped + rated jobs "
        "(do-you-have-this-skill, one at a time)\n"
        "`!tag`: show your approved skills\n"
        "`!tag <skill>`: add a skill (auto-applies, never asked again)\n"
        "`!tag remove <skill>`: forget one\n"
        "`!model` / `!model 1-4`: show or switch the local scoring model "
        "(re-scores on your next `!scrape`)\n"
        "`!pause` (or `!stop`): stop the current scrape/score run; progress "
        "is saved and `!scrape` resumes it\n"
        "`!kill`: shut down the bot, every instance on this token; restart "
        "with `automatch`\n"
        "`!flush`: wipe ALL scraped jobs, scores and matches for a clean "
        "slate (your profile + approved skills are kept); `!scrape` then "
        "starts fresh\n"
        "`!edit` (or `!metric`): how to change your metrics, job search, "
        "Discord login, or resume (done in the terminal)\n"
        "`!commands`: this list\n"
        "\n"
        "**in the terminal** (where you launched the bot)\n"
        "`automatch`: start this bot; keep that window open while you use it. "
        "run it again anytime to restart (no setup needed)\n"
        "`python3 edit.py`: change metrics, job search, Discord login, or "
        "resume\n"
        "`python3 setup.py`: redo the one-time setup (scoring rubric, model, "
        "job search, or Discord settings)")


def _reload_profile():
    """Re-read config.yaml + profile.yaml from disk so edits made while the bot
    is running (metrics via edit_metrics.py, threshold, search terms, the
    persisted judge) apply on the next !scrape with NO restart. Returns
    (cfg, vectors), or None if the profile is missing/invalid -- the caller then
    keeps the last good settings instead of letting a bad edit crash the bot."""
    try:
        from . import main           # lazy: avoids a circular import at load
        cfg = main.apply_profile(main.load_yaml(paths.CONFIG))
        vectors = cfg.pop("vectors", None)
        if vectors is None and paths.VECTORS.exists():
            vectors = (main.load_yaml(paths.VECTORS) or {}).get("vectors")
        if not vectors:
            return None
        return cfg, vectors
    except (SystemExit, Exception):   # _die() raises SystemExit on a bad profile
        return None


def run(cfg: dict, vectors: dict) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN missing from .env; rerun python3 setup.py")
    tailor.template()            # fail fast if the <tag> template isn't ready

    intents = discord.Intents.default()
    intents.message_content = True    # privileged: enable it in the dev portal
    client = discord.Client(intents=intents)
    busy = {"on": False}
    cancel = threading.Event()       # set by !pause; read inside the worker
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

    async def refresh(ch, scrape_first: bool = True):
        """scrape -> score -> rank (score -> rank when scrape_first is False);
        returns the top jobs (or [])."""
        nonlocal cfg, vectors
        cancel.clear()                  # a stale !pause must not kill this run
        fresh = await asyncio.to_thread(_reload_profile)
        if fresh:                       # adopt profile.yaml edits, no restart
            cfg, vectors = fresh
        else:
            await ch.send("note: config/profile.yaml didn't reload (missing or "
                          "mid-edit?); using the settings from bot startup")
        await ch.send("on it: scraping fresh postings..." if scrape_first
                      else "on it: re-scoring your already-scraped jobs "
                           "(no new scrape)...")
        lock = paths.PipelineLock()
        if not await asyncio.to_thread(lock.try_acquire):
            # never block forever: a terminal closed mid-run can leave a
            # container alive still holding the lock, which used to wedge the
            # bot silently ("stuck mid-scrape"). Wait a few seconds, then bail
            # with an actionable message instead of hanging.
            await ch.send("another automatch run is already going; giving it "
                          "a moment...")
            for _ in range(10):
                await asyncio.sleep(1)
                if await asyncio.to_thread(lock.try_acquire):
                    break
            else:
                lock.release()
                await ch.send("still locked. if you closed a terminal mid-run, "
                              "that container may still be alive holding the "
                              "lock; stop it (`docker compose down`), then "
                              "try `!scrape` again")
                return []
        try:
            if scrape_first:
                new = await asyncio.to_thread(scrape.run, cfg, None,
                                              cancel.is_set)
                if cancel.is_set():
                    await ch.send("⏸️ paused. what scraped so far is saved; "
                                  "`!scrape` picks up where it left off")
                    return []
                await ch.send(f"{new} new postings. scoring against your "
                              "rubric (the slow part)...")
            else:
                await ch.send("scoring against your rubric (the slow part)...")
            await asyncio.to_thread(score.run, cfg, vectors, cancel.is_set)
            if cancel.is_set():
                await ch.send("⏸️ paused mid-scoring. scored jobs are saved; "
                              "`!scrape` resumes the rest")
                return []
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
            head = (f"---\n**{tag}: {job['title'][:80]} @ "
                    f"{str(job['company'])[:40]}**\n{job['url']}")
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

    async def do_score(ch):
        # re-evaluate jobs WITHOUT scraping: judges anything unscored under the
        # current rubric (a metric/resume edit re-judges everything), then ranks
        jobs = await refresh(ch, scrape_first=False)
        if jobs:
            await ch.send("`!build` to start the resume builder on these")

    async def do_flush(ch):
        """Wipe the scrape/score/rank pipeline for a clean slate. Keeps the
        user's profile, approved skills and answered-job memory; clearing
        seen.json is what lets the next !scrape repopulate from scratch."""
        if busy["on"]:
            await ch.send("a run is in progress; `!pause` it first, then `!flush`")
            return
        targets = [paths.JOBS, paths.SCORES, paths.SEEN,
                   paths.MATCHES_JSON, paths.MATCHES_HTML]
        present = [p for p in targets if p.exists()]
        if not present:
            await ch.send("pipeline is already empty; nothing to flush")
            return
        njobs = (sum(1 for _ in paths.read_jsonl(paths.JOBS))
                 if paths.JOBS.exists() else 0)
        view = ConfirmView(owner["id"])
        view.message = await ch.send(
            f"⚠️ **flush the pipeline?** this deletes all scraped jobs "
            f"({njobs}), their scores, the seen-list and the current matches "
            "for a clean slate. your profile, approved skills and answered "
            "jobs are kept. `!scrape` afterward starts fresh.", view=view)
        await view.wait()
        if not view.confirmed:
            await ch.send("flush cancelled; nothing was removed")
            return
        removed = []
        for p in present:
            try:
                p.unlink()
                removed.append(p.name)
            except OSError as e:
                await ch.send(f"couldn't remove {p.name}: {type(e).__name__}")
        await ch.send("🧹 flushed: " + ", ".join(removed) +
                      "\n`!scrape` (or `!match`) for a fresh run")

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

    async def do_model(ch, n):
        model = JUDGE_TIERS[n - 1]
        if model == str(cfg["models"].get("judge")):
            await ch.send(f"already using **{model}**")
            return
        host = score.ollama_host(cfg)
        if not await asyncio.to_thread(_model_pulled, host, model):
            await ch.send(f"downloading **{model}** (one-time; can take a few "
                          "minutes)...")
            try:
                await asyncio.to_thread(_pull, host, model)
            except Exception as e:
                await ch.send(f"couldn't download {model}: {type(e).__name__}; "
                              "model unchanged")
                return
        cfg["models"]["judge"] = model      # propagate to THIS running session
        _write_judge(model)                 # and persist for the next start
        await ch.send(f"✅ scoring model is now **{model}**. your next "
                      "`!scrape` re-scores your jobs with it")

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
        elif low == "!score":
            await guarded(msg.channel, do_score(msg.channel))
        elif low == "!build":
            await guarded(msg.channel, do_build(msg.channel))
        elif low in ("!pause", "!stop"):
            if busy["on"]:
                cancel.set()
                await msg.channel.send("⏸️ pausing once the current step "
                                       "finishes; everything so far is saved")
            else:
                await msg.channel.send("nothing is running right now")
        elif low == "!kill":
            # every bot connected to this token receives this message, so each
            # one shutting ITSELF down takes them all down (the only way from
            # inside the container, which has no docker access). --rm drops each
            # container as it exits.
            await msg.channel.send(
                "🛑 shutting down. every bot on this token gets this, so all "
                "instances stop. run `automatch` for a fresh single one.")
            cancel.set()              # let any in-progress run stop cleanly
            await client.close()      # ends this instance
        elif low == "!flush":
            await do_flush(msg.channel)
        elif low == "!model" or low.startswith("!model "):
            arg = text[6:].strip()
            cur = str(cfg["models"].get("judge", "?"))
            if not arg:
                lst = "\n".join(
                    f"`{i}.` {m}" + ("  <- current" if m == cur else "")
                    for i, m in enumerate(JUDGE_TIERS, 1))
                await _send(msg.channel, f"**scoring model** (now: {cur})\n"
                            f"{lst}\nsend `!model 1`-`4` to switch")
            elif arg in ("1", "2", "3", "4"):
                if busy["on"]:
                    await msg.channel.send("a run is in progress; `!pause` "
                                           "first, then switch models")
                else:
                    await guarded(msg.channel, do_model(msg.channel, int(arg)))
            else:
                await msg.channel.send("use `!model 1`-`4` (or `!model` to "
                                       "see the options)")
        elif low in ("!commands", "!comands", "!help"):
            await _send(msg.channel, HELP)
        elif (low in ("!metric", "!metrics", "!edit", "!search", "!resume",
                      "!discord")
              or low.startswith(("!metric ", "!edit "))):
            await msg.channel.send(
                "to change your config, run `python3 edit.py` in the terminal "
                "(the project folder): scoring metrics, job search, Discord "
                "login, or resume. metric + search edits apply on your next "
                "`!scrape` (no restart); a Discord-login change needs a restart.")
        elif low == "!tag" or low.startswith("!tag "):
            await do_tag(msg.channel, text[4:].strip())

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
