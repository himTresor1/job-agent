"""Base class for job sources.

Every source subclasses Source and implements fetch(), returning a list of
normalized Job objects. The pipeline doesn't care where a job came from once
it's a Job — that's the whole point of normalizing here.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import requests

from ..models import Job

log = logging.getLogger(__name__)

# Be a polite citizen: identify the client and don't hammer endpoints.
USER_AGENT = "personal-job-agent/0.1 (single-user; polling public boards)"
TIMEOUT = 20


class Source(ABC):
    name: str = "base"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    @abstractmethod
    def fetch(self) -> list[Job]:
        """Return normalized jobs from this source. Must not raise on a single
        bad record — log and skip instead, so one malformed posting doesn't
        sink the whole run."""
        ...

    def _get(self, url: str, **kw):
        r = self.session.get(url, timeout=TIMEOUT, **kw)
        r.raise_for_status()
        return r
