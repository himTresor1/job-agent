"""Greenhouse and Lever connectors.

These are the highest-value sources for startups: both expose a clean, public,
per-company JSON API. There's no global "all jobs" endpoint, so you maintain a
watchlist of company board tokens (the slug in their careers URL) and we poll
each one.

  Greenhouse: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  Lever:      https://api.lever.co/v0/postings/{token}?mode=json

The board token is the company slug. For a Greenhouse board at
boards.greenhouse.io/acme the token is "acme". For Lever at
jobs.lever.co/acme it's "acme".
"""

from __future__ import annotations

import logging
import re

from ..models import ATS, Job
from .base import Source

log = logging.getLogger(__name__)


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class GreenhouseSource(Source):
    name = "greenhouse"
    API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def __init__(self, board_tokens: list[str]):
        super().__init__()
        self.board_tokens = board_tokens

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for token in self.board_tokens:
            try:
                data = self._get(self.API.format(token=token)).json()
            except Exception as e:
                log.warning("greenhouse: failed board %s: %s", token, e)
                continue
            for j in data.get("jobs", []):
                try:
                    jobs.append(self._parse(j, token))
                except Exception as e:
                    log.warning("greenhouse: skip bad record on %s: %s", token, e)
        log.info("greenhouse: %d jobs from %d boards", len(jobs), len(self.board_tokens))
        return jobs

    def _parse(self, j: dict, token: str) -> Job:
        loc = (j.get("location") or {}).get("name", "")
        return Job(
            source=self.name,
            source_url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url", ""),
            company=token,  # board token; can be prettified via a watchlist map
            title=j.get("title", "").strip(),
            location=loc,
            remote=("remote" in loc.lower()) or None,
            description=j.get("content", ""),
            ats=ATS.GREENHOUSE,
        )


class LeverSource(Source):
    name = "lever"
    API = "https://api.lever.co/v0/postings/{token}?mode=json"

    def __init__(self, board_tokens: list[str]):
        super().__init__()
        self.board_tokens = board_tokens

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for token in self.board_tokens:
            try:
                data = self._get(self.API.format(token=token)).json()
            except Exception as e:
                log.warning("lever: failed board %s: %s", token, e)
                continue
            for j in data:
                try:
                    jobs.append(self._parse(j, token))
                except Exception as e:
                    log.warning("lever: skip bad record on %s: %s", token, e)
        log.info("lever: %d jobs from %d boards", len(jobs), len(self.board_tokens))
        return jobs

    def _parse(self, j: dict, token: str) -> Job:
        cats = j.get("categories", {}) or {}
        loc = cats.get("location", "")
        return Job(
            source=self.name,
            source_url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
            company=token,
            title=j.get("text", "").strip(),
            location=loc,
            department=cats.get("team", "") or cats.get("department", ""),
            remote=("remote" in (loc or "").lower()) or None,
            description=j.get("description", ""),
            ats=ATS.LEVER,
        )
