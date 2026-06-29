# Setup guide

Copy-paste setup for developers. For full docs see [README.md](README.md).

---

## Checklist

- [ ] Python 3.9+ installed
- [ ] Repo cloned, venv created, dependencies installed
- [ ] `config.json` and `profile.json` created (not committed)
- [ ] At least one LLM API key added (optional but recommended)
- [ ] Profile filled from your real resume
- [ ] Job sources configured (Greenhouse boards and/or LinkedIn)
- [ ] LinkedIn session saved (if using LinkedIn)
- [ ] Test run: `python -m jobagent run` → `python -m jobagent list`

---

## 1. Install

```bash
git clone <your-repo-url> jobagent
cd jobagent

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

---

## 2. Create your files

```bash
cp config.example.json config.json
cp profile.example.json profile.json
```

`config.json` and `profile.json` are gitignored. **Do not commit them.**

---

## 3. Add an API key

Open `config.json` and set **one** key (pick whichever you already use):

```json
"anthropic_api_key": "sk-ant-...",
```

or

```json
"openai_api_key": "sk-...",
```

or

```json
"gemini_api_key": "..."
```

If no key is set, the agent still runs with free keyword scoring and template resumes — good for testing, weaker tailoring.

---

## 4. Build `profile.json` with Claude (copy-paste)

Open Claude, ChatGPT, or Gemini. Paste this prompt, then paste your resume below it.

```
Convert my resume into a JSON profile for a job-search automation tool.

Output valid JSON only (no markdown fences). Use this exact structure:

{
  "name": "",
  "email": "",
  "phone": "",
  "location": "",
  "headline": "",
  "summary": "",
  "target_titles": [],
  "target_keywords": [],
  "avoid_keywords": [],
  "skills": [],
  "experience": [
    {
      "company": "",
      "title": "",
      "dates": "",
      "location": "",
      "bullets": []
    }
  ],
  "education": [
    {
      "school": "",
      "degree": "",
      "dates": "",
      "location": ""
    }
  ],
  "links": {
    "portfolio": "",
    "github": "",
    "linkedin": ""
  },
  "certifications": [],
  "languages": [],
  "preferences": {
    "remote_ok": true,
    "open_to_relocation": false,
    "notice_period": "2 weeks",
    "remote_preference": "Yes — seeking a fully remote role",
    "visa_sponsorship_required": false
  }
}

Rules:
- Use ONLY facts from my resume. Do not invent employers, dates, titles, or metrics.
- target_titles: 4–8 roles I should be matched against
- target_keywords: technologies and skills I want to match (lowercase)
- avoid_keywords: job types I never want (e.g. internship, sales, recruiter, unpaid)
- bullets: achievement-focused, 2–4 per role
- preferences: realistic defaults for application forms (notice period, visa, remote)

My resume:
[PASTE RESUME HERE]
```

Save the JSON response as `profile.json` (replace the example file).

### Quick manual edits after generation

| Field | What to tune |
|-------|----------------|
| `target_titles` | Roles you actually want |
| `avoid_keywords` | Auto-skip bad fits by job title |
| `preferences.notice_period` | e.g. `"7 days"`, `"2 weeks"`, `"Immediately"` |
| `preferences.visa_sponsorship_required` | `true` if you need sponsorship |
| `preferences.remote_preference` | Shown on remote/hybrid screening questions |
| `links.linkedin` | Required for many Easy Apply forms |

---

## 5. Configure job search (`config.json`)

### Minimum (Greenhouse only, no LinkedIn)

```json
{
  "greenhouse_boards": ["stripe", "figma"],
  "lever_boards": [],
  "enable_remoteok": true,
  "remoteok_keywords": ["frontend", "react"],
  "enable_linkedin": false,
  "daily_apply_goal": 10,
  "auto_submit_score_threshold": 80
}
```

Find board tokens from careers URLs: `boards.greenhouse.io/acme` → `"acme"`.

### With LinkedIn

```json
{
  "enable_linkedin": true,
  "linkedin_keywords": ["Frontend Engineer", "Product Designer"],
  "linkedin_locations": ["Remote", "EMEA", "United Kingdom"],
  "linkedin_easy_apply_enabled": true,
  "linkedin_apply_emea_or_remote": true,
  "daily_apply_goal": 20
}
```

Start with `daily_apply_goal: 10` until you trust the results, then raise it.

---

## 6. LinkedIn login (one time)

Only needed if `enable_linkedin: true`.

```bash
python login_linkedin.py
```

A browser opens. Log in to LinkedIn manually. Wait ~30 seconds or close the window. Session is saved to `data/browser_profile/`.

Re-run if you see authwall/login errors during apply.

---

## 7. Verify setup

```bash
# Discover jobs + score (no applying yet)
python -m jobagent run

# See matches
python -m jobagent list --min-score 70

# Check database state
python -m jobagent status
```

You should see jobs with scores and rationales. If the list is empty, add more `greenhouse_boards` or enable LinkedIn discovery.

---

## 8. Start applying

### Automatic (batch)

```bash
python -m jobagent -v daily-apply
```

This discovers, scores, and applies toward `daily_apply_goal` in `config.json`.

### Manual review first (recommended for first day)

```bash
python -m jobagent.dashboard
```

Open http://127.0.0.1:5000 — approve each job before submit.

### Greenhouse/Lever only (no LinkedIn)

```bash
python -m jobagent auto-submit
```

---

## 9. Tune your rules

All in `profile.json`:

```json
"avoid_keywords": ["internship", "unpaid", "sales", "recruiter"],
"preferences": {
  "remote_ok": true,
  "notice_period": "7 days",
  "visa_sponsorship_required": true,
  "remote_preference": "Yes — seeking a fully remote role"
}
```

All in `config.json`:

```json
"auto_submit_score_threshold": 80,
"auto_submit_remote_only": true,
"linkedin_apply_min_score": 75,
"daily_apply_goal": 20
```

Lower thresholds = more applications, more noise. Raise them if you're getting poor matches.

---

## 10. Common fixes

| Symptom | Fix |
|---------|-----|
| No jobs found | Add `greenhouse_boards` or enable LinkedIn |
| All scores low | Expand `target_keywords` and `skills` in profile |
| LinkedIn login page | Run `python login_linkedin.py` again |
| `config not found` | `cp config.example.json config.json` |
| Applies fail on custom forms | Use dashboard first; check `output/screenshots/` |
| Want to pause applying | Set `"auto_submit_enabled": false` in config |

---

## File reference

| File | Purpose |
|------|---------|
| `config.json` | API keys, job boards, apply limits (secret) |
| `profile.json` | Your resume data + rules (personal) |
| `data/jobs.db` | Job database (auto-created) |
| `data/browser_profile/` | LinkedIn session cookies |
| `output/resumes/` | Generated resumes per job |
| `output/screenshots/` | Pre-submit screenshots |

---

## Share safely

When sharing this repo with friends:

1. They clone and run steps 1–8 above.
2. They create their **own** `config.json` and `profile.json`.
3. Never share files containing API keys or LinkedIn passwords.
4. LinkedIn automation may violate LinkedIn ToS — use at your own risk.
5. Share [PERFORMANCE.md](PERFORMANCE.md) so they see honest stage-by-stage scores and known challenges.

---

## Agent performance

See **[PERFORMANCE.md](PERFORMANCE.md)** for proud % scores per pipeline stage (scraping, scoring, resume generation, applying), current challenges, and prompts for AI agents to push each stage toward 100%.
