"""Core data model.

Every source — Greenhouse, Lever, RemoteOK, WWR, HN Who-is-hiring — produces
messy, differently-shaped data. They all get normalized into this one Job shape
before anything else touches them. This is the single contract the rest of the
pipeline depends on.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Status(str, Enum):
    """Lifecycle of a job as it moves through the pipeline.

    The status field is what makes the tool feel effortless: it's how we avoid
    showing you the same job twice and how the approval gate (later) knows what
    is waiting on you.
    """

    NEW = "new"            # just discovered, not yet scored
    SCORED = "scored"      # LLM has rated it against your profile
    GENERATED = "generated"  # tailored resume + cover letter ready
    APPROVED = "approved"  # you (or the gate) cleared it for submission
    APPLIED = "applied"    # application submitted
    SKIPPED = "skipped"    # you passed on it
    BLACKLISTED = "blacklisted"  # never show again (company or role)
    ERROR = "error"        # something failed; needs a look


class ATS(str, Enum):
    """Which applicant-tracking system hosts the application.

    This drives the submission strategy later: Greenhouse/Lever are the most
    automatable, Workday is painful, LinkedIn is risky, UNKNOWN means apply
    manually.
    """

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    PERSONIO = "personio"
    SMARTRECRUITERS = "smartrecruiters"
    WORKDAY = "workday"
    LINKEDIN = "linkedin"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass
class Job:
    """One normalized job posting.

    `job_id` is a deterministic hash of company+title+source so the same posting
    seen on two days, or via two sources, collapses to one row instead of
    spamming you.
    """

    # --- identity / source ---
    source: str                      # e.g. "greenhouse", "remoteok"
    source_url: str                  # canonical link to the posting
    company: str
    title: str

    # --- details (best-effort; sources vary in what they provide) ---
    location: str = ""
    remote: Optional[bool] = None
    description: str = ""
    department: str = ""
    salary_text: str = ""            # raw, unparsed — salary formats are chaos
    ats: ATS = ATS.UNKNOWN
    apply_url: str = ""              # where the application form lives

    # --- pipeline state ---
    status: Status = Status.NEW
    score: Optional[float] = None    # 0-100 match score from the scorer
    score_rationale: str = ""        # one-line "why" from the LLM
    resume_path: str = ""            # generated, tailored resume
    cover_letter_path: str = ""      # generated cover letter

    # --- bookkeeping ---
    first_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes: str = ""

    job_id: str = ""

    def __post_init__(self):
        # Coerce string enums coming back from the DB into real enum members.
        if isinstance(self.status, str):
            self.status = Status(self.status)
        if isinstance(self.ats, str):
            self.ats = ATS(self.ats)
        if not self.job_id:
            self.job_id = self._compute_id()
        if not self.apply_url:
            self.apply_url = self.source_url

    def _compute_id(self) -> str:
        basis = f"{self.company.strip().lower()}|{self.title.strip().lower()}|{self.source}"
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    def touch(self):
        self.last_updated = datetime.now(timezone.utc).isoformat()

    def to_row(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["ats"] = self.ats.value
        return d

    @classmethod
    def from_row(cls, row: dict) -> "Job":
        return cls(**row)
