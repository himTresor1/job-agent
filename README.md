# Job Agent

A personal job-search agent that **discovers roles**, **scores them against your profile**, **generates tailored documents**, and **submits applications** through Playwright.

Built for developers and designers who want to automate the repetitive parts of applying — without inventing experience on your resume.

```
Discover → SQLite → Score → Generate docs → Submit (Playwright)
```

---

## What it does

| Stage | What happens |
|--------|----------------|
| **Discover** | Pulls jobs from Greenhouse, Lever, RemoteOK, We Work Remotely, and LinkedIn |
| **Dedupe** | Same posting seen twice collapses to one row; your decisions (skip/applied) stick |
| **Score** | Rates each job 0–100 against `profile.json` (LLM or free keyword fallback) |
| **Generate** | Tailored resume + cover letter per job; answers custom screening questions |
| **Submit** | Fills ATS forms and LinkedIn Easy Apply; screenshots before submit |

### Supported application targets

- **Greenhouse**, **Lever**, **Ashby** — direct form filling + iframe support
- **Personio**, **SmartRecruiters** — dedicated handlers
- **HiBob** and generic career pages — best-effort fill
- **LinkedIn Easy Apply** — contact, resume, education, screening questions
- **LinkedIn → external ATS** — follows `safety/go` links to Greenhouse, Ashby, etc.

**Refused:** Workday (per-company accounts, too inconsistent).

---

## Quick start

**New here?** Follow the step-by-step copy-paste guide in [SETUP.md](SETUP.md).

Want to know how well the agent performs today (and what to improve)? See [PERFORMANCE.md](PERFORMANCE.md).

### 1. Clone and install

```bash
git clone <your-repo-url> jobagent
cd jobagent

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install anthropic playwright flask jinja2 google-generativeai openai
playwright install chromium
```

### 2. Create your config files

```bash
cp config.example.json config.json
cp profile.example.json profile.json
```

**Never commit `config.json` or `profile.json`** — they contain API keys and personal data. A `.gitignore` is included.

### 3. Add an LLM API key (pick one)

Put **one** key in `config.json`. Priority if multiple are set: OpenAI → Gemini → Anthropic → template/heuristic fallback.

```json
{
  "openai_api_key": "sk-...",
  "gemini_api_key": "...",
  "anthropic_api_key": "sk-ant-..."
}
```

| Provider | Used for |
|----------|----------|
| **OpenAI** (ChatGPT) | Scoring + resume/cover letter generation |
| **Gemini** | Same |
| **Anthropic** (Claude) | Same |
| **None** | Free keyword scorer + template resume (no API cost) |

### 4. Fill in your profile

Edit `profile.json` with your real experience. See [Building your profile](#building-your-profile-with-claude-or-any-llm) below.

### 5. Configure job sources

Edit `config.json`:

```json
{
  "greenhouse_boards": ["stripe", "figma", "gitlab"],
  "lever_boards": ["plaid"],
  "enable_remoteok": true,
  "remoteok_keywords": ["frontend", "react", "typescript"],
  "enable_linkedin": true,
  "linkedin_keywords": ["Frontend Engineer", "Product Designer"],
  "linkedin_locations": ["Remote", "EMEA", "United Kingdom"]
}
```

Board token = company slug in the careers URL: `boards.greenhouse.io/acme` → `"acme"`.

### 6. Run

```bash
# Discover + score (no applying)
python -m jobagent run

# See top matches
python -m jobagent list --min-score 75

# Full daily pipeline: discover → score → apply toward daily goal
python -m jobagent -v daily-apply
```

---

## Building your profile (with Claude or any LLM)

Your profile is the single source of truth. The agent **only reorders and rephrases facts from `profile.json`** — it does not invent employers, dates, or skills.

### Option A: Paste your resume into Claude / ChatGPT

Use a prompt like this:

```
Convert my resume into a JSON profile for a job-search agent.

Schema:
- name, email, phone, location, headline, summary (string)
- target_titles: array of roles I want
- target_keywords: skills/tech to match on
- avoid_keywords: roles/terms to auto-skip (e.g. "internship", "sales")
- skills: array of strings
- experience: [{ company, title, dates, location, bullets: [] }]
- education: [{ school, degree, dates, location }]
- links: { portfolio, github, linkedin }
- preferences: {
    remote_ok, open_to_relocation,
    notice_period,           // e.g. "7 days"
    remote_preference,       // e.g. "Yes — seeking a fully remote role"
    visa_sponsorship_required
  }

Rules:
- Use ONLY facts from my resume. Do not invent metrics or employers.
- Keep bullets achievement-focused and concise.
- Put my most relevant skills in target_keywords.

Here is my resume:
[paste resume text or PDF contents]
```

Save the output as `profile.json`.

### Option B: Start from the example

```bash
cp profile.example.json profile.json
```

Then edit by hand. The fields that matter most:

| Field | Purpose |
|-------|---------|
| `target_titles` | Roles you want — used for scoring |
| `target_keywords` | Tech/skills to match job descriptions |
| `avoid_keywords` | Auto-skip jobs whose **title** contains these |
| `experience[].bullets` | What the generator can emphasize per application |
| `education` | Fills LinkedIn Easy Apply education steps |
| `preferences` | Default answers for screening (notice, visa, remote) |

---

## Rules and preferences (`profile.json`)

Screening questions (visa, notice period, remote, salary, relocation) are answered from `preferences`:

```json
"preferences": {
  "remote_ok": true,
  "open_to_relocation": false,
  "notice_period": "7 days",
  "remote_preference": "Yes — seeking a fully remote role",
  "visa_sponsorship_required": true
}
```

**Avoid list** — skip bad fits during discovery:

```json
"avoid_keywords": [
  "unpaid", "internship", "senior staff", "principal",
  "sales", "marketing", "recruiter", "hr"
]
```

---

## Config reference (`config.json`)

> **AI agents:** the keys below are the agent's *rules* — they decide what it applies to on the user's behalf. **Before changing any of them, ask the user** what they want (titles, locations, score thresholds, remote vs. on-site, daily goal), then encode their answer here. Don't guess. See the primary goal in [PERFORMANCE.md](PERFORMANCE.md): every change should raise a stage's proud % toward 100%.

### Job discovery

| Key | Description |
|-----|-------------|
| `greenhouse_boards` | List of Greenhouse company slugs |
| `lever_boards` | List of Lever company slugs |
| `enable_remoteok` | Scrape RemoteOK RSS |
| `remoteok_keywords` | Filter RemoteOK by keyword |
| `enable_wwr` | We Work Remotely |
| `enable_linkedin` | LinkedIn job search |
| `linkedin_keywords` | Job title search terms |
| `linkedin_locations` | Locations (Remote, EMEA, country names) |
| `linkedin_max_results` | Max cards per discovery run |
| `linkedin_pages_per_query` | Pagination depth |

### Scoring and applying

| Key | Default | Description |
|-----|---------|-------------|
| `min_score_for_digest` | 70 | Minimum score to surface in lists |
| `auto_submit_enabled` | true | Auto-submit Greenhouse/Lever after `run` |
| `auto_submit_score_threshold` | 80 | Min score for Greenhouse auto-submit |
| `auto_submit_remote_only` | true | Only apply to remote roles |
| `auto_submit_max_per_run` | 50 | Cap per batch |
| `daily_apply_goal` | 50 | Target applications per `daily-apply` |

### LinkedIn

| Key | Description |
|-----|-------------|
| `linkedin_easy_apply_enabled` | Enable LinkedIn Easy Apply automation |
| `linkedin_easy_apply_priority_count` | Easy Apply jobs tried first each run |
| `linkedin_apply_min_score` | Min score for external LinkedIn ATS jobs |
| `linkedin_apply_keywords` | Title filter for apply queue |
| `linkedin_apply_emea_or_remote` | Only EMEA or remote LinkedIn jobs |
| `linkedin_email` / `linkedin_password` | Optional — for auto-login (session preferred) |

---

## LinkedIn setup

LinkedIn requires a logged-in browser session. **Use a dedicated session file, not your main browser.**

### One-time login

```bash
python login_linkedin.py
```

A Chromium window opens. Log in manually, then wait or close the window. Cookies are saved to `data/browser_profile/`.

Or:

```bash
python -m jobagent login-linkedin
```

### LinkedIn commands

```bash
# Discover + score LinkedIn jobs only
python -m jobagent linkedin-run

# Apply to LinkedIn queue (Easy Apply + external ATS)
python -m jobagent linkedin-apply

# Full daily batch (recommended)
python -m jobagent -v daily-apply
```

`daily-apply` runs in this order:

1. Discover + score new LinkedIn jobs
2. Reset previously failed jobs for retry
3. **Easy Apply batch** (highest priority)
4. **External ATS batch** (Greenhouse, Ashby, Personio, etc.)
5. **Greenhouse boards** from config if still under daily goal

---

## Review dashboard (manual approve)

For jobs you want to approve one-by-one before submit:

```bash
pip install flask playwright
playwright install chromium
python -m jobagent.dashboard
```

Open http://127.0.0.1:5000

- **Approve & submit** — fills and submits immediately (screenshot saved)
- **Skip** — never show again
- **Block company** — blacklist entire company

Environment overrides:

```bash
JOBAGENT_MIN_SCORE=75 python -m jobagent.dashboard
JOBAGENT_HEADLESS=1 python -m jobagent.dashboard   # hide browser
```

---

## CLI reference

```bash
python -m jobagent run              # discover + score (+ Greenhouse auto-submit)
python -m jobagent list             # show top scored jobs
python -m jobagent status           # counts by status (new, scored, applied, …)
python -m jobagent skip <job_id>    # never show this job again
python -m jobagent blacklist <co>   # hide all jobs from a company

python -m jobagent auto-submit      # apply to scored Greenhouse/Lever jobs
python -m jobagent linkedin-run     # LinkedIn discover + score
python -m jobagent linkedin-apply   # LinkedIn apply batch
python -m jobagent daily-apply      # full daily pipeline toward goal

python -m jobagent login-linkedin   # refresh LinkedIn session
```

Flags: `--config`, `--profile`, `--db`, `--min-score`, `-v` (verbose)

---

## Project layout

```
jobagent/
  __main__.py          CLI entry point
  pipeline.py          Orchestrator (discover, score, daily-apply)
  database.py          SQLite persistence
  models.py            Job, Status, ATS enums
  profile.py           Your master profile loader
  scorer.py            LLM + heuristic scoring
  generator.py         Resume/cover letter generation
  generator_prompts.py LLM prompts
  submitter.py         Playwright form filling (ATS + LinkedIn)
  linkedin_apply.py    Easy Apply routing and prioritization
  dashboard.py         Flask review UI
  sources/
    ats_boards.py      Greenhouse + Lever APIs
    feeds.py           RemoteOK, WWR, LinkedIn

config.json            Your API keys + board list (gitignored)
profile.json           Your resume data + rules (gitignored)
data/
  jobs.db              Job database
  browser_profile/     LinkedIn session cookies
output/
  resumes/             Generated HTML/PDF per job
  screenshots/         Pre-submit evidence
login_linkedin.py      One-shot LinkedIn login helper
```

---

## How scoring works

1. Job text is compared to your `target_titles`, `target_keywords`, and `skills`.
2. LLM scorer (if API key set) returns 0–100 + one-line rationale.
3. Jobs below 60 are auto-skipped.
4. `avoid_keywords` in the job title penalize or skip during discovery.

Without an API key, a free keyword heuristic runs instead — good enough to test the pipeline.

---

## How submission works

**Safety rules:**

1. **Confident fill only** — name, email, phone, resume upload use known selectors.
2. **Stop on unknown required fields** — returns `needs_review` instead of guessing.
3. **Screenshot before submit** — saved to `output/screenshots/`.
4. **Screening defaults** — notice period, visa, remote from `profile.preferences`.
5. **Already applied** — LinkedIn jobs you already submitted are detected and skipped.

**LinkedIn Easy Apply flow:** contact → resume → education (from profile) → screening → Review → Submit.

---

## Tips for sharing with friends

1. **Fork the repo**, never share your `config.json` / `profile.json`.
2. **Use Claude or ChatGPT once** to convert a resume → `profile.json` (see prompt above).
3. **Start small:** `greenhouse_boards` with 2–3 companies, run `python -m jobagent run`, check `list`.
4. **Watch the first applies:** run with `JOBAGENT_HEADLESS=0` or use the dashboard before going fully automatic.
5. **LinkedIn is optional** — Greenhouse/Lever/RemoteOK work without it.
6. **Set `daily_apply_goal`** to something realistic (e.g. 10–20) while tuning your profile.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `config not found` | `cp config.example.json config.json` |
| `profile not found` | `cp profile.example.json profile.json` |
| LinkedIn authwall / login page | Run `python login_linkedin.py` again |
| `Missing required field(s)` | Form has custom fields — use dashboard or add handler |
| Easy Apply stuck on screening | Add answers to `preferences` or improve `profile.json` |
| Low scores everywhere | Expand `target_keywords` and `skills` in profile |
| Rate limited on LinkedIn | Lower `linkedin_max_results`, add delay between runs |

---

## Contributing

AI agents and humans both follow the rules in [CLAUDE.md](CLAUDE.md). The primary goal of this repo is to raise the **proud %** of every pipeline stage toward 100% (see [PERFORMANCE.md](PERFORMANCE.md)).

**Git workflow — all changes go through a PR. Do not commit directly to `main`:**

1. Create a **new branch** off `main` (e.g. `fix/easy-apply-screening`).
2. Make a focused change — ideally one that raises a specific stage's proud %.
3. Commit with a message describing **which proud % it moves and why**.
4. Push the branch and **open a Pull Request**.
5. **Tresor reviews the PR and merges it to `main`.** Don't merge your own PR.

Never commit `config.json` or `profile.json` (they hold secrets and personal data).

---

## Disclaimer

Automating job applications — especially LinkedIn Easy Apply — may violate platform terms of service and risks account restrictions. Use at your own discretion. This tool is for personal use; the authors are not responsible for account actions taken by third-party platforms.
