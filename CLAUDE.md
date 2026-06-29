# CLAUDE.md — rules for AI agents working on this repo

This file is read by Claude / Cursor / any AI agent on every session. Treat everything here as standing instructions. Follow it on **every** change.

---

## 0. Primary goal — raise the "proud %" toward 100%

The single most important objective of this project is to raise the **proud %** of every pipeline stage toward **100%**:

| Stage | Meaning | Current proud % |
|-------|---------|----------------:|
| **Scraping** | Jobs discovered, deduped, stored with usable URLs | 82% |
| **Scoring** | Jobs rated against the profile; bad fits filtered early | 76% |
| **Resume changing** | Tailored resume + cover letter generated per attempt | 88% |
| **Applying** | Applications that reach "submitted" vs attempts | 14% |

(Full definitions, funnel math, and challenges live in [PERFORMANCE.md](PERFORMANCE.md). Keep that file's numbers in sync with this table.)

This goal is the lens for **everything** you do:

- **Every change you make must move at least one stage's proud % up** (or protect it). If a change doesn't serve this goal, reconsider whether it belongs here. No feature, refactor, or "nice to have" ships if it doesn't raise a percentage.
- When you have to choose what to build next, choose **whatever raises the lowest proud %** (right now that's **Applying at 14%**).

---

## 1. Failures are the work — resolve them, don't just log them

Every time the agent scrapes, scores, tailors, or applies and **fails at any stage**, that failure is a signal and your next task. Do not log it and move on.

Run this loop on every run:

```
scrape / score / tailor / apply
        │
        ├─ success ──► count it, keep going
        │
        └─ FAILURE ──► find the root cause ──► fix it so it won't recur ──► re-measure ──► proud % goes up
```

1. Run `daily-apply` (or the stage you're working on).
2. Inspect what **failed**: errored jobs, jobs that didn't scrape, weak scores, missing resumes, applies that didn't reach Submit.
3. Find the **root cause**, not the symptom.
4. Implement a fix so that same failure **won't recur**.
5. Re-measure and update the proud % in this file and `PERFORMANCE.md`. If the number didn't go up, the fix wasn't real.
6. Pick the **lowest** stage and repeat.

A recurring failure that gets permanently resolved is a percentage point earned. Never leave a recurring failure unaddressed.

Data sources for diagnosis:

```bash
sqlite3 data/jobs.db "SELECT status, COUNT(*) FROM jobs GROUP BY status;"
sqlite3 data/jobs.db "SELECT notes FROM jobs WHERE status='error';"
# plus output/daily_apply*.log
```

---

## 2. Ask the user before changing the rules

The agent's behavior is driven by **rules and preferences** the user owns:

- `config.json` — discovery keywords, locations, score thresholds, apply caps, daily goal.
- `profile.json` → `preferences` — visa, notice period, remote, salary, relocation, screening answers.

These rules decide what the agent applies to **on the user's behalf**. **Before changing any of them — or adding behavior that depends on a new rule — ask the user** what they want, then encode their answer in config. Do not silently guess. Confirm things like:

- Target titles, locations, seniority (`linkedin_keywords`, `linkedin_locations`, `linkedin_apply_keywords`).
- Score thresholds and daily goal (`auto_submit_score_threshold`, `linkedin_apply_min_score`, `daily_apply_goal`).
- Remote-only vs. on-site, visa/relocation answers, notice period (`profile.preferences`).
- Whether to relax or tighten a rule when it's the thing blocking a higher proud %.

---

## 3. Never invent resume facts

The profile (`profile.json`) is the single source of truth. The agent **only reorders and rephrases facts that already exist there** — it never invents employers, dates, titles, metrics, or skills. Generated screening answers must be consistent with `profile.preferences`.

---

## 4. Don't commit secrets

`config.json` and `profile.json` contain API keys and personal data and are gitignored. Never commit them, and never paste their contents into a PR, commit message, or shared doc.

---

## 5. Contribution / git workflow

All changes go through a pull request. **Do not commit directly to `main`.**

1. Create a **new branch** off `main` (e.g. `feat/easy-apply-state-machine`, `fix/scoring-precision`).
2. Make your changes, keeping each PR focused on one improvement (ideally one that raises a specific proud %).
3. Commit with a clear message describing **which proud % it moves and why**.
4. Push the branch and **open a Pull Request**.
5. **Tresor reviews the PR and merges it to `main`.** Don't merge your own PR to main.

Every PR description should answer: *which stage's proud % does this raise, and how was it measured?*

---

**Let's work toward 100% proud level.**
