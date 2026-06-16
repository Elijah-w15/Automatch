# automatch

A fully local job-matching tool. It scrapes job boards (via
[JobSpy](https://github.com/Bunsly/JobSpy)), uses AI **on your own machine** to
score every posting against your resume and your priorities, and gives you a
ranked, clickable list at `output/matches.html`. Nothing leaves your machine,
and the core flow needs no accounts and no API keys.

**What it does, in order:** scrape jobs → a local AI scores each one against a
rubric you define (your questions, your 0-to-1 examples) → rank → open the
results in your browser.

**Optional Discord mode:** a free bot you create DMs you your top matches and
builds an ATS-targeted resume for each job. It proposes the skill keywords that
job's screener scans for, and you confirm only the ones you actually have.

## Get started

> ### Warning: put automatch on a LOCAL drive first
>
> Do **not** run it from a cloud-synced folder (OneDrive, Google Drive,
> Dropbox). Move the downloaded zip to a normal local folder (your Desktop, or
> `C:\automatch` on Windows, `~/automatch` on Linux), unzip it there, and run
> everything from that folder. Cloud sync fights with Docker, the multi-GB AI
> models, and automatch's file locks, and will hang setup or corrupt `output/`.

To start, open the file for your OS. It installs what it needs, then runs the
setup wizard, which walks you through the rest (Docker, Ollama, the AI models)
and asks before installing anything:

- Windows: double-click `WINDOWS_START_HERE.cmd`. It checks for Python and
  installs it (via winget) if missing, then starts the wizard.
- Linux: run `sh LINUX_START_HERE.sh` in the folder. It installs `python3` and
  `curl` if they're missing (it asks for your password), then starts the wizard.

> ### Downloaded content is saved: you're safe to close and reopen the start file, and it keeps your progress
>
> Windows, step by step:
> 1. Double-click `WINDOWS_START_HERE.cmd`. It checks for Python.
>    - Already have Python? It goes straight into the setup wizard.
>    - If not, it installs Python, then asks you to close the window and open
>      `WINDOWS_START_HERE.cmd` again. Reopening drops you straight into the
>      wizard. (This is not a reboot: Windows only shows newly installed programs
>      to fresh terminals.)
> 2. Follow the wizard until it installs Docker. Docker installs WSL. This
>    usually requires a reboot, so the wizard asks "reboot now?":
>    - Yes, and it restarts the PC for you.
>    - No, and you reboot yourself (the wizard stops and waits, since setup
>      can't continue until WSL is active).
>    After the reboot, open `WINDOWS_START_HERE.cmd` again to finish. (If no
>    reboot is needed, the wizard just keeps going, no interruption.)
> 3. The wizard installs Ollama, downloads your AI models (several GB, no
>    reboot), scores your first batch of jobs, and opens the results. From here
>    there are no more restarts.
>
> Linux, step by step:
> 1. Open `LINUX_START_HERE.sh` (double-click it, or run `sh LINUX_START_HERE.sh`
>    in a terminal). It installs `python3` and `curl` if missing, then the wizard
>    installs Docker and adds you to the `docker` group. For that to take effect,
>    **log out of your computer and back in once** (the Linux version of the
>    Windows reboot). Already in the `docker` group? It just keeps going.
> 2. Open `LINUX_START_HERE.sh` again to finish: it installs Ollama, downloads
>    your AI models (several GB), scores your first batch of jobs, and opens the
>    results.
>
> WSL is **not** installed by the start file; on Windows, Docker Desktop pulls it
> in during step 2. Whenever a step hands control back, just reopen the start
> file; it always continues where it left off.

> ### Docker Desktop (Windows) asks you to sign in
>
> The first time Docker Desktop opens it makes you sign in. Create a free Docker
> account, or just click "Continue with Google." It's free and quick. automatch
> won't run until Docker is signed in and running.

## What gets installed

automatch installs only what's missing, and the wizard asks before each one. On
your computer:

- Python 3.12, to run the launcher and wizard. Windows: winget
  (`Python.Python.3.12`). Linux: apt (`python3`). Skipped if you already have
  Python 3.10 or newer.
- `curl`, used to download Ollama. Already built in on Windows; Linux installs
  it via apt.
- Docker runs automatch in a container.
  - Windows: Docker Desktop (via winget). It uses WSL2, which may need a
    one-time reboot to turn on.
  - Linux: `docker.io` plus the `docker-compose` plugin (via apt). You're added
    to the `docker` group so Docker works without `sudo`; log out of your
    computer and back in once for that to take effect.
- Ollama, to run the local AI on your machine. Windows: winget (`Ollama.Ollama`).
  Linux: the official script (`ollama.com/install.sh`).

AI models, downloaded by Ollama (several GB, one time): `nomic-embed-text` for
match similarity, plus one scoring model you choose: `llama3.1:8b` (~4.9 GB),
`mistral-nemo` (~7 GB), `qwen2.5:14b` (~9 GB), or `qwen2.5:32b` (~20 GB). Only
the one you pick is downloaded.

Inside Docker (bundled in the container image, not installed on your machine
directly): the Python libraries automatch uses, namely `python-jobspy`,
`pandas`, `pyyaml`, `requests`, plus `discord.py` for the optional Discord bot.

## Changing things later

Run `python project_files/edit.py` (Windows) or `python3 project_files/edit.py` (Linux) to change your
scoring metrics, scoring settings (threshold, wildcard, model), job search,
Discord login, or resume, without redoing setup.

Once the Discord bot is running, message it `!commands` for the full command
list. Not using the bot? The same options live in the terminal: run `edit.py`
from the `project_files` folder.

Already have Python 3.10 or newer and just want to run it? `python project_files/start.py`
(Windows) or `python3 project_files/start.py` (Linux). By-hand setup is in
[project_files/docs/manual-setup.md](project_files/docs/manual-setup.md); sharper-results tips in
[project_files/docs/tuning.md](project_files/docs/tuning.md); a map of every file in
[project_files/docs/appendix.md](project_files/docs/appendix.md).

---

MIT licensed.
