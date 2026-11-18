"""Scoring.

Rates each job 0-100 against your profile with a one-line rationale. This is the
highest-value, lowest-risk place to use the LLM.

Two backends:
  - LLMScorer: calls the Anthropic API. Used when ANTHROPIC_API_KEY is set.
  - HeuristicScorer: keyword overlap. Zero-dependency fallback so the pipeline
    runs end-to-end today even with no key, and a cheap pre-filter.

Both implement score(job, profile) -> (score, rationale). The runner picks the
LLM one when a key is present, else the heuristic, so nothing blocks on setup.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Protocol

from .models import Job
from .profile import Profile

log = logging.getLogger(__name__)


class Scorer(Protocol):
    def score(self, job: Job, profile: Profile) -> tuple[float, str]: ...


class HeuristicScorer:
    """No-LLM baseline: overlap between job text and profile keywords/titles.
    Crude but deterministic, free, and good enough as a pre-filter."""

    def score(self, job: Job, profile: Profile) -> tuple[float, str]:
        haystack = f"{job.title} {job.description}".lower()
        kws = [k.lower() for k in (profile.target_keywords + profile.skills)]
        if not kws:
            return 50.0, "no profile keywords set; neutral score"

        hits = sorted({k for k in kws if k and k in haystack})
        base = 100.0 * len(hits) / max(len(set(kws)), 1)

        # Title alignment bonus.
        if any(t.lower() in job.title.lower() for t in profile.target_titles):
            base = min(100.0, base + 20)

        # Avoid-list penalty.
        if any(a.lower() in haystack for a in profile.avoid_keywords):
            base = max(0.0, base - 30)

        rationale = (
            f"matched {len(hits)} keyword(s): {', '.join(hits[:5])}"
            if hits else "no keyword overlap"
        )
        return round(base, 1), rationale


class LLMScorer:
    """Anthropic-backed scorer. Asks for strict JSON so we can parse reliably."""

    MODEL = "claude-sonnet-4-6"

    def __init__(self, api_key: str | None = None):
        # Imported lazily so the package works without the SDK installed.
        from anthropic import Anthropic  # type: ignore

        self.client = Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    def score(self, job: Job, profile: Profile) -> tuple[float, str]:
        prompt = self._build_prompt(job, profile)
        try:
            resp = self.client.messages.create(
                model=self.MODEL,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            return self._parse(text)
        except Exception as e:
            log.warning("LLM scoring failed for %s; falling back: %s", job.job_id, e)
            return HeuristicScorer().score(job, profile)

    def _build_prompt(self, job: Job, profile: Profile) -> str:
        desc = job.description[:4000]
        return (
            "You are screening a job for a specific candidate. Rate how well it "
            "fits, 0-100, and give a one-sentence rationale.\n\n"
            "Respond with ONLY a JSON object, no prose, no code fences:\n"
            '{"score": <number 0-100>, "rationale": "<one sentence>"}\n\n'
            f"=== CANDIDATE ===\n{profile.to_scoring_blurb()}\n\n"
            f"=== JOB ===\nTitle: {job.title}\nCompany: {job.company}\n"
            f"Location: {job.location}\nDescription: {desc}\n"
        )

    @staticmethod
    def _parse(text: str) -> tuple[float, str]:
        cleaned = re.sub(r"```(?:json)?", "", text).strip()
        try:
            obj = json.loads(cleaned)
            return float(obj["score"]), str(obj.get("rationale", ""))[:300]
        except Exception:
            m = re.search(r"(\d{1,3})", cleaned)
            return (float(m.group(1)) if m else 50.0), cleaned[:200]


class GeminiScorer:
    """Gemini-backed scorer. Uses google-generativeai with structured JSON output."""

    MODEL = "gemini-2.5-flash"

    def __init__(self, api_key: str | None = None):
        import google.generativeai as genai
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        genai.configure(api_key=key)
        self.model = genai.GenerativeModel(self.MODEL)

    def score(self, job: Job, profile: Profile) -> tuple[float, str]:
        prompt = self._build_prompt(job, profile)
        try:
            import google.generativeai as genai
            resp = self.model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    response_mime_type="application/json"
                )
            )
            return self._parse(resp.text)
        except Exception as e:
            log.warning("Gemini scoring failed for %s; falling back: %s", job.job_id, e)
            return HeuristicScorer().score(job, profile)

    def _build_prompt(self, job: Job, profile: Profile) -> str:
        desc = job.description[:4000]
        return (
            "You are screening a job for a specific candidate. Rate how well it "
            "fits, 0-100, and give a one-sentence rationale.\n\n"
            "Respond with a JSON object containing 'score' and 'rationale':\n"
            '{"score": <number 0-100>, "rationale": "<one sentence>"}\n\n'
            f"=== CANDIDATE ===\n{profile.to_scoring_blurb()}\n\n"
            f"=== JOB ===\nTitle: {job.title}\nCompany: {job.company}\n"
            f"Location: {job.location}\nDescription: {desc}\n"
        )

    @staticmethod
    def _parse(text: str) -> tuple[float, str]:
        try:
            obj = json.loads(text.strip())
            return float(obj["score"]), str(obj.get("rationale", ""))[:300]
        except Exception:
            cleaned = re.sub(r"```(?:json)?", "", text).strip()
            try:
                obj = json.loads(cleaned)
                return float(obj["score"]), str(obj.get("rationale", ""))[:300]
            except Exception:
                m = re.search(r"(\d{1,3})", cleaned)
                return (float(m.group(1)) if m else 50.0), cleaned[:200]


class ChatGPTScorer:
    """OpenAI ChatGPT-backed scorer."""

    MODEL = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")

    def score(self, job: Job, profile: Profile) -> tuple[float, str]:
        import requests
        prompt = self._build_prompt(job, profile)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }
        try:
            res = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=15)
            res.raise_for_status()
            res_json = res.json()
            text = res_json["choices"][0]["message"]["content"]
            return self._parse(text)
        except Exception as e:
            log.warning("ChatGPT scoring failed for %s; falling back: %s", job.job_id, e)
            return HeuristicScorer().score(job, profile)

    def _build_prompt(self, job: Job, profile: Profile) -> str:
        desc = job.description[:4000]
        return (
            "You are screening a job for a specific candidate. Rate how well it "
            "fits, 0-100, and give a one-sentence rationale.\n\n"
            "Respond with a JSON object containing 'score' and 'rationale':\n"
            '{"score": <number 0-100>, "rationale": "<one sentence>"}\n\n'
            f"=== CANDIDATE ===\n{profile.to_scoring_blurb()}\n\n"
            f"=== JOB ===\nTitle: {job.title}\nCompany: {job.company}\n"
            f"Location: {job.location}\nDescription: {desc}\n"
        )

    @staticmethod
    def _parse(text: str) -> tuple[float, str]:
        try:
            obj = json.loads(text.strip())
            return float(obj["score"]), str(obj.get("rationale", ""))[:300]
        except Exception:
            cleaned = re.sub(r"```(?:json)?", "", text).strip()
            try:
                obj = json.loads(cleaned)
                return float(obj["score"]), str(obj.get("rationale", ""))[:300]
            except Exception:
                m = re.search(r"(\d{1,3})", cleaned)
                return (float(m.group(1)) if m else 50.0), cleaned[:200]


class GroqScorer:
    """Groq-backed scorer. Same OpenAI-compatible chat-completions shape as
    ChatGPTScorer, just a different endpoint/model — Groq hosts open models
    (Llama) at very low latency and is used as the default fast/free-tier path."""

    MODEL = "llama-3.3-70b-versatile"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not self.api_key:
            raise ValueError("GROQ_API_KEY environment variable not set")

    def score(self, job: Job, profile: Profile) -> tuple[float, str]:
        import requests
        prompt = self._build_prompt(job, profile)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }
        try:
            res = requests.post(self.URL, json=payload, headers=headers, timeout=15)
            res.raise_for_status()
            res_json = res.json()
            text = res_json["choices"][0]["message"]["content"]
            return self._parse(text)
        except Exception as e:
            log.warning("Groq scoring failed for %s; falling back: %s", job.job_id, e)
            return HeuristicScorer().score(job, profile)

    def _build_prompt(self, job: Job, profile: Profile) -> str:
        desc = job.description[:4000]
        return (
            "You are screening a job for a specific candidate. Rate how well it "
            "fits, 0-100, and give a one-sentence rationale.\n\n"
            "Respond with a JSON object containing 'score' and 'rationale':\n"
            '{"score": <number 0-100>, "rationale": "<one sentence>"}\n\n'
            f"=== CANDIDATE ===\n{profile.to_scoring_blurb()}\n\n"
            f"=== JOB ===\nTitle: {job.title}\nCompany: {job.company}\n"
            f"Location: {job.location}\nDescription: {desc}\n"
        )

    @staticmethod
    def _parse(text: str) -> tuple[float, str]:
        try:
            obj = json.loads(text.strip())
            return float(obj["score"]), str(obj.get("rationale", ""))[:300]
        except Exception:
            cleaned = re.sub(r"```(?:json)?", "", text).strip()
            try:
                obj = json.loads(cleaned)
                return float(obj["score"]), str(obj.get("rationale", ""))[:300]
            except Exception:
                m = re.search(r"(\d{1,3})", cleaned)
                return (float(m.group(1)) if m else 50.0), cleaned[:200]


def get_scorer() -> Scorer:
    """Pick the best available scorer without making setup mandatory."""
    if os.environ.get("GROQ_API_KEY"):
        try:
            return GroqScorer()
        except Exception as e:
            log.warning("could not init Groq scorer (%s); falling back", e)
    if os.environ.get("OPENAI_API_KEY"):
        try:
            return ChatGPTScorer()
        except Exception as e:
            log.warning("could not init ChatGPT scorer (%s); falling back", e)
    if os.environ.get("GEMINI_API_KEY"):
        try:
            return GeminiScorer()
        except Exception as e:
            log.warning("could not init Gemini scorer (%s); falling back", e)
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return LLMScorer()
        except Exception as e:
            log.warning("could not init LLM scorer (%s); using heuristic", e)
    return HeuristicScorer()
