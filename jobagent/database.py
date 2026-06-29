"""SQLite persistence layer.

A personal tool does not need Postgres. SQLite is one file, zero setup, and
handles everything here comfortably. This module is the single source of truth
the whole pipeline reads from and writes to.

Key behaviors:
- upsert_job: dedupe on job_id. If we've seen it, we don't clobber your
  decisions (a SKIPPED job stays skipped, a BLACKLISTED company stays hidden).
- get_jobs: status- and score-filtered queries that drive the digest and gate.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import Job, Status

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jobs.db"

# Statuses that represent a decision you've already made. We never silently
# overwrite these when a job is re-discovered on a later run.
_STICKY = {Status.SKIPPED, Status.BLACKLISTED, Status.APPLIED, Status.APPROVED}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT,
    remote          INTEGER,
    description     TEXT,
    department      TEXT,
    salary_text     TEXT,
    ats             TEXT,
    apply_url       TEXT,
    status          TEXT NOT NULL,
    score           REAL,
    score_rationale TEXT,
    resume_path     TEXT,
    cover_letter_path TEXT,
    first_seen      TEXT,
    last_updated    TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_score  ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_company ON jobs(company);

-- Company-level blacklist, independent of individual postings.
CREATE TABLE IF NOT EXISTS company_blacklist (
    company TEXT PRIMARY KEY
);
"""


def _adapt(job: Job) -> dict:
    row = job.to_row()
    row["remote"] = None if row["remote"] is None else int(row["remote"])
    return row


def _restore(raw: sqlite3.Row) -> Job:
    d = dict(raw)
    if d.get("remote") is not None:
        d["remote"] = bool(d["remote"])
    return Job.from_row(d)


class Database:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- writes ---

    def upsert_job(self, job: Job) -> str:
        """Insert a new job or refresh a known one without losing decisions.

        Returns one of: "inserted", "updated", "skipped_sticky", "blacklisted".
        """
        with self._conn() as c:
            # Company blacklisted? Never resurface.
            row = c.execute(
                "SELECT 1 FROM company_blacklist WHERE company = ? COLLATE NOCASE",
                (job.company,),
            ).fetchone()
            if row:
                return "blacklisted"

            existing = c.execute(
                "SELECT status FROM jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()

            if existing is None:
                job.touch()
                cols = _adapt(job)
                placeholders = ", ".join(":" + k for k in cols)
                c.execute(
                    f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({placeholders})",
                    cols,
                )
                return "inserted"

            # Known job. If you've already decided on it, leave it alone.
            if Status(existing["status"]) in _STICKY:
                return "skipped_sticky"

            # Otherwise refresh the volatile fields (description can change, etc.)
            job.touch()
            c.execute(
                """UPDATE jobs SET
                       description = :description,
                       salary_text = :salary_text,
                       location = :location,
                       apply_url = :apply_url,
                       last_updated = :last_updated
                   WHERE job_id = :job_id""",
                _adapt(job),
            )
            return "updated"

    def update_status(self, job_id: str, status: Status, **fields):
        sets = ["status = :status", "last_updated = datetime('now')"]
        params = {"job_id": job_id, "status": status.value}
        for k, v in fields.items():
            sets.append(f"{k} = :{k}")
            params[k] = v
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = :job_id", params)

    def save_score(self, job_id: str, score: float, rationale: str):
        self.update_status(job_id, Status.SCORED, score=score, score_rationale=rationale)

    def blacklist_company(self, company: str):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO company_blacklist (company) VALUES (?)",
                (company,),
            )
            c.execute(
                "UPDATE jobs SET status = ? WHERE company = ? COLLATE NOCASE",
                (Status.BLACKLISTED.value, company),
            )

    # --- reads ---

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _restore(r) if r else None

    def get_job_by_url(self, source_url: str) -> Optional[Job]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE source_url = ?", (source_url,)).fetchone()
            return _restore(r) if r else None

    def get_jobs(
        self,
        status: Optional[Status] = None,
        min_score: Optional[float] = None,
        limit: Optional[int] = None,
        order_by_score: bool = False,
        remote_only: bool = False,
        source: Optional[str] = None,
        title_keywords: Optional[list[str]] = None,
        exclude_ats: Optional[list[str]] = None,
    ) -> list[Job]:
        q = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if status is not None:
            q += " AND status = ?"
            params.append(status.value)
        if min_score is not None:
            q += " AND score >= ?"
            params.append(min_score)
        if source is not None:
            q += " AND source = ?"
            params.append(source)
        if title_keywords:
            clauses = " OR ".join("lower(title) LIKE ?" for _ in title_keywords)
            q += f" AND ({clauses})"
            params.extend(f"%{k.lower()}%" for k in title_keywords)
        if exclude_ats:
            placeholders = ", ".join("?" for _ in exclude_ats)
            q += f" AND ats NOT IN ({placeholders})"
            params.extend(exclude_ats)
        if remote_only:
            q += (
                " AND (remote = 1 OR lower(location) LIKE '%remote%'"
                " OR lower(title) LIKE '%remote%'"
                " OR source IN ('remoteok', 'weworkremotely')"
                " OR (ats = 'greenhouse' AND lower(description) LIKE '%remote%')"
                " OR (ats IN ('greenhouse', 'lever') AND lower(company) IN"
                " ('discord', 'gitlab', 'notion', 'stripe', 'figma', 'plaid')))"
            )
        if order_by_score:
            q += " ORDER BY score DESC"
        if limit is not None:
            q += " LIMIT ?"
            params.append(limit)
        with self._conn() as c:
            return [_restore(r) for r in c.execute(q, params).fetchall()]

    def counts_by_status(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) n FROM jobs GROUP BY status"
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}

    def count_applied(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) n FROM jobs WHERE status = 'applied'").fetchone()
            return int(row["n"]) if row else 0

    def reset_linkedin_errors(self) -> int:
        """Re-queue failed LinkedIn jobs and restore LinkedIn apply URLs."""
        with self._conn() as c:
            cur = c.execute(
                """UPDATE jobs SET status = 'scored', notes = NULL,
                   apply_url = source_url
                   WHERE source = 'linkedin' AND status = 'error'
                   AND source_url LIKE '%linkedin.com%'"""
            )
            return cur.rowcount
