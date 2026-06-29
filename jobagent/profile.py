"""Your master profile.

This is the single record of who you are that both the scorer and the document
generator read from. The quality of everything downstream depends on this being
rich and accurate.

Hard rule enforced by convention throughout the pipeline: the generator may
REORDER and RE-EMPHASIZE facts from here, but must NEVER invent experience.
Everything a generated resume claims must trace back to this file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Profile:
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    headline: str = ""               # e.g. "Senior Product Designer"
    summary: str = ""                # 2-3 sentence pitch
    target_titles: list[str] = field(default_factory=list)
    target_keywords: list[str] = field(default_factory=list)  # skills to match on
    avoid_keywords: list[str] = field(default_factory=list)   # auto-deprioritize
    skills: list[str] = field(default_factory=list)
    experience: list[dict] = field(default_factory=list)      # [{company,title,dates,bullets:[...]}]
    education: list[dict] = field(default_factory=list)
    links: dict = field(default_factory=dict)                 # {portfolio, github, linkedin}
    preferences: dict = field(default_factory=dict)           # {remote_only, min_salary, ...}

    @classmethod
    def load(cls, path: Path | str) -> "Profile":
        path = Path(path)
        data = json.loads(path.read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | str):
        Path(path).write_text(json.dumps(self.__dict__, indent=2))

    def to_scoring_blurb(self) -> str:
        """Compact text the scorer feeds to the model alongside each job."""
        parts = [
            f"Name: {self.name}",
            f"Targeting: {', '.join(self.target_titles) or '(unspecified)'}",
            f"Key skills: {', '.join(self.skills) or '(unspecified)'}",
            f"Summary: {self.summary}",
        ]
        if self.preferences:
            parts.append(f"Preferences: {json.dumps(self.preferences)}")
        return "\n".join(parts)
