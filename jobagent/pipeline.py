"""Pipeline orchestrator.

Wires the foundation stages together:

    discover  -> fetch from every configured source
    persist   -> upsert into the DB, deduping and respecting your decisions
    score     -> rate new/updated jobs against your profile

The submission layer (assisted / gated / whitelist-auto) plugs in AFTER this,
consuming jobs the DB marks as scored/approved. It's deliberately not here yet —
that's the decision you'll make once you've seen real jobs flow through.
"""

from __future__ import annotations

import logging

from .database import Database
from .models import Status
from .profile import Profile
from .scorer import Scorer, get_scorer
from .sources import build_sources

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, db: Database, profile: Profile, config: dict, scorer: Scorer | None = None):
        self.db = db
        self.profile = profile
        self.config = config
        self.scorer = scorer or get_scorer()

    def discover(self) -> dict[str, int]:
        """Fetch from all sources and persist. Returns per-outcome counts."""
        sources = build_sources(self.config)
        tally = {"inserted": 0, "updated": 0, "skipped_sticky": 0, "blacklisted": 0}
        avoid_kws = [k.lower() for k in (self.profile.avoid_keywords or [])]
        for src in sources:
            try:
                jobs = src.fetch()
            except Exception as e:
                log.error("source %s crashed: %s", getattr(src, "name", src), e)
                continue
            for job in jobs:
                title_lower = job.title.lower()
                if any(k in title_lower for k in avoid_kws):
                    job.status = Status.SKIPPED
                    job.notes = "Skipped during discovery: title matches avoid keyword."
                outcome = self.db.upsert_job(job)
                tally[outcome] = tally.get(outcome, 0) + 1
        log.info("discover: %s", tally)
        return tally

    def score_new(self) -> int:
        """Score everything still in NEW status. Returns how many were scored."""
        pending = self.db.get_jobs(status=Status.NEW)
        for job in pending:
            score, rationale = self.scorer.score(job, self.profile)
            if score < 60.0:
                self.db.update_status(job.job_id, Status.SKIPPED, score=score, score_rationale=rationale)
            else:
                self.db.save_score(job.job_id, score, rationale)
        log.info("scored %d new jobs", len(pending))
        return len(pending)

    def run(self) -> dict:
        """One full pass: discover, score, then auto-submit high matches."""
        discovered = self.discover()
        scored = self.score_new()
        submitted = self.auto_submit()
        return {
            "discovered": discovered,
            "scored": scored,
            "submitted": submitted,
            "status_counts": self.db.counts_by_status(),
        }

    def auto_submit(self) -> dict:
        """Submit scored jobs in a fresh subprocess to avoid Playwright/async conflicts."""
        cfg = self.config
        if not cfg.get("auto_submit_enabled", True):
            return {"skipped": True, "reason": "auto_submit_enabled is false"}

        import os
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env["JOBAGENT_SKIP_FORM_PARSE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-m", "jobagent", "auto-submit"],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=int(cfg.get("auto_submit_timeout_seconds", 7200)),
        )
        if proc.returncode != 0:
            log.error("auto-submit subprocess failed: %s", proc.stderr[-2000:])
            return {"error": proc.stderr[-500:], "stdout": proc.stdout[-500:]}
        try:
            # auto-submit prints JSON as last output block
            import json
            text = proc.stdout.strip()
            start = text.rfind("{")
            return json.loads(text[start:]) if start >= 0 else {"stdout": text}
        except Exception:
            return {"stdout": proc.stdout[-1000:]}

    def discover_linkedin(self) -> dict[str, int]:
        """Fetch LinkedIn jobs only (EMEA + keyword searches)."""
        from .sources.feeds import LinkedInSource

        cfg = self.config
        src = LinkedInSource(
            keywords=cfg.get("linkedin_keywords"),
            locations=cfg.get("linkedin_locations"),
            max_results=int(cfg.get("linkedin_max_results", 80)),
            pages_per_query=int(cfg.get("linkedin_pages_per_query", 4)),
        )
        tally = {"inserted": 0, "updated": 0, "skipped_sticky": 0, "blacklisted": 0}
        avoid_kws = [k.lower() for k in (self.profile.avoid_keywords or [])]
        try:
            jobs = src.fetch()
        except Exception as e:
            log.error("linkedin discovery crashed: %s", e)
            return tally
        for job in jobs:
            title_lower = job.title.lower()
            if any(k in title_lower for k in avoid_kws):
                job.status = Status.SKIPPED
                job.notes = "Skipped during discovery: title matches avoid keyword."
            outcome = self.db.upsert_job(job)
            tally[outcome] = tally.get(outcome, 0) + 1
        log.info("linkedin discover: %s", tally)
        return tally

    _EMEA_LOCATIONS = (
        "united kingdom", "uk", "london", "england", "scotland", "ireland", "dublin",
        "germany", "berlin", "munich", "france", "paris", "netherlands", "amsterdam",
        "spain", "madrid", "barcelona", "italy", "milan", "switzerland", "zurich",
        "sweden", "stockholm", "poland", "warsaw", "belgium", "brussels", "austria",
        "vienna", "portugal", "lisbon", "lisboa", "denmark", "copenhagen", "norway",
        "oslo", "finland", "helsinki", "emea", "europe", "european union", "eu ",
    )

    @classmethod
    def _is_remote_job(cls, job) -> bool:
        if job.remote:
            return True
        loc = (job.location or "").lower()
        title = (job.title or "").lower()
        desc = (job.description or "")[:2500].lower()
        if "remote" in loc or "remote" in title:
            return True
        if any(p in desc for p in ("fully remote", "100% remote", "work from anywhere", "remote-first")):
            return True
        return False

    @classmethod
    def _is_emea_job(cls, job) -> bool:
        loc = (job.location or "").lower()
        return any(m in loc for m in cls._EMEA_LOCATIONS)

    def linkedin_auto_submit(
        self,
        max_apply: int = 15,
        min_score: float = 75.0,
        *,
        easy_apply_only: bool = False,
        external_only: bool = False,
    ) -> dict:
        """Apply to LinkedIn-sourced roles (Easy Apply + off-site ATS)."""
        from .generator import get_generator
        from .linkedin_apply import is_easy_apply_job, sort_linkedin_jobs
        from .models import ATS
        from .submitter import Submitter

        apply_kw = self.config.get("linkedin_apply_keywords") or self.config.get(
            "linkedin_design_keywords",
            ["product design", "product designer", "ui", "ux", "designer"],
        )
        emea_or_remote = self.config.get("linkedin_apply_emea_or_remote", True)
        easy_min = float(self.config.get("linkedin_easy_apply_min_score", 60))
        easy_enabled = self.config.get("linkedin_easy_apply_enabled", True)

        exclude = [ATS.WORKDAY.value]
        if external_only:
            exclude.append(ATS.LINKEDIN.value)

        candidates = self.db.get_jobs(
            status=Status.SCORED,
            source="linkedin",
            min_score=0 if easy_apply_only else min_score,
            title_keywords=apply_kw,
            exclude_ats=exclude,
            order_by_score=True,
        )

        filtered = []
        for j in candidates:
            if emea_or_remote and not (self._is_remote_job(j) or self._is_emea_job(j)):
                continue
            if easy_apply_only:
                if easy_enabled and is_easy_apply_job(j) and (j.score or 0) >= easy_min:
                    filtered.append(j)
            elif external_only:
                if not is_easy_apply_job(j):
                    filtered.append(j)
            else:
                if is_easy_apply_job(j):
                    if easy_enabled and (j.score or 0) >= easy_min:
                        filtered.append(j)
                elif (j.score or 0) >= min_score:
                    filtered.append(j)

        # Prioritize Easy Apply, then Greenhouse/Ashby/HiBoB direct, then score
        if not easy_apply_only:
            filtered = [j for j in filtered if not is_easy_apply_job(j) or easy_enabled]
        jobs = sort_linkedin_jobs(filtered)[:max_apply]

        log.info(
            "linkedin auto-submit (%s%s): %d candidates (pool %d)",
            "easy-only " if easy_apply_only else "",
            "external-only " if external_only else "",
            len(jobs), len(candidates),
        )

        gen = get_generator()
        headless = self.config.get("auto_submit_headless", True)
        results = {"attempted": 0, "submitted": 0, "failed": 0, "easy_apply": 0, "details": []}

        with Submitter(self.profile, headless=headless) as sub:
            for job in jobs:
                results["attempted"] += 1
                tier = "easy" if is_easy_apply_job(job) else "external"
                log.info("linkedin submit [%s]: %s @ %s (%.0f%%)", tier, job.title, job.company, job.score or 0)
                try:
                    docs = gen.generate(job, self.profile)
                    result = sub.submit_application(job, docs)
                except Exception as e:
                    results["failed"] += 1
                    self.db.update_status(job.job_id, Status.ERROR, notes=str(e))
                    results["details"].append({"job_id": job.job_id, "ok": False, "error": str(e)})
                    continue
                if result.ok:
                    results["submitted"] += 1
                    if is_easy_apply_job(job):
                        results["easy_apply"] += 1
                    self.db.update_status(job.job_id, Status.APPLIED, notes=result.message)
                else:
                    results["failed"] += 1
                    self.db.update_status(job.job_id, Status.ERROR, notes=result.message)
                results["details"].append({
                    "job_id": job.job_id, "ok": result.ok,
                    "status": result.status, "message": result.message,
                })
        return results

    def daily_apply(self) -> dict:
        """Apply until daily_apply_goal is reached (retries LinkedIn errors first)."""
        goal = int(self.config.get("daily_apply_goal", 50))
        applied = self.db.count_applied()
        remaining = max(0, goal - applied)
        if remaining == 0:
            log.info("daily apply: goal %d already reached (%d applied)", goal, applied)
            return {"goal": goal, "applied_before": applied, "remaining": 0, "skipped": True}

        reset = self.db.reset_linkedin_errors()
        with self.db._conn() as c:
            c.execute(
                "UPDATE jobs SET status='scored', notes=NULL WHERE status='error' AND source != 'linkedin'"
            )
            c.execute(
                """UPDATE jobs SET ats='linkedin' WHERE source='linkedin'
                   AND ats='unknown' AND (
                     lower(description) LIKE '%easy apply%'
                     OR lower(description) LIKE '%easy-apply%'
                   )"""
            )

        if self.config.get("linkedin_discover_on_daily", True):
            self.discover_linkedin()
            self.score_new()

        log.info("daily apply: reset %d failed LinkedIn jobs; need %d more to hit %d", reset, remaining, goal)

        min_score = float(self.config.get("linkedin_apply_min_score", 75))
        easy_cap = min(remaining, int(self.config.get("linkedin_easy_apply_priority_count", 15)))

        easy = self.linkedin_auto_submit(
            max_apply=easy_cap, easy_apply_only=True,
        )
        submitted = easy.get("submitted", 0)
        remaining = max(0, remaining - submitted)

        linkedin = {"easy_apply_batch": easy, "external_batch": {}}
        if remaining > 0:
            ext = self.linkedin_auto_submit(
                max_apply=remaining, min_score=min_score, external_only=True,
            )
            linkedin["external_batch"] = ext
            submitted += ext.get("submitted", 0)
            remaining = max(0, remaining - ext.get("submitted", 0))

        greenhouse = {}
        if remaining > 0 and self.config.get("auto_submit_enabled", True):
            import os
            import subprocess
            import sys
            from pathlib import Path

            root = Path(__file__).resolve().parent.parent
            env = os.environ.copy()
            env["JOBAGENT_SKIP_FORM_PARSE"] = "1"
            env["JOBAGENT_MAX_APPLY"] = str(remaining)
            proc = subprocess.run(
                [sys.executable, "-m", "jobagent", "auto-submit"],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                timeout=int(self.config.get("auto_submit_timeout_seconds", 7200)),
            )
            if proc.returncode == 0:
                try:
                    import json
                    text = proc.stdout.strip()
                    start = text.rfind("{")
                    greenhouse = json.loads(text[start:]) if start >= 0 else {}
                except Exception:
                    greenhouse = {"stdout": proc.stdout[-500:]}
            else:
                greenhouse = {"error": proc.stderr[-500:]}
            submitted += greenhouse.get("submitted", 0)

        applied_after = self.db.count_applied()
        return {
            "goal": goal,
            "applied_before": applied,
            "applied_after": applied_after,
            "linkedin": linkedin,
            "greenhouse": greenhouse,
            "reset_errors": reset,
        }

    def linkedin_run(self, max_apply: int = 15) -> dict:
        """Discover LinkedIn (50+), score, apply remote product design roles."""
        discovered = self.discover_linkedin()
        scored = self.score_new()
        submitted = self.linkedin_auto_submit(max_apply=max_apply)
        return {
            "discovered": discovered,
            "scored": scored,
            "submitted": submitted,
            "linkedin_total": self.db.get_jobs(source="linkedin"),
            "status_counts": self.db.counts_by_status(),
        }
