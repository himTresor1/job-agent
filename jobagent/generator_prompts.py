"""Shared LLM prompts for resume + cover letter generation.

Inspired by offerloop, applypilot-ai, and engineering-resume tailoring workflows:
evidence-only facts, STAR bullets, company-specific motivation, longer cover letters.
"""

from __future__ import annotations

import json

from .models import Job
from .profile import Profile


def build_generation_prompt(job: Job, profile: Profile, questions: list[str]) -> str:
    questions_str = ""
    if questions:
        questions_str = (
            "\n=== CUSTOM APPLICATION QUESTIONS TO ANSWER ===\n"
            + "\n".join(f"- {q}" for q in questions)
        )

    prefs = profile.preferences or {}
    return (
        "You are an expert career coach tailoring a job application for a specific role.\n\n"
        "=== RESUME RULES ===\n"
        "- Use ONLY facts from the candidate profile. Never invent employers, titles, dates, metrics, or skills.\n"
        "- Select the 4-6 most relevant roles (not every role). Drop irrelevant entries entirely.\n"
        "- For each kept role, write 4-6 bullets using STAR format (situation → action → result).\n"
        "- Lead bullets with strong verbs and quantified outcomes already in the profile.\n"
        "- Mirror keywords from the job description naturally (React, TypeScript, design systems, etc.).\n"
        "- Summary: 3-4 sentences tying the candidate's arc to THIS role and company.\n\n"
        "=== COVER LETTER RULES ===\n"
        "- Write 4-5 substantial paragraphs (not 2 short ones). Total length: 320-450 words.\n"
        "- Paragraph 1: Hook — why this role at this company now (reference a specific product, mission, or tech).\n"
        "- Paragraph 2: Map 2-3 profile achievements directly to job requirements with metrics.\n"
        "- Paragraph 3: Technical + design depth — stack, systems thinking, cross-functional work.\n"
        "- Paragraph 4: Motivation — why this company specifically (not generic 'excited to apply').\n"
        "- Paragraph 5: Close with confidence and availability.\n"
        "- Voice: warm, professional, human. Ban clichés: thrilled, passionate, delighted, synergy, rockstar.\n\n"
        "=== CUSTOM QUESTION DEFAULTS (mark confident: true) ===\n"
        f"- Work authorization: Yes (candidate location: {profile.location or 'Rwanda'})\n"
        "- Visa sponsorship needed: No\n"
        "- Notice period: 2 weeks or Immediate\n"
        "- Salary: Negotiable or market-appropriate for remote roles ($80k-$130k range)\n"
        "- Consent/recording/compliance questions: Yes / I agree\n"
        f"- Remote preference: {prefs.get('remote_ok', True)}\n"
        "- For unknown questions, answer from profile facts; set confident:false only if guessing.\n\n"
        "Respond with ONLY valid JSON (no markdown fences):\n"
        "{\n"
        '  "summary": "3-4 sentence tailored summary",\n'
        '  "experience": [{"company","title","dates","bullets":["4-6 STAR bullets each"]}],\n'
        '  "cover_paragraphs": ["para1","para2","para3","para4","para5"],\n'
        '  "custom_answers": [{"question","answer","confident":true|false}]\n'
        "}\n"
        f"{questions_str}\n\n"
        f"=== CANDIDATE PROFILE ===\n{json.dumps(profile.__dict__, indent=2)}\n\n"
        f"=== JOB ===\nTitle: {job.title}\nCompany: {job.company}\n"
        f"Location: {job.location}\nDescription:\n{job.description[:6000]}\n"
    )
