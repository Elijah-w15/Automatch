# Better-quality results: tuning guide

Everything here is optional; defaults work. These are the knobs that
sharpen the rankings, roughly in order of impact.

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

`threshold` in profile.yaml = minimum weighted-average score to appear
in matches. Start at 0 (see everything, ranked), look at where your
real matches separate from the noise, then set it (0.65–0.7 is typical).
Changing it never re-judges existing scores; the next run re-ranks
them (plus scrapes whatever's new).

## 4. Weights

Per-metric `weight` in profile.yaml (1 = normal, 0.5 = half, 2 =
double) and `cosine_weight` in config.yaml. Weight changes apply at
rank time: instant, no re-judging. Note: cosine (resume↔description
similarity) spans only ~0.55–0.80 in practice, narrower than your 0–1
metrics, so raise `cosine_weight` if you want resume-match to steer
harder.

## 5. The resume the embedding sees (pre-stripping helps)

Before the embedder compares your resume to a job, automatch strips the
name header and contact details (email, phone, links) so cosine matches
on skills and experience, not boilerplate. The judge and the resume
builder still see your FULL resume -- only the embedding input is trimmed.

You get a sharper match by going one step further yourself: a resume cut
down to just skills, tools, duties and outcomes embeds better than one
padded with a name, title line, employer names, dates and section
headers. Concrete nouns ("OBD-II scan tools", "MIG welding") carry the
signal; "John Doe / Professional Summary / 2019-2023" is noise to an
embedding model.

A worked example ships in the zip -- the sample resume reduced to content
only (no name, no contacts, no "Experience / Projects / Skills" labels,
no employer or dates):

    config/resume_stripped.example.txt     (and .example.docx)

```
BEFORE  (config/resume.example.txt)
    John Doe
    john.doe@example.com  •  (888) 888-8888  •  Philadelphia, PA 19104
    Professional Work Experience
    Summit Peak Auto Group               King of Prussia, PA
       Automotive Technician                     March 2023 - Present
    Diagnosed and repaired 40+ vehicles per month ...

AFTER   (config/resume_stripped.example.txt)
    Automotive technician moving into fleet diagnostics and shop
    management, with side projects in engine building ...
    Diagnosed and repaired 40+ vehicles per month ...
    Engine Diagnostics, MIG Welding, Brake Systems, OBD-II Scan Tools ...
```

The first-startup wizard offers this for you: after you upload your
resume it asks whether to add a stripped copy for matching, and saves it
to `config/resume_embed.txt`. When that file exists the matcher embeds it
instead of auto-stripping `resume.txt` -- and the judge and the resume
builder still read your FULL resume, so you lose no context where it
matters.

Either way is OPTIONAL: the auto-stripper handles a normal resume fine and
DETECTS an already-stripped one. With no contact block to key off, it
won't mistake your first line (a heading or a skill) for a name and delete
that word everywhere -- an already-stripped file is passed through
untouched.

## 6. Re-scoring rules (what triggers what)

| You change | What happens next run |
|---|---|
| anchors / questions / metric names | full re-judge (automatic) |
| resume.txt | full re-judge (automatic) |
| judge model | full re-judge (automatic) |
| weights / cosine_weight | re-rank only: no re-judging |
| threshold / level / salary / exclude / terms | re-rank only: no re-judging |

## 7. Models

No GPU or slow scoring? Swap the judge in config.yaml:
`judge: "llama3.2:3b"` (pull it first). Smaller = faster, slightly
blunter judgments; the anchored 0–1 rubric keeps small models usable.
