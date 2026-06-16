# Better-quality results: tuning guide

Everything here is optional; defaults work. These are the knobs that
sharpen the rankings, roughly in order of impact.

## How to change any of this

The easy way is the editor. Run `python3 edit.py` (or `python edit.py` on
Windows) for a menu:

- scoring metrics: add, remove, edit, or replace them (including each
  metric's weight)
- scoring settings: threshold, wildcard, and the judge model
- job search: titles, location, radius, age, max jobs, level, salary, blocks
- Discord login: token, public key, user id
- resume: your tagged resume, and an optional matching/embedding resume

Edits apply on your next run, and the Discord bot re-reads them automatically
(no restart). You can still hand-edit `config/profile.yaml` and
`config/config.yaml` if you prefer.

In the Discord bot, send `!commands` for the full list:

- `!match`: scrape + score + rank, then the per-job resume builder
- `!scrape`: scrape + score + rank only, posts the top list
- `!build`: resume builder on the jobs already scraped + rated
- `!model` / `!model 1-4`: show or switch the local judge model
- `!tag` / `!tag <skill>` / `!tag remove <skill>`: view, add, or forget an
  approved skill
- `!pause` (or `!stop`): stop the current run; progress is saved and `!scrape`
  resumes it
- `!metric`: how to edit your scoring metrics (done in the terminal)
- `!commands`: this list

Terminal commands: `python3 start.py` (run it / first-time setup),
`python3 edit.py` (change config), `python3 setup.py` (redo the full setup
wizard), `automatch` (start the Discord bot, advanced mode).

## Where your settings live

For easy changes, use `edit.py`: it edits the right file for you, so you rarely
need to open these by hand. Two files hold everything:

- `config/profile.yaml` is your file: job search, scoring metrics, threshold,
  and wildcard. `edit.py` writes it for you, and its values override the
  matching defaults in config.yaml.
- `config/config.yaml` is system internals (shipped defaults; profile.yaml wins
  on the overlapping fields). You rarely touch it, but it holds a few advanced
  knobs worth knowing, edited by hand (plain YAML; changes apply next run):
  - `score.cosine_weight`: how hard resume↔description similarity steers the
    ranking (see Weights below).
  - `score.top_n`: how many jobs land in `matches.html`.
  - `score.level_adjust` / `score.level_filter`: the entry/mid/senior/executive
    preference. `level_adjust` softly rewards or punishes levels (re-rank only);
    uncomment `level_filter` to keep ONLY the levels you list.
  - `models.judge` / `models.embed` / `models.host`: which local models score
    and embed, and where Ollama lives. Change the judge in `edit.py` (scoring
    settings) or with `!model`; the rest rarely change.
  - `scrape.*`: which boards (`sites`), request delay, country, and other
    scraper plumbing.

## 1. Anchors are the whole game

The judge scores each metric by matching the job's inferred day-to-day
against YOUR anchor descriptions. Vague anchors → middle-of-the-road
scores for everything. Two rules:

**Make low scores easy to give.** If nothing "looks like" your 0.0, the
judge never uses it.

```yaml
# BAD: too vague, everything scores 0.6:
0.0: "bad job"
0.6: "decent job"
1.0: "great job"

# GOOD: concrete, distinguishable situations:
0.0: "completely different field, zero overlapping skills"
0.2: "they require a degree or clearance I don't have"
0.4: "a stretch: a few overlapping skills"
0.6: "decent chance, several overlapping skills"
0.8: "strong fit for my experience and skillset"
1.0: "this job was made for me"
```

**Anchor the behavior, not the adjective.** "Pressing one button all
day" beats "boring". The judge reads job duties; give it duties to
match against.

## 2. The judge reasons before scoring

Each job gets a `day_to_day` inference first: what the role actually
involves, with marketing fluff ("dynamic", "fast-paced", "rockstar")
explicitly ignored. The metrics are scored against that reality.
You can read it: hover any job title in `matches.html`, or see it in
the bot's job cards. If a score looks wrong, read the day_to_day first;
it usually explains the judgment, and the fix is usually an anchor.

## 3. Threshold

The threshold is the minimum weighted-average score a job needs to appear
in matches. Start at 0 (see everything, ranked), look at where your real
matches separate from the noise, then set it (0.65–0.7 is typical). Set it
in `edit.py` under scoring settings, or by hand in profile.yaml. Changing it
never re-judges existing scores; the next run re-ranks them (plus scrapes
whatever's new).

## 4. Weights: what they do

Every metric has a `weight` that decides how much it counts toward a job's
final score. The final score is the weighted average of all your metrics plus
the resume-similarity (cosine) signal:

```
score = (cosine_weight * cosine + sum(weight * metric_score)) / total_weight
```

So 1 = normal, 0.5 = counts half as much, 2 = counts double. Setup doesn't ask
for weights (every metric starts at 1, which is why it's a "hidden" value);
change one in `edit.py` under scoring metrics → edit a metric → weight, or by
hand in `config/profile.yaml`. There's also `cosine_weight` in config.yaml for
the resume-similarity signal.

Weight changes apply at rank time: instant, no re-judging. Note: cosine
(resume↔description similarity) spans only ~0.55–0.80 in practice, narrower
than your 0–1 metrics, so raise `cosine_weight` if you want resume-match to
steer harder.

## 5. The resume the embedding sees (and why it matters)

Part of every job's score is cosine similarity: automatch turns your resume
into a vector (an "embedding"), turns each job description into a vector, and
measures how closely they point the same way. The cleaner and more on-topic
your resume text, the better that signal separates real fits from noise, so
embedding quality directly moves your rankings.

Boilerplate hurts it. Names, contact lines, links, and objective statements
("a motivated, detail-oriented professional seeking a dynamic role") say
nothing about the work, so they dilute the vector. automatch auto-strips the
name header and contact lines before embedding, but a hand-trimmed version
does better.

Two resumes do two different jobs, so don't mix them up:

- **Your real resume** (`resume.txt`; in advanced mode also
  `resume_template.txt`, the copy carrying the `<tag>` the bot tailors per
  job). The judge reads this to score jobs against your rubric. Keep it
  complete and human-readable.
- **The matching resume** (`resume_embed.txt`, optional) is used ONLY for the
  cosine similarity above. No `<tag>`, no name, no contact, no objective: just
  a dense list of your real skills, tools, duties, and outcomes.

```
# resume.txt / resume_template.txt - your full, human-facing resume:
Jane Doe
jane@email.com | 555-123-4567 | linkedin.com/in/janedoe
Objective: a motivated, detail-oriented professional seeking a dynamic role.
Skills: Microsoft Office, <tag>, Python, SQL, Excel
Experience: Data Analyst, 2021-2024 ...

# resume_embed.txt - skills-dense, no boilerplate, no <tag>:
Python, SQL, Excel, Tableau, pandas, ETL pipelines, data warehousing.
Automated weekly reporting (cut turnaround 40%), A/B testing, dashboard
design, sales and operations analysis, stakeholder presentations.
```

To add or change it, run `edit.py` and choose resume: it shows your
auto-stripped resume (what gets embedded today) so you can compare, then lets
you upload your own. When `resume_embed.txt` is present it's embedded instead
of the auto-strip, and changing it re-scores your jobs.

## 6. Re-scoring rules (what triggers what)

| You change | What happens next run |
|---|---|
| anchors / questions / metric names | full re-judge (automatic) |
| `resume.txt` | full re-judge (automatic) |
| custom matching resume (`resume_embed.txt`) | full re-judge (automatic) |
| judge model | full re-judge (automatic) |
| weights / cosine_weight | re-rank only: no re-judging |
| threshold / level / salary / exclude / terms | re-rank only: no re-judging |

## 7. Models

No GPU or slow scoring? Switch the judge in `edit.py` under scoring settings,
or send `!model 1-4` in the bot, or hand-edit config.yaml
(`judge: "llama3.2:3b"`, pull it first). Smaller = faster, slightly blunter
judgments; the anchored 0–1 rubric keeps small models usable.
