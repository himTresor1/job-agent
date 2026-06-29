"""Source registry. Add or remove sources by editing build_sources()."""

from __future__ import annotations

from .ats_boards import GreenhouseSource, LeverSource
from .feeds import (
    HNWhoIsHiringSource,
    LinkedInSource,
    RemoteOKSource,
    WeWorkRemotelySource,
    WellfoundSource,
)

__all__ = [
    "GreenhouseSource",
    "LeverSource",
    "RemoteOKSource",
    "WeWorkRemotelySource",
    "HNWhoIsHiringSource",
    "WellfoundSource",
    "LinkedInSource",
    "build_sources",
]


def build_sources(config: dict) -> list:
    """Construct the active source list from a config dict.

    config example:
        {
          "greenhouse_boards": ["stripe", "airbnb"],
          "lever_boards": ["netflix"],
          "remoteok_keywords": ["designer", "product"],
        }
    """
    sources = []
    if config.get("greenhouse_boards"):
        sources.append(GreenhouseSource(config["greenhouse_boards"]))
    if config.get("lever_boards"):
        sources.append(LeverSource(config["lever_boards"]))
    if config.get("enable_remoteok", True):
        sources.append(RemoteOKSource(config.get("remoteok_keywords")))
    if config.get("enable_wwr"):
        kws = list(set((config.get("remoteok_keywords") or []) + (config.get("linkedin_keywords") or [])))
        sources.append(WeWorkRemotelySource(keywords=kws))
    if config.get("enable_hn"):
        sources.append(HNWhoIsHiringSource())
    if config.get("enable_linkedin", True):
        sources.append(LinkedInSource(
            keywords=config.get("linkedin_keywords"),
            locations=config.get("linkedin_locations"),
            max_results=int(config.get("linkedin_max_results", 60)),
            pages_per_query=int(config.get("linkedin_pages_per_query", 4)),
        ))
    return sources
