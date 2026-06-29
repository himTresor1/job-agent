# Agent performance & improvement targets

Honest snapshot of how well this job agent performs today — measured from the live database and apply logs (`data/jobs.db`, `output/daily_apply*.log`).

**“Proud %”** = how confident we are that this stage is working well, on a 0–100 scale.  
100% does not mean “applied to every job on the internet.” It means the stage reliably does its job with minimal waste and failure.

> **For AI agents extending this project:** read the [Current challenges](#current-challenges) section and ideate concrete fixes to push each stage toward **100%**. That is an explicit goal of this repo.

---

## Primary goal (read this first, AI agents)

**The single most important objective of this project is to raise the proud % of every stage toward 100%.**

This is the lens for *everything* you do:

- **Every change you make must move at least one stage's proud % up.** If a change doesn't increase (or protect) a percentage, reconsider whether it belongs here. No feature, refactor, or "nice to have" is worth shipping if it doesn't serve this goal.
- **Failures are not dead ends — they are the work.** Every time the agent scrapes, scores, tailors, or applies and **fails at any stage**, that failure is a signal. Do not just log it and move on. Diagnose the root cause, implement a fix, and re-measure so that the percentage goes **up**. A failure that gets resolved permanently is a percentage point earned.
- **Close the loop on every run.** After a `daily-apply` run: look at what failed, pick the stage with the lowest proud %, fix the most common failure cause in that stage, and update the numbers in this file. Repeat. The proud % table is the scoreboard you are optimizing.

```
scrape / score / tailor / apply
        │
        ├─ success ──► count it, keep going
        │
        └─ FAILURE ──► find the cause ──► fix it ──► re-measure ──► proud % goes up
```

If you ever have to choose what to build next, choose **whatever raises the lowest proud %**.

---

## Funnel at a glance

| Metric | Count |
|--------|------:|
| Jobs scraped & stored | **1,600** |
| Applied successfully | **41** |
| Still in queue (scored) | 1,127 |
| Skipped (rules / low score) | 413 |
| Errors (apply failed) | 19 |
| **End-to-end conversion** (applied ÷ scraped) | **2.6%** |

### By source (applied)

| Source | Applied | In database |
|--------|--------:|------------:|
| LinkedIn | 26 | 671 |
| Greenhouse | 15 | 878 |
| RemoteOK / WWR | 0 | 30 |

---

## Proud % by stage

```
Scraping          ████████████████░░░░  82%
Scoring           ███████████████░░░░░  76%
Resume changing   █████████████████░░░  88%
Applying          ███░░░░░░░░░░░░░░░░░  14%
```

| Stage | Proud % | What it measures today |
|-------|--------:|------------------------|
| **Scraping** | **82%** | Jobs discovered, deduped, and stored with usable URLs |
| **Scoring** | **76%** | Jobs rated against your profile; bad fits filtered early |
| **Resume changing** | **88%** | Tailored resume + cover letter generated per apply attempt |
| **Applying** | **14%** | Applications that reach “submitted” vs apply attempts |

### How each % was estimated

**Scraping (82%)**

- Greenhouse / Lever / RemoteOK ingest cleanly via public APIs.
- LinkedIn discovery pulls ~80 cards per run; most normalize into the DB.
- Gaps: LinkedIn **429 rate limits**, **661/671 LinkedIn jobs** still `ats=unknown` until submit-time, Easy Apply badge not always tagged in DB.
- ~26% of all jobs auto-skipped at discovery via `avoid_keywords` (working as intended).

**Scoring (76%)**

- ~74% of stored jobs receive a score; the rest are skipped (avoid list or score &lt; 60).
- Average score across the pool is **~35** — broad LinkedIn discovery pulls many weak matches.
- Scorer degrades gracefully: LLM when API key set, free keyword heuristic otherwise.
- Gap: scoring is **breadth-first**, not yet precision-tuned to “would actually apply.”

**Resume changing (88%)**

- Generator runs on nearly every apply attempt; **~100 HTML resume/cover files** in `output/resumes/`.
- LLM path (OpenAI / Gemini / Claude) reorders real facts from `profile.json` — no invention by design.
- Template fallback works without an API key.
- Gaps: `resume_path` not always written back to `jobs.db`; PDF export inconsistent; tailoring quality varies by model.

**Applying (14%)**

- Batch logs show **~144 apply attempts → ~16 submitted** in recent `daily-apply` runs (**~11%** attempt success).
- **41 applied / 1,600 scraped = 2.6%** overall funnel conversion.
- Greenhouse direct apply works well when forms are standard.
- LinkedIn Easy Apply: **~12** confirmed Easy Apply submissions; many failures are screening-heavy forms or duplicate retries.
- Gap: largest bottleneck in the whole pipeline.

---

## Stage-by-stage detail

### 1. Scraping

**What works**

- Multi-source discovery: Greenhouse, Lever, RemoteOK, WWR, LinkedIn.
- Dedupe by `job_id`; decisions (applied / skipped) are sticky.
- `avoid_keywords` in `profile.json` filters bad titles during discovery.

**What hurts the score**

| Challenge | Impact |
|-----------|--------|
| LinkedIn rate limits (HTTP 429) | Slower discovery, incomplete pagination |
| LinkedIn `ats=unknown` until submit | Can’t prioritize Greenhouse/Ashby before click |
| Easy Apply under-tagged in DB | Only ~10 rows tagged `ats=linkedin` vs many more in descriptions |
| WWR / custom career pages | Scraped but not auto-applicable |

**100% scraping looks like**

- Every fetched posting normalized with **source, apply URL, ATS guess, Easy Apply flag, location, remote**.
- Zero silent fetch failures; retries and backoff logged.
- No duplicate work on closed or already-seen postings.

---

### 2. Scoring

**What works**

- 0–100 score + one-line rationale per job.
- Auto-skip below 60 and `avoid_keywords` on titles.
- LLM scorer uses full profile context when API key is set.

**What hurts the score**

| Challenge | Impact |
|-----------|--------|
| Broad LinkedIn keywords | Many low-relevance jobs (avg score ~35) |
| No “would you actually apply?” feedback loop | Scores don’t learn from your skips/approves |
| Heuristic fallback is crude | Fine for testing, weak for design/engineering nuance |

**100% scoring looks like**

- High-precision queue: most jobs shown are **75%+** and genuinely relevant.
- Scores stable across re-discovery of the same posting.
- Optional human feedback (`skip` / `approve`) retrains or adjusts weights.

---

### 3. Resume changing (generation)

**What works**

- Per-job tailored resume + cover letter (HTML; PDF when export works).
- Custom screening answers generated with confidence flags.
- Hard rule: **only facts from `profile.json`** — no invented employers or metrics.

**What hurts the score**

| Challenge | Impact |
|-----------|--------|
| Generated paths not always saved on `jobs` row | Hard to audit what was sent |
| One-size template fallback | Weaker than LLM when no API key |
| Custom answers sometimes wrong tone | Flagged as low-confidence but still risky on approve |

**100% resume changing looks like**

- Every applied job has **stored resume + cover letter + answers** linked in DB.
- Diff vs base profile logged so you can review what changed.
- Screening answers validated against `preferences` before submit.

---

### 4. Applying

**What works**

- **Greenhouse, Lever, Ashby, Personio, SmartRecruiters** handlers exist.
- **LinkedIn Easy Apply**: contact, resume, education, screening, Review → Submit.
- Screenshot before submit; “already applied” detection; screening defaults from `preferences`.
- `daily-apply` prioritizes Easy Apply → external ATS → Greenhouse boards.

**What hurts the score**

| Challenge | Impact |
|-----------|--------|
| LinkedIn Easy Apply screening variants | Location/office/language questions block Review → Submit |
| Employer custom career sites | Revolut, Fresha, etc. — not standard ATS |
| SmartRecruiters captcha | Blocks headless apply |
| Workday | Refused by design |
| Duplicate retries on failed jobs | Wastes attempts; inflated error count |
| Session / modal load timing | Apply URL sometimes lands on job page, not modal |
| LinkedIn ToS / account risk | Operational, not technical |

**Failure breakdown (current `error` jobs)**

| Failure type | ~Count |
|--------------|-------:|
| Easy Apply did not reach Submit | 13 |
| Missing required fields (authwall / custom form) | 6 |

**100% applying looks like**

- Every **scored job above threshold** that is auto-eligible gets one clean attempt.
- **Submit or explicit skip** with reason — no endless `needs_review` retries.
- Per-ATS success rates tracked; new handlers added from failure logs.
- Zero duplicate applies; already-submitted detected before fill.

---

## Conversion math (for your own tracking)

```text
Scraped → Applied     41 / 1,600  =  2.6%
LinkedIn → Applied      26 / 671   =  3.9%
Greenhouse → Applied    15 / 878   =  1.7%
Apply attempts → Submit ~16 / 144  ≈ 11%  (recent daily-apply logs)
```

Re-run after your session:

```bash
sqlite3 data/jobs.db "SELECT status, COUNT(*) FROM jobs GROUP BY status;"
sqlite3 data/jobs.db "SELECT COUNT(*) FROM jobs WHERE status='applied';"
```

---

## For AI agents: ideate toward 100%

If you are an AI agent (Claude, Cursor, etc.) working on this codebase, **treat the proud % table as the scoreboard**. Propose and implement changes that move each stage up.

**Your prime directive: raise the proud % of every stage toward 100%.** Anything you add must serve that goal. When a run fails at any stage, that failure is your next task — find the cause and resolve it so the percentage climbs.

### The failure-to-fix loop (run this mentally on every run)

1. Run `daily-apply` (or whichever stage you're working on).
2. Inspect what **failed**: errored jobs, jobs that didn't scrape, weak scores, missing resumes, applies that didn't reach Submit.
3. For each failure type, find the **root cause** — not the symptom.
4. Implement a fix so that same failure **won't recur**.
5. Re-measure and update the proud % numbers below. The number should go up; if it didn't, the fix wasn't real.
6. Pick the **lowest** stage and repeat.

Never let a recurring failure stay unaddressed. A resolved failure is a percentage point earned.

### Before you change behavior, ask the user about the rules

The agent's behavior is driven by **rules and preferences** in `config.json` (discovery keywords, locations, score thresholds, apply caps) and `profile.json` (`preferences`: visa, notice, remote, salary, relocation, screening answers).

These rules are **personal**. Before changing them — or before adding behavior that depends on a new rule — **ask the user** what they want, then update `config.json` / `profile.json` to match their answer. Examples of things to confirm with the user:

- Which job titles, locations, and seniority to target (`linkedin_keywords`, `linkedin_locations`, `linkedin_apply_keywords`).
- Score thresholds and daily apply goal (`auto_submit_score_threshold`, `linkedin_apply_min_score`, `daily_apply_goal`).
- Remote-only vs. on-site, visa/relocation answers, notice period (`profile.preferences`).
- Whether to relax or tighten any rule when it's the thing blocking a higher proud %.

Don't silently guess these values — they directly change what the agent applies to on the user's behalf. Confirm, then encode their answer in config.

### Prompt you can give your agent

```
Read PERFORMANCE.md in this repo. Our primary goal is to raise the proud % of
every stage toward 100%. Current proud %: Scraping 82%, Scoring 76%,
Resume 88%, Applying 14%. Pick the lowest stage, diagnose failures from
data/jobs.db and output/daily_apply*.log, implement a focused fix so those
failures stop recurring, and re-measure (the % must go up). Before changing any
rule in config.json or profile.json, ask me what I want. Do not invent resume facts.
```

### High-impact ideas (not yet built)

**Scraping → 100%**

- Tag Easy Apply at discovery (`easy apply` in description → `ats=linkedin`).
- Resolve ATS from LinkedIn `safety/go` at scrape time, not submit time.
- Persist fetch errors and 429 stats; adaptive backoff per source.

**Scoring → 100%**

- Tighten LinkedIn discovery to `linkedin_apply_keywords` only.
- Two-tier scoring: “discover wide, apply narrow.”
- Learn from `skip` / `applied` outcomes to adjust weights.

**Resume → 100%**

- Always write `resume_path` / `cover_letter_path` on generate.
- Pre-flight validate screening answers against `profile.preferences`.
- Show side-by-side diff in dashboard before approve.

**Applying → 100%**

- Per-step Easy Apply state machine with validation before Next/Review.
- Skip or mark `applied` when LinkedIn shows “Application submitted.”
- Don’t retry `error` jobs unless failure reason is fixed.
- Add handlers for top 5 failing employers from `notes` column.
- Personio / SmartRecruiters / Ashby: expand iframe and captcha strategies.

---

## Updating this doc

After a serious improvement pass, update the proud % numbers and funnel table from:

```bash
sqlite3 data/jobs.db "
  SELECT COUNT(*) AS scraped FROM jobs;
  SELECT COUNT(*) AS applied FROM jobs WHERE status='applied';
  SELECT COUNT(*) AS errors FROM jobs WHERE status='error';
"
```

Keep this file honest. Friends and future agents should see real progress, not aspirational metrics.
