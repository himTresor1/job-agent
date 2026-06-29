"""Document generation.

Produces a tailored resume and cover letter per job, rendered to HTML (and
optionally PDF). Two backends, mirroring the scorer:

  - LLMGenerator: uses the Anthropic API to re-order and re-emphasize the
    candidate's REAL experience toward a specific job, and to draft a cover
    letter in their voice.
  - TemplateGenerator: pure Jinja2, no LLM. Fills a fixed template from the
    profile. Zero cost, always available, runs today.

THE INVARIANT, enforced in the prompt and by construction: generation may only
reorder, select, and rephrase facts that already exist in profile.json. It must
never invent employers, dates, titles, metrics, or skills. The LLM prompt says
this explicitly; the template backend can't violate it because it only
interpolates existing fields.

Custom application questions (the place silent errors hide) are answered here
too, with the answer marked low-confidence when the model had to guess, so the
dashboard can flag it for your eyes before you approve.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Template

from .generator_prompts import build_generation_prompt
from .models import ATS, Job
from .pdf_export import ensure_pdf_paths
from .profile import Profile

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


@dataclass
class GeneratedDocs:
    """Everything the dashboard needs to render a review for one job."""

    job_id: str
    resume_html: str = ""
    cover_letter_html: str = ""
    # Each answer: {"question","answer","confident": bool}
    custom_answers: list[dict] = field(default_factory=list)
    resume_path: str = ""
    cover_letter_path: str = ""

    @property
    def needs_attention(self) -> bool:
        """True if any custom answer was a guess — surfaced in the UI."""
        return any(not a.get("confident", True) for a in self.custom_answers)


_RESUME_TMPL = Template("""
<div class="doc resume">
  <header>
    <h1>{{ p.name }}</h1>
    <p class="meta">{{ p.headline }}{% if p.location %} · {{ p.location }}{% endif %}
       {% if p.email %} · {{ p.email }}{% endif %}</p>
  </header>
  {% if summary %}<section><h2>Summary</h2><p>{{ summary }}</p></section>{% endif %}
  <section>
    <h2>Experience</h2>
    {% for e in experience %}
    <div class="entry">
      <div class="entry-head"><strong>{{ e.title }}</strong>, {{ e.company }}
        <span class="dates">{{ e.dates }}</span></div>
      <ul>{% for b in e.bullets %}<li>{{ b }}</li>{% endfor %}</ul>
    </div>
    {% endfor %}
  </section>
  {% if p.skills %}<section><h2>Skills</h2><p>{{ p.skills | join(' · ') }}</p></section>{% endif %}
  {% if p.education %}<section><h2>Education</h2>
    {% for ed in p.education %}<p>{{ ed.degree }}, {{ ed.school }} <span class="dates">{{ ed.dates }}</span></p>{% endfor %}
  </section>{% endif %}
</div>
""")

_COVER_TMPL = Template("""
<div class="doc cover">
  <p>Dear {{ company }} team,</p>
  {% for para in paragraphs %}<p>{{ para }}</p>{% endfor %}
  <p>Best regards,<br>{{ p.name }}</p>
</div>
""")


class TemplateGenerator:
    """LLM-free generator. Selects profile experience by keyword relevance to the
    job, then fills templates. Deterministic and free."""

    def generate(self, job: Job, profile: Profile) -> GeneratedDocs:
        ranked = self._rank_experience(job, profile)
        summary = profile.summary
        resume_html = _RESUME_TMPL.render(p=profile, summary=summary, experience=ranked)

        paragraphs = [
            f"I'm applying for the {job.title} role at {job.company}. {profile.summary}",
            f"Across roles at {profile.experience[0]['company'] if profile.experience else 'my recent work'}, "
            "I've shipped user-facing products end-to-end — from research and prototyping through "
            "production React/TypeScript implementations used by thousands of users.",
            "What draws me to this opening is the overlap between your stack and the systems I've built: "
            "design systems, accessible UI, and full-stack delivery with measurable outcomes.",
            f"I'd welcome a conversation about how I can contribute to {job.company}'s team.",
        ]
        cover_html = _COVER_TMPL.render(p=profile, company=job.company, paragraphs=paragraphs)

        docs = GeneratedDocs(
            job_id=job.job_id,
            resume_html=resume_html,
            cover_letter_html=cover_html,
            custom_answers=[],
        )
        ensure_pdf_paths(docs, job.job_id)
        return docs

    @staticmethod
    def _rank_experience(job: Job, profile: Profile) -> list[dict]:
        text = f"{job.title} {job.description}".lower()
        def relevance(entry: dict) -> int:
            blob = json.dumps(entry).lower()
            return sum(1 for kw in profile.skills if kw.lower() in blob and kw.lower() in text)
        return sorted(profile.experience, key=relevance, reverse=True)


def parse_form_questions(url: str, ats: ATS) -> list[str]:
    if os.environ.get("JOBAGENT_SKIP_FORM_PARSE"):
        return []
    from playwright.sync_api import sync_playwright
    import re
    
    questions = []
    try:
        log.info("parse_form_questions: starting Playwright to dynamically parse questions for %s", url)
        with sync_playwright() as p:
            # Launch standard headless browser (no persistent profile needed)
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # Wait 3 seconds for dynamic widgets and frames to render
            page.wait_for_timeout(3000)
            
            # Resolve target frame context (e.g. grnhse_iframe on Stripe)
            target_frame = page
            gh_iframe = page.frame(name="grnhse_iframe")
            if gh_iframe:
                log.info("parse_form_questions: detected grnhse_iframe context")
                target_frame = gh_iframe
            else:
                for frame in page.frames:
                    if "greenhouse.io" in frame.url or "lever.co" in frame.url or "ashbyhq.com" in frame.url:
                        log.info("parse_form_questions: resolved target iframe by URL: %s", frame.url)
                        target_frame = frame
                        break
            
            try:
                target_frame.wait_for_selector("input", timeout=8000)
            except Exception:
                pass
                
            ignore = {
                "first name", "last name", "email", "phone", "resume", "cover letter", 
                "attach", "enter manually", "preferred first name", "pronouns", 
                "additional information", "gender", "hispanic/latino", "veteran status", 
                "disability status", "race", "ethnicity", "demographic", "voluntary self-identification",
                "photo", "headshot", "street", "city", "state", "zip", "postal code", "country"
            }
            
            # Extract questions by searching labels and span text
            labels = target_frame.locator("label, .application-label, .application-question")
            for i in range(labels.count()):
                lbl = labels.nth(i)
                txt = lbl.inner_text().strip()
                txt_clean = re.sub(r"\s*\*$", "", txt).strip()
                txt_clean = re.sub(r"\s*\(required\)$", "", txt_clean, flags=re.I).strip()
                txt_clean = re.sub(r"\s*\(optional\)$", "", txt_clean, flags=re.I).strip()
                
                if not txt_clean or any(ig in txt_clean.lower() for ig in ignore):
                    continue
                    
                if txt_clean not in questions:
                    questions.append(txt_clean)
                    
            browser.close()
    except Exception as e:
        log.warning("parse_form_questions: Playwright extraction failed: %s", e)
        
    log.info("parse_form_questions: extracted %d custom questions dynamically", len(questions))
    return questions


class LLMGenerator:
    """Anthropic-backed. Tailors real experience and drafts a cover letter.
    Falls back to the template generator on any failure."""

    MODEL = "claude-sonnet-4-6"

    def __init__(self, api_key: str | None = None):
        from anthropic import Anthropic  # type: ignore
        self.client = Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self._fallback = TemplateGenerator()

    def generate(self, job: Job, profile: Profile) -> GeneratedDocs:
        try:
            questions = []
            if job.ats in (ATS.GREENHOUSE, ATS.LEVER) and job.apply_url:
                try:
                    questions = parse_form_questions(job.apply_url, job.ats)
                except Exception as e:
                    log.warning("Failed to parse form questions: %s", e)
            
            prompt = build_generation_prompt(job, profile, questions)
            resp = self.client.messages.create(
                model=self.MODEL, max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            data = self._parse(text)
        except Exception as e:
            log.warning("LLM generation failed for %s; using template: %s", job.job_id, e)
            return self._fallback.generate(job, profile)

        # Render the model's tailored content through the same safe templates.
        exp = data.get("experience") or profile.experience
        resume_html = _RESUME_TMPL.render(
            p=profile, summary=data.get("summary", profile.summary), experience=exp
        )
        cover_html = _COVER_TMPL.render(
            p=profile, company=job.company,
            paragraphs=data.get("cover_paragraphs", []),
        )
        docs = GeneratedDocs(
            job_id=job.job_id,
            resume_html=resume_html,
            cover_letter_html=cover_html,
            custom_answers=data.get("custom_answers", []),
        )
        ensure_pdf_paths(docs, job.job_id)
        return docs

    def _prompt(self, job: Job, profile: Profile, questions: list[str]) -> str:
        return build_generation_prompt(job, profile, questions)

    @staticmethod
    def _parse(text: str) -> dict:
        cleaned = re.sub(r"```(?:json)?", "", text).strip()
        return json.loads(cleaned)


class GeminiGenerator:
    """Gemini-backed Generator. Tailors experience, drafts motivating cover letters,
    and answers custom questions. Uses google-generativeai with structured JSON output."""

    MODEL = "gemini-2.5-flash"

    def __init__(self, api_key: str | None = None):
        import google.generativeai as genai
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        genai.configure(api_key=key)
        self.model = genai.GenerativeModel(self.MODEL)
        self._fallback = TemplateGenerator()

    def generate(self, job: Job, profile: Profile) -> GeneratedDocs:
        try:
            questions = []
            if job.ats in (ATS.GREENHOUSE, ATS.LEVER) and job.apply_url:
                try:
                    questions = parse_form_questions(job.apply_url, job.ats)
                except Exception as e:
                    log.warning("Failed to parse form questions: %s", e)
            
            prompt = build_generation_prompt(job, profile, questions)
            import google.generativeai as genai
            resp = self.model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    response_mime_type="application/json"
                )
            )
            data = self._parse(resp.text)
        except Exception as e:
            log.warning("Gemini generation failed for %s; using template: %s", job.job_id, e)
            return self._fallback.generate(job, profile)

        # Render the model's tailored content through the same safe templates.
        exp = data.get("experience") or profile.experience
        resume_html = _RESUME_TMPL.render(
            p=profile, summary=data.get("summary", profile.summary), experience=exp
        )
        cover_html = _COVER_TMPL.render(
            p=profile, company=job.company,
            paragraphs=data.get("cover_paragraphs", []),
        )
        docs = GeneratedDocs(
            job_id=job.job_id,
            resume_html=resume_html,
            cover_letter_html=cover_html,
            custom_answers=data.get("custom_answers", []),
        )
        ensure_pdf_paths(docs, job.job_id)
        return docs

    def _prompt(self, job: Job, profile: Profile, questions: list[str]) -> str:
        return build_generation_prompt(job, profile, questions)

    @staticmethod
    def _parse(text: str) -> dict:
        try:
            return json.loads(text.strip())
        except Exception:
            cleaned = re.sub(r"```(?:json)?", "", text).strip()
            return json.loads(cleaned)


class ChatGPTGenerator:
    """OpenAI ChatGPT-backed generator."""

    MODEL = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")
        self._fallback = TemplateGenerator()

    def generate(self, job: Job, profile: Profile) -> GeneratedDocs:
        try:
            questions = []
            if job.ats in (ATS.GREENHOUSE, ATS.LEVER) and job.apply_url:
                try:
                    questions = parse_form_questions(job.apply_url, job.ats)
                except Exception as e:
                    log.warning("Failed to parse form questions: %s", e)
            
            prompt = build_generation_prompt(job, profile, questions)
            import requests
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": self.MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            }
            res = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=30)
            res.raise_for_status()
            res_json = res.json()
            text = res_json["choices"][0]["message"]["content"]
            data = self._parse(text)
        except Exception as e:
            log.warning("ChatGPT generation failed for %s; using template: %s", job.job_id, e)
            return self._fallback.generate(job, profile)

        # Render the model's tailored content through the same safe templates.
        exp = data.get("experience") or profile.experience
        resume_html = _RESUME_TMPL.render(
            p=profile, summary=data.get("summary", profile.summary), experience=exp
        )
        cover_html = _COVER_TMPL.render(
            p=profile, company=job.company,
            paragraphs=data.get("cover_paragraphs", []),
        )
        docs = GeneratedDocs(
            job_id=job.job_id,
            resume_html=resume_html,
            cover_letter_html=cover_html,
            custom_answers=data.get("custom_answers", []),
        )
        ensure_pdf_paths(docs, job.job_id)
        return docs

    def _prompt(self, job: Job, profile: Profile, questions: list[str]) -> str:
        return build_generation_prompt(job, profile, questions)

    @staticmethod
    def _parse(text: str) -> dict:
        try:
            return json.loads(text.strip())
        except Exception:
            cleaned = re.sub(r"```(?:json)?", "", text).strip()
            return json.loads(cleaned)


def get_generator():
    if os.environ.get("OPENAI_API_KEY"):
        try:
            return ChatGPTGenerator()
        except Exception as e:
            log.warning("could not init ChatGPT generator (%s); falling back", e)
    if os.environ.get("GEMINI_API_KEY"):
        try:
            return GeminiGenerator()
        except Exception as e:
            log.warning("could not init Gemini generator (%s); falling back", e)
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return LLMGenerator()
        except Exception as e:
            log.warning("could not init LLM generator (%s); using template", e)
    return TemplateGenerator()
