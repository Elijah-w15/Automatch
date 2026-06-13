# automatch

**What it is:** a fully local job-matching tool. It scrapes job boards
(using [JobSpy](https://github.com/Bunsly/JobSpy)), scores every posting
against YOUR resume and YOUR priorities using AI on your own machine, and
gives you a ranked, clickable list of matches. The matching runs
entirely on your machine: no accounts, no API keys for the core flow
(the optional Discord mode uses a free bot you create).

**How it works:** scrape → score (a local AI judges every job against a
rubric YOU define: your questions, your 0-to-1 examples) → rank →
`output/matches.html`.

**Optional advanced mode:** a Discord bot DMs you your top matches and
builds an ATS-targeted resume for each job: it proposes the skill
keywords each job's system scans for, and you confirm only the ones you
actually have (or skip).

## Get started

**Windows:** no commands, no terminal. Double-click
**`windows start here.bat`** and say Yes to any prompts.

**Linux:** open a terminal in this folder and run
`sh "linux start here.sh"`. It installs what's missing and runs the
same wizard.

## What it downloads

Setup installs only what you don't already have. At a high level:

- **Python 3**: the language automatch runs on; powers setup and the pipeline.
- **Docker**: runs automatch in an isolated container, kept separate from your system.
- **WSL2** (Windows only): the small Linux layer Docker Desktop runs on.
- **Ollama**: runs the AI locally, so your data never leaves your machine.
- **AI models** (nomic-embed-text, mistral-nemo): the local AI that scores your jobs; the biggest download, a few GB.
- **curl** (Linux): fetches the Ollama installer.
- **Container image**: automatch's own Python libraries (like JobSpy), built once.

---

MIT licensed. Manual setup in
[project_files/docs/manual-setup.md](project_files/docs/manual-setup.md),
sharper-results tips in
[project_files/docs/tuning.md](project_files/docs/tuning.md).
