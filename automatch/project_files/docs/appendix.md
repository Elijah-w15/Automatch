# Appendix: every file and what it does

The layout you get after extracting and what each file is for. The start files
do everything for you; this is just a map for the curious.

```
automatch/
├── README.md                      what automatch is and how to start
├── WINDOWS_START_HERE.cmd          Windows: double-click to install + launch
├── LINUX_START_HERE.sh             Linux: run to install + launch
└── project_files/
    ├── start.py                    entry point: first run opens the setup wizard, after that it runs the matcher
    ├── setup.py                    the first-run setup wizard (deps, search, rubric, resume, Discord)
    ├── edit.py                     change settings later (metrics, scoring, search, Discord, resume)
    ├── edit_metrics.py             the add / remove / edit / replace metrics editor (used by edit.py)
    ├── Dockerfile                  builds the app's container image (base stage, plus an advanced stage)
    ├── docker-compose.yml          defines the automatch and bot services and their volumes
    ├── .dockerignore               keeps secrets and junk out of the image build
    ├── .gitignore                  keeps secrets and personal data out of git
    ├── .env.example                template showing the Discord credential keys
    ├── requirements.txt            Python packages for the image (the bot stage adds discord.py)
    ├── LICENSE                     MIT license
    ├── core/                       the application itself (runs inside the container)
    │   ├── __main__.py             makes `python -m core` run the app
    │   ├── main.py                 command line + loads profile.yaml/config.yaml, runs the pipeline
    │   ├── scrape.py               scrapes job boards (via JobSpy)
    │   ├── score.py                embeds your resume and has the local AI judge each job vs your rubric
    │   ├── rank.py                 weighted-average ranking, threshold, writes matches.html / matches.json
    │   ├── bot.py                  the Discord bot and its commands (advanced mode)
    │   ├── tailor.py               builds the per-job resume by swapping the <tag> with each job's keywords
    │   ├── paths.py                one place that defines every file path, plus the run lock
    │   └── __init__.py             marks the folder as a Python package
    ├── config/
    │   ├── config.yaml             system defaults: models, weights, scraper settings (profile.yaml overrides these)
    │   └── profile.example.yaml    sample of YOUR file: job search + scoring metrics
    └── docs/
        ├── manual-setup.md         set things up by hand instead of using the wizard
        ├── tuning.md               get sharper rankings (anchors, weights, threshold, models)
        ├── appendix.md             this file
        ├── resume.example.docx     sample resume (the John Doe sample)
        ├── resume_tag.example.docx   the same resume with a <tag> in the skills line (advanced)
        └── resume_embed.example.docx   the same resume stripped to dense skills, for matching (optional)
```

## Created when you run it (not shipped, and gitignored)

The wizard and the app write these from your input and results:

```
project_files/
├── config/
│   ├── profile.yaml            YOUR job search + scoring rubric (wizard / edit.py)
│   ├── resume.txt              your resume, scored and matched against jobs
│   ├── resume_template.txt     your <tag> resume (advanced; the bot tailors from it)
│   ├── resume_embed.txt        optional hand-trimmed resume used only for matching (edit.py)
│   └── approvedskills.txt      skills you confirmed in the bot, auto-applied later
├── .env                        your Discord credentials (advanced)
└── output/
    ├── jobs.jsonl              raw scraped postings
    ├── scores.jsonl            per-job scores (cosine + each metric + final)
    ├── matches.html            the ranked, clickable results you open
    ├── matches.json            the same results as data
    ├── seen.json               which postings were already scraped (dedupe)
    ├── resumes/                per-job tailored resumes (advanced)
    └── resume_choices.json     your per-job keyword decisions (advanced)
```
