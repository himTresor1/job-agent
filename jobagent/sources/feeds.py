"""Remote-job feeds and the messier sources.

RemoteOK has a clean public JSON endpoint — implemented fully here.
We Work Remotely publishes RSS — sketched (needs an RSS parser).
HN "Who is hiring" is a monthly thread via the Algolia/Firebase API — sketched.
Wellfound and LinkedIn are scrape-only / ToS-restricted — deliberately left as
explicit stubs that explain why, rather than half-working scrapers that break
silently and risk your accounts.
"""

from __future__ import annotations

import logging

from ..models import ATS, Job
from .base import Source

log = logging.getLogger(__name__)


class RemoteOKSource(Source):
    """RemoteOK exposes a public JSON feed. First element is metadata/legal —
    skip it. This is clean and allowed."""

    name = "remoteok"
    API = "https://remoteok.com/api"

    def __init__(self, keywords: list[str] | None = None):
        super().__init__()
        self.keywords = [k.lower() for k in (keywords or [])]

    def fetch(self) -> list[Job]:
        try:
            data = self._get(self.API).json()
        except Exception as e:
            log.warning("remoteok: fetch failed: %s", e)
            return []

        jobs: list[Job] = []
        for j in data:
            if not isinstance(j, dict) or "position" not in j:
                continue  # skips the leading legal/metadata object
            title = (j.get("position") or "").strip()
            if self.keywords and not any(k in title.lower() for k in self.keywords):
                continue
            try:
                jobs.append(
                    Job(
                        source=self.name,
                        source_url=j.get("url", ""),
                        apply_url=j.get("apply_url") or j.get("url", ""),
                        company=(j.get("company") or "").strip(),
                        title=title,
                        location=j.get("location") or "Remote",
                        remote=True,
                        description=(j.get("description") or "").strip(),
                        salary_text=self._salary(j),
                        ats=ATS.OTHER,
                    )
                )
            except Exception as e:
                log.warning("remoteok: skip bad record: %s", e)
        log.info("remoteok: %d jobs", len(jobs))
        return jobs

    @staticmethod
    def _salary(j: dict) -> str:
        lo, hi = j.get("salary_min"), j.get("salary_max")
        if lo and hi:
            return f"${lo:,}–${hi:,}"
        return ""


class WeWorkRemotelySource(Source):
    """We Work Remotely publishes per-category RSS feeds.
    We parse the XML using standard library xml.etree.ElementTree, avoiding feedparser dependency.
    """

    name = "weworkremotely"
    FEEDS = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-design-jobs.rss"
    ]

    def __init__(self, keywords: list[str] | None = None):
        super().__init__()
        self.keywords = [k.lower() for k in (keywords or [])]

    def fetch(self) -> list[Job]:
        import xml.etree.ElementTree as ET
        from ..models import ATS

        jobs: list[Job] = []
        for url in self.FEEDS:
            try:
                log.info("weworkremotely: fetching RSS from %s", url)
                res = self._get(url)
                if res.status_code != 200:
                    log.warning("weworkremotely: failed to fetch %s: %s", url, res.status_code)
                    continue
                
                # Parse XML tree
                root = ET.fromstring(res.content)
                items = root.findall('.//item')
                for item in items:
                    title_el = item.find('title')
                    link_el = item.find('link')
                    desc_el = item.find('description')
                    
                    if title_el is None or link_el is None:
                        continue
                        
                    raw_title = title_el.text or ""
                    apply_url = link_el.text or ""
                    description = desc_el.text or "" if desc_el is not None else ""
                    
                    # Title format is typically: "Company Name: Job Title"
                    if ":" in raw_title:
                        company, title = raw_title.split(":", 1)
                        company = company.strip()
                        title = title.strip()
                    else:
                        company = "Unknown"
                        title = raw_title.strip()
                        
                    # Filter by title keywords
                    if self.keywords and not any(k in title.lower() or k in company.lower() for k in self.keywords):
                        continue
                        
                    # WeWorkRemotely postings resolve to ATS dynamically
                    jobs.append(Job(
                        source=self.name,
                        source_url=apply_url,
                        apply_url=apply_url,
                        company=company,
                        title=title,
                        location="Remote",
                        remote=True,
                        description=description,
                        ats=ATS.UNKNOWN,
                    ))
            except Exception as e:
                log.warning("weworkremotely: failed to parse feed %s: %s", url, e)
                
        log.info("weworkremotely: returned %d jobs", len(jobs))
        return jobs


class HNWhoIsHiringSource(Source):
    """Hacker News monthly "Ask HN: Who is hiring?" thread. High signal for
    startups. Pull the thread's comment tree via the HN Firebase API or Algolia
    search, then parse each top-level comment as one posting.

    Stub: comment parsing is fuzzy (free-text), so this is a later refinement."""

    name = "hn_whoishiring"

    def fetch(self) -> list[Job]:
        log.info("hn_whoishiring: stub — wire up HN API + comment parsing to enable")
        return []


class WellfoundSource(Source):
    """Wellfound (AngelList Talent) has no clean public API; discovery is
    scrape-only and the layout changes often. Treat as nice-to-have, handled via
    the browser layer if at all — not a stable foundation source."""

    name = "wellfound"

    def fetch(self) -> list[Job]:
        log.info("wellfound: intentionally not auto-scraped — see docstring")
        return []


class LinkedInSource(Source):
    """LinkedIn guest search scraper.

    Searches by keyword + EMEA location with pagination. Off-site apply URLs are
    resolved at submission time (not during discovery) to keep discovery fast
    and avoid Playwright/async conflicts.
    """

    name = "linkedin"
    SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

    def __init__(
        self,
        keywords: list[str] | None = None,
        locations: list[str] | None = None,
        max_results: int = 60,
        pages_per_query: int = 4,
    ):
        super().__init__()
        self.keywords = keywords or ["Product Designer"]
        self.locations = locations or ["Remote", "EMEA"]
        self.max_results = max_results
        self.pages_per_query = pages_per_query

    def _get_with_retry(self, url: str, max_retries: int = 4):
        import time
        import random
        import requests

        for attempt in range(max_retries):
            try:
                return self._get(url)
            except requests.exceptions.HTTPError as he:
                if he.response is not None and he.response.status_code == 429:
                    backoff = 20.0 * (attempt + 1) + random.random() * 10.0
                    log.warning(
                        "linkedin: 429 on %s — backing off %.0fs (retry %d/%d)",
                        url[:80], backoff, attempt + 1, max_retries,
                    )
                    time.sleep(backoff)
                    continue
                raise
        return self._get(url)

    def fetch(self) -> list[Job]:
        import re
        import time
        import random
        import urllib.parse
        from bs4 import BeautifulSoup
        from ..database import Database

        db = Database()
        seen_urls: set[str] = set()
        cards: list[dict] = []

        for kw in self.keywords:
            for loc in self.locations:
                if len(cards) >= self.max_results:
                    break
                for page_idx in range(self.pages_per_query):
                    if len(cards) >= self.max_results:
                        break
                    start = page_idx * 25
                    url = (
                        f"{self.SEARCH}?keywords={urllib.parse.quote(kw)}"
                        f"&location={urllib.parse.quote(loc)}&start={start}"
                    )
                    time.sleep(2.5 + random.random() * 2.5)
                    try:
                        res = self._get_with_retry(url)
                    except Exception as e:
                        log.warning("linkedin: search failed (%s, %s, start=%d): %s", kw, loc, start, e)
                        break
                    if res.status_code != 200:
                        break
                    page_cards = self._parse_search_cards(res.text)
                    if not page_cards:
                        break
                    for card in page_cards:
                        if card["source_url"] in seen_urls:
                            continue
                        if db.get_job_by_url(card["source_url"]):
                            continue
                        seen_urls.add(card["source_url"])
                        cards.append(card)
                        if len(cards) >= self.max_results:
                            break

        if not cards:
            log.info("linkedin: no new job cards found")
            return []

        log.info("linkedin: fetching details for %d new cards", len(cards))
        jobs: list[Job] = []
        consecutive_429 = 0

        for card in cards:
            if consecutive_429 >= 3:
                log.warning("linkedin: halting details fetch after repeated 429s")
                break
            time.sleep(3.0 + random.random() * 3.0)
            detail_url = self.DETAIL.format(job_id=card["linkedin_id"])
            try:
                res = self._get_with_retry(detail_url)
            except Exception as e:
                if "429" in str(e):
                    consecutive_429 += 1
                log.warning("linkedin: detail fetch failed %s: %s", card["linkedin_id"], e)
                continue
            consecutive_429 = 0
            try:
                jobs.append(self._parse_detail(card, res.text))
            except Exception as e:
                log.warning("linkedin: parse failed %s: %s", card["linkedin_id"], e)

        log.info("linkedin: returning %d jobs", len(jobs))
        return jobs

    @staticmethod
    def _parse_search_cards(html: str) -> list[dict]:
        import re
        from urllib.parse import urlparse
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", class_="job-search-card") or soup.find_all("li")
        out: list[dict] = []
        for card in cards:
            link_el = card.find("a", class_="base-card__full-link")
            if not link_el or not link_el.get("href"):
                continue
            link = link_el["href"]
            linkedin_id = None
            try:
                path = urlparse(link).path.strip("/").split("/")
                if path:
                    seg = path[-1].split("-")
                    if seg and seg[-1].isdigit():
                        linkedin_id = seg[-1]
            except Exception:
                pass
            if not linkedin_id:
                m = re.search(r"-(\d+)(?:\?|$)", link)
                linkedin_id = m.group(1) if m else None
            if not linkedin_id:
                continue
            title_el = card.find("h3", class_="base-search-card__title")
            co_el = card.find("h4", class_="base-search-card__subtitle") or card.find(
                "a", class_="hidden-nested-link"
            )
            loc_el = card.find("span", class_="job-search-card__location")
            badge_el = card.find("span", class_="job-search-card__list-badge") or card.find(
                "li", class_="job-card-container__footer-item"
            )
            badge_text = badge_el.get_text(strip=True).lower() if badge_el else ""
            easy_apply = "easy apply" in badge_text
            out.append({
                "linkedin_id": linkedin_id,
                "title": title_el.text.strip() if title_el else "N/A",
                "company": co_el.text.strip() if co_el else "N/A",
                "location": loc_el.text.strip() if loc_el else "",
                "source_url": f"https://www.linkedin.com/jobs/view/{linkedin_id}",
                "easy_apply": easy_apply,
            })
        return out

    @staticmethod
    def _parse_detail(card: dict, html: str) -> Job:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        desc_div = soup.find("div", class_="description__text") or soup.find(
            "section", class_="description"
        )
        description = desc_div.decode_contents().strip() if desc_div else ""
        easy_apply = card.get("easy_apply", False) or "easy apply" in html.lower() or "easy-apply" in html.lower()
        loc = card["location"] or ""
        loc_lower = loc.lower()
        is_remote = (
            "remote" in loc_lower
            or "remote" in description[:2000].lower()
            or "work from home" in description[:2000].lower()
        )
        return Job(
            source="linkedin",
            source_url=card["source_url"],
            apply_url=card["source_url"],
            company=card["company"],
            title=card["title"],
            location=loc or ("Remote" if is_remote else ""),
            remote=is_remote or None,
            description=description,
            ats=ATS.LINKEDIN if easy_apply else ATS.UNKNOWN,
        )
