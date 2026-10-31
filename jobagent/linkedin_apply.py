"""LinkedIn apply routing, prioritization, and Easy Apply detection.

Standard apply order for any LinkedIn-sourced job:
  1. Easy Apply (in-app modal) — fastest, highest priority
  2. safety/go → direct ATS URL (Greenhouse, Ashby, HiBob, Lever, …)
  3. Apply button / popup → external career page → resolve embedded ATS
"""

from __future__ import annotations

import re
from typing import Optional

from .models import ATS, Job

# Lower = higher priority
TIER_EASY_APPLY = 0
TIER_GREENHOUSE = 1
TIER_ASHBY = 2
TIER_HIBOB = 3
TIER_LEVER = 4
TIER_PERSONIO = 4
TIER_SMARTRECRUITERS = 4
TIER_OTHER_KNOWN = 5
TIER_UNKNOWN = 9

_ATS_URL_MARKERS = {
    TIER_GREENHOUSE: ("greenhouse.io", "gh_jid=", "easyapply.jobs"),
    TIER_ASHBY: ("ashbyhq.com",),
    TIER_HIBOB: ("hibob.com", "careers.hibob"),
    TIER_LEVER: ("lever.co",),
    TIER_PERSONIO: ("personio.com", "personio.de"),
    TIER_SMARTRECRUITERS: ("smartrecruiters.com",),
    TIER_OTHER_KNOWN: ("workable.com", "teamtailor.com", "recruitee.com"),
}


def linkedin_job_id(job: Job) -> Optional[str]:
    for url in (job.apply_url, job.source_url):
        if not url:
            continue
        m = re.search(r"/jobs/view/(\d+)", url)
        if m:
            return m.group(1)
    return None


def easy_apply_url(job: Job) -> Optional[str]:
    jid = linkedin_job_id(job)
    if not jid:
        return None
    return f"https://www.linkedin.com/jobs/view/{jid}/apply/?openSDUIApplyFlow=true"


def is_easy_apply_job(job: Job) -> bool:
    if job.ats == ATS.LINKEDIN:
        return True
    blob = f"{job.title} {job.description or ''} {job.apply_url or ''}".lower()
    return "easy apply" in blob or "easy-apply" in blob


def predict_ats_tier(job: Job) -> int:
    if is_easy_apply_job(job):
        return TIER_EASY_APPLY
    urls = " ".join(filter(None, (job.apply_url, job.source_url, job.description or ""))).lower()
    for tier, markers in _ATS_URL_MARKERS.items():
        if any(m in urls for m in markers):
            return tier
    if job.ats == ATS.GREENHOUSE:
        return TIER_GREENHOUSE
    if job.ats == ATS.ASHBY:
        return TIER_ASHBY
    if job.ats == ATS.LEVER:
        return TIER_LEVER
    if job.ats == ATS.PERSONIO:
        return TIER_PERSONIO
    if job.ats == ATS.SMARTRECRUITERS:
        return TIER_SMARTRECRUITERS
    if job.ats in (ATS.WORKABLE, ATS.TEAMTAILOR, ATS.RECRUITEE):
        return TIER_OTHER_KNOWN
    return TIER_UNKNOWN


def priority_score(job: Job) -> float:
    """Higher score = apply sooner. Easy Apply and direct ATS beat unknown career pages."""
    tier = predict_ats_tier(job)
    tier_bonus = {
        TIER_EASY_APPLY: 10_000,
        TIER_GREENHOUSE: 5_000,
        TIER_ASHBY: 4_500,
        TIER_HIBOB: 4_000,
        TIER_LEVER: 3_500,
        TIER_PERSONIO: 3_200,
        TIER_SMARTRECRUITERS: 3_200,
        TIER_OTHER_KNOWN: 2_000,
        TIER_UNKNOWN: 0,
    }.get(tier, 0)
    score = job.score or 0
    # Deprioritize jobs that already failed (notes set while still scored is rare; errors reset)
    if job.notes and any(x in job.notes.lower() for x in ("missing required", "could not locate")):
        tier_bonus -= 1_500
    return tier_bonus + score


def sort_linkedin_jobs(jobs: list[Job]) -> list[Job]:
    return sorted(jobs, key=priority_score, reverse=True)
