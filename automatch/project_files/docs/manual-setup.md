# Manual setup & details

Everything here is optional; `python3 start.py` and the wizard handle it
all interactively. This page is for people who prefer doing it by hand.

## Prerequisites

- **Python 3.10+** (runs `start.py`/`setup.py` on the host):
  `winget install Python.Python.3.12` (Windows) or
  `sudo apt install python3` (Linux). Then run `python3 start.py`.
  Windows gotcha: a bare "Python was not found" pointing at the Microsoft
  Store means `python3` is hitting the App-execution-alias stub, not a real
  Python; install with the command above, then use `python start.py`.
- **Docker**: Windows: Docker Desktop → https://docs.docker.com/desktop/
  Linux: `sudo apt install docker.io docker-compose-v2`, then
  `sudo usermod -aG docker $USER` and log out/in.
- **Ollama**: https://ollama.com/download. Then:
  `ollama pull nomic-embed-text` and `ollama pull mistral-nemo`

**No GPU?** Still works: Ollama falls back to CPU, just slower. Pull a
small judge model (`ollama pull llama3.2:3b`) and set `judge: llama3.2:3b`
in `config/config.yaml`, and lower `max_jobs` in profile.yaml until you
know how fast your machine chews through them.

## Manual setup (instead of the wizard)

```bash
cp config/profile.example.yaml config/profile.yaml   # then edit it
# your resume as plain text -> config/resume.txt
mkdir -p output
env UID=$(id -u) GID=$(id -g) \
  docker compose run --rm automatch run -r 5         # small test run
```
(`start.py` and the `automatch` launcher set UID/GID automatically; the
`env` prefix only matters for hand-run compose commands on accounts whose
uid isn't 1000. Plain `export UID GID` doesn't work: bash never defines
GID.)

## Linux + Docker note

Ollama must listen on all interfaces or the container can't reach it
(`Connection refused`). One-time fix (the wizard offers to run this):
```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0"\n' | sudo tee /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
```
(Docker Desktop on Windows/Mac needs no change.)

## No Docker?

Runs as plain Python too:
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m core run -r 5
```
(`AUTOMATCH_CONFIG` / `AUTOMATCH_OUTPUT` env vars relocate the data dirs.)

## Advanced mode details

The Discord bot is a listener: message it `!match` and it scrapes fresh
postings, scores them against your rubric, ranks them, then walks your
top 10 one at a time. Keyword candidates are generated DURING scoring
(same model call: no extra cost): up to 3 ATS skills each job clearly
wants that aren't on your resume yet. Per job the bot asks
"do you have experience with:" and shows clickable buttons: click the
skill you have, or Skip. You are the honesty check. Confirmed skills are remembered in
`config/approvedskills.txt`: you're never asked about them again, and
they auto-apply when a future job wants them. The picked keyword is
swapped into your resume's `<tag>` and the per-job copy lands in
`output/resumes/J01..J10/resume.docx` (W1 for the wild card; a real
.docx with your formatting when you uploaded one; named plainly so it
uploads to applications as-is, with a job.txt identifying the posting)
and in the chat. Nothing is auto-submitted anywhere. At the end of
setup the bot DMs you "I'm alive" so you know exactly where that
conversation lives.

Bot commands: `!match` (full run + resume questions), `!scrape` (full run,
list only), `!build` (resume questions on existing matches), `!tag` /
`!tag <skill>` / `!tag remove <skill>` (view / add / forget approved
skills), `!commands` (help).

Data notes: `exclude` words match whole words only (`int` won't hide
"Maintenance") against title + company + industry; but Indeed rarely
fills the industry field, so company-type excludes work best by company
name. Listed salaries are sparse on LinkedIn; the salary floor only drops
jobs that LIST pay below it. Postings without a date age from the day
they were first scraped.

Needs: your resume with a `<tag>` marker in the skills line (sample:
`docs/resume_tag.example.docx`) and a free Discord bot's keys in
`.env`; the wizard steps through the developer portal one screen at a
time, including the required MESSAGE CONTENT intent and DM settings.
Start it:

```bash
docker compose --profile advanced run --rm bot
```

Basic installs never see any of this: the bot has its own image stage
(`discord.py` is installed only in that stage) behind a compose
profile.

## Files

```
start.py              THE entry point: wizard on first run, daily driver after
setup.py              interview-style setup wizard: writes profile.yaml,
                      places your resume, checks docker/ollama/models
config/profile.yaml   THE user file: titles, location, level, salary floor,
                      exclusions + YOUR metrics (question + anchors + weight)
config/config.yaml    system internals: weights, model names, scraper plumbing
config/resume.txt     the resume jobs are matched against
config/resume_template.txt  ADVANCED: resume with <tag> in the skills line
docs/resume_tag.example.docx  sample resume showing the format
.env / .env.example   discord keys for advanced mode (compose env_file);
                      .env is gitignored; secrets never enter the image
output/jobs.jsonl     raw scraped postings (append-only, deduped by seen.json)
output/scores.jsonl   per-job scores (cosine + each vector + level + final)
output/matches.html   the ranked, clickable result
```

## Nonstandard Ollama (remote host / different port)

Set `OLLAMA_HOST` in two places: export it before running `setup.py`
(the wizard's checks and spellfix honor it), and add an
`OLLAMA_HOST=http://your-host:port` line to `.env` so the container
picks it up. The default assumes Ollama on this machine at 11434.
