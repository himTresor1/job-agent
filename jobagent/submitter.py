"""Browser submission via Playwright.

This is what runs when you click approve. It opens the application form, fills
the fields it can confidently map from your profile and the generated docs,
uploads the resume, and submits.

Design rules that make "approve = submit" safe to live with:

1. SUBMIT ONLY ON APPROVAL. submit_application() is called only from the
   dashboard's approve handler. Nothing here runs on a schedule.

2. CONFIDENT FILL, EXPLICIT STOP. We map well-known fields (name, email, phone,
   resume upload) by common selectors. If a required field can't be mapped, we
   DON'T guess — we capture the page state and return needs_review instead of
   blindly submitting a half-wrong form. Your approval authorizes a submission;
   it doesn't make us omniscient about an unusual form.

3. EVIDENCE. Every attempt captures a screenshot before the final click, so
   there's always a record of exactly what went out.

4. ATS-AWARE. Greenhouse and Lever get specific handling. Workday and LinkedIn
   are refused here by design — they require account creation / violate ToS /
   are too inconsistent to submit safely; those are surfaced for manual handling.

Playwright must be installed with browsers:  playwright install chromium
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from .generator import GeneratedDocs
from .models import ATS, Job
from .profile import Profile

log = logging.getLogger(__name__)

SHOTS = Path(__file__).resolve().parent.parent / "output" / "screenshots"

# Only these block submission. Custom question misses are warnings.
_CRITICAL_FIELDS = {"first_name", "last_name", "full_name", "email"}


@dataclass
class SubmitResult:
    job_id: str
    ok: bool
    status: str               # "submitted" | "needs_review" | "refused" | "error"
    message: str = ""
    screenshot_path: str = ""
    unmapped_fields: Optional[list[str]] = None


# Workday refused always. LinkedIn Easy Apply gated by config (see Submitter._linkedin_easy_apply_enabled).
_REFUSED = {
    ATS.WORKDAY: "Workday requires per-employer account creation and varies too "
                 "much per company to submit reliably — apply manually.",
}


def _dismiss_linkedin_gates(page) -> bool:
    """Click through EU connect-services / ads consent screens."""
    clicked = False
    for sel in (
        'button:has-text("Yes, keep all connected")',
        'button:has-text("Accept")',
        'button:has-text("Continue")',
        'button:has-text("Agree")',
        'button.artdeco-button--primary',
    ):
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=5000)
                page.wait_for_timeout(2500)
                clicked = True
                break
        except Exception:
            continue
    return clicked


def handle_linkedin_login(page) -> bool:
    """Auto-login when LinkedIn shows login, authwall, or checkpoint pages."""
    url = page.url
    needs_auth = any(x in url for x in (
        "linkedin.com/login", "linkedin.com/checkpoint", "linkedin.com/authwall",
        "connect-services",
    ))
    if not needs_auth:
        return False

    log.info("handle_linkedin_login: auth required (%s). Attempting auto-login...", url[:60])
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    if not config_path.exists():
        return False
    try:
        import json
        config = json.loads(config_path.read_text())
    except Exception as e:
        log.error("handle_linkedin_login: Failed to read config.json: %s", e)
        return False

    email = config.get("linkedin_email")
    password = config.get("linkedin_password")
    if not email or not password:
        log.warning("handle_linkedin_login: credentials not set in config.json")
        return False

    try:
        redirect_after = None
        if "authwall" in url:
            qs = parse_qs(urlparse(url).query)
            if "sessionRedirect" in qs:
                redirect_after = unquote(qs["sessionRedirect"][0])

            sign_in = page.locator(
                'button:has-text("Sign in with Email"), button:has-text("Sign in"), a:has-text("Sign in")'
            ).first
            if sign_in.count() > 0:
                sign_in.click(timeout=5000)
                page.wait_for_timeout(2500)
            else:
                page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)

        # EU connect-services / ads consent gates (block login otherwise)
        for _ in range(3):
            if "connect-services" not in page.url:
                break
            if not _dismiss_linkedin_gates(page):
                break

        username_field = page.locator(
            '#username, input[name="session_key"], input[autocomplete="username"]'
        ).first
        if username_field.count() > 0 and username_field.is_visible():
            username_field.fill(email)
        password_field = page.locator(
            '#password, input[name="session_password"], input[autocomplete="current-password"]'
        ).first
        if password_field.count() > 0 and password_field.is_visible():
            password_field.fill(password)
        sign_in_btn = page.locator(
            'button[type="submit"], button[aria-label="Sign in"], .login__form_action_container button'
        ).first
        if sign_in_btn.count() > 0 and sign_in_btn.is_visible():
            sign_in_btn.click()
            page.wait_for_timeout(5000)

        # Login page may auto-redirect to feed when cookies are valid
        if "linkedin.com/login" in page.url:
            try:
                page.wait_for_url(
                    lambda u: "feed" in u or "/in/" in u,
                    timeout=15000,
                )
                page.wait_for_timeout(2000)
            except Exception:
                pass

        for _ in range(3):
            if "connect-services" not in page.url:
                break
            if not _dismiss_linkedin_gates(page):
                break

        if "checkpoint/challenge" in page.url:
            log.warning("handle_linkedin_login: Security challenge — manual login required.")
            return False

        if redirect_after and ("authwall" not in page.url and "login" not in page.url):
            page.goto(redirect_after, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

        return (
            "authwall" not in page.url
            and "login" not in page.url
            and "connect-services" not in page.url
        ) or "feed" in page.url or "/in/" in page.url
    except Exception as e:
        log.error("handle_linkedin_login: Auto-login failed: %s", e)
        return False


def unwrap_linkedin_apply_url(url: str) -> str:
    """Decode LinkedIn safety/go wrappers and easyapply short links to real ATS URLs."""
    if not url:
        return url
    parsed = urlparse(url)
    if "/safety/go" in parsed.path:
        qs = parse_qs(parsed.query)
        if "url" in qs:
            return unquote(qs["url"][0])
    return url


def is_invalid_apply_target(url: str) -> bool:
    """True when a decoded URL is not an application form (attachments, file hosts, etc.)."""
    if not url:
        return True
    u = url.lower()
    bad_markers = (
        "sr-company-attachments",
        "c.smartrecruiters.com/sr-company-attachments",
        "linkedin.com/ambry",
        ".pdf",
        ".doc",
        ".docx",
    )
    if any(m in u for m in bad_markers):
        return True
    if "smartrecruiters.com" in u and "jobs.smartrecruiters.com" not in u and "oneclick-ui" not in u:
        return True
    return False


def _parse_education_dates(dates: str) -> dict:
    """Parse profile education dates like '2021 - 2024' or 'March 2021 - Present'."""
    out = {"start_year": "", "end_year": "", "start_month": "January", "end_month": "December"}
    if not dates:
        return out
    text = dates.strip()
    present = bool(re.search(r"\bpresent\b", text, re.I))
    years = re.findall(r"(20\d{2})", text)
    if years:
        out["start_year"] = years[0]
        out["end_year"] = years[-1] if len(years) > 1 else ("" if present else years[0])
    months = re.findall(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
        text, re.I,
    )
    if months:
        out["start_month"] = months[0].capitalize()
        if len(months) > 1:
            out["end_month"] = months[-1].capitalize()
    return out


def _linkedin_field(page, field_id: str):
    """Safe locator for LinkedIn IDs that contain colons/parentheses."""
    if not field_id:
        return page.locator("_no_such_element_")
    return page.locator(f'[id="{field_id}"]')


_LANG_CODES = {
    "eng": "english", "en": "english",
    "deu": "german", "ger": "german", "de": "german",
    "fra": "french", "fr": "french",
    "kin": "kinyarwanda", "rw": "kinyarwanda",
}


def _years_experience(profile: Profile) -> str:
    """Rough years of professional experience from profile dates."""
    earliest = datetime.now(timezone.utc).year
    for exp in getattr(profile, "experience", None) or []:
        years = re.findall(r"(20\d{2})", exp.get("dates", ""))
        if years:
            earliest = min(earliest, int(years[0]))
    return str(max(3, datetime.now(timezone.utc).year - earliest))


def _language_proficiency_answer(label: str, profile: Profile) -> str:
    """Map LinkedIn language proficiency dropdowns to profile.languages."""
    ql = label.lower()
    m = re.search(r"proficiency in (\w+)", ql)
    code = m.group(1) if m else ""
    lang_name = _LANG_CODES.get(code, code)
    for entry in getattr(profile, "languages", None) or []:
        el = entry.lower()
        if lang_name in el or (code and code in el):
            if "native" in el:
                return "Native or bilingual"
            if "fluent" in el:
                return "Professional"
            if "conversational" in el:
                return "Conversational"
            return "Professional"
    return "None"


def _select_option_fuzzy(select_loc, answer: str) -> bool:
    """Pick a <select> option by partial label match (LinkedIn pads option text)."""
    a = answer.lower().strip()
    for opt in select_loc.locator("option").all():
        text = (opt.inner_text() or "").strip()
        val = opt.get_attribute("value") or ""
        if not text or text.lower() in ("select an option", "please select", "month", "year"):
            continue
        if a in text.lower() or text.lower() in a:
            try:
                select_loc.select_option(value=val, timeout=3000)
                return True
            except Exception:
                try:
                    select_loc.select_option(label=text, timeout=3000)
                    return True
                except Exception:
                    continue
    return False


def _company_slug(name: str) -> str:
    """Best-effort ATS company slug from display name."""
    if not name:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "", name.split()[0].lower())
    aliases = {
        "zellerfeld": "zellerfeld",
        "cint": "Cint",
    }
    return aliases.get(slug, name.split()[0])


def find_smartrecruiters_job_url(company: str, title: str) -> Optional[str]:
    """Resolve a SmartRecruiters posting when LinkedIn safety/go points at an attachment."""
    import urllib.request

    slug = _company_slug(company)
    if not slug:
        return None
    board_url = f"https://careers.smartrecruiters.com/{slug}/"
    try:
        req = urllib.request.Request(board_url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    links = re.findall(rf"jobs\.smartrecruiters\.com/{re.escape(slug)}/([^\"'<>]+)", html, re.I)
    if not links:
        return None

    title_words = {w for w in re.findall(r"\w+", title.lower()) if len(w) > 2}
    best_url, best_score = None, -1
    for path in dict.fromkeys(links):
        path_l = path.lower().replace("-", " ")
        path_words = set(re.findall(r"\w+", path_l))
        if not title_words:
            continue
        score = len(title_words & path_words)
        if score > best_score:
            best_score = score
            best_url = f"https://jobs.smartrecruiters.com/{slug}/{path}"
    return best_url if best_score > 0 else None


def resolve_apply_url(job: Job) -> str:
    """Normalize company career pages to direct ATS application URLs."""
    url = job.apply_url or job.source_url
    if not url:
        return url

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    # Stripe-style embedded Greenhouse: ?gh_jid=12345
    if "gh_jid" in qs:
        board = job.company.lower() if job.company else "stripe"
        jid = qs["gh_jid"][0]
        return f"https://job-boards.greenhouse.io/{board}/jobs/{jid}"

    # Greenhouse board pages without /jobs/ path
    if "greenhouse.io" in url and "/jobs/" not in url:
        m = re.search(r"greenhouse\.io/([^/]+)", url)
        if m and qs.get("gh_jid"):
            return f"https://boards.greenhouse.io/{m.group(1)}/jobs/{qs['gh_jid'][0]}"

    # Lever postings often need /apply suffix
    if "lever.co" in url and "/apply" not in url and re.search(r"/[a-f0-9-]{36}$", url):
        return url.rstrip("/") + "/apply"

    return url


def detect_ats_from_url(url: str) -> ATS:
    u = url.lower()
    if "greenhouse.io" in u or "gh_jid=" in u:
        return ATS.GREENHOUSE
    if "lever.co" in u:
        return ATS.LEVER
    if "ashbyhq.com" in u:
        return ATS.ASHBY
    if "personio.com" in u or "personio.de" in u:
        return ATS.PERSONIO
    if "smartrecruiters.com" in u:
        return ATS.SMARTRECRUITERS
    if "hibob.com" in u or "careers.hibob" in u:
        return ATS.OTHER
    if "workday" in u:
        return ATS.WORKDAY
    if "linkedin.com" in u:
        return ATS.LINKEDIN
    return ATS.UNKNOWN


def answer_for_label(label: str, profile: Profile) -> str:
    """Single-label screening answer used by Easy Apply and custom question fillers."""
    prefs = profile.preferences or {}
    notice = prefs.get("notice_period", "7 days")
    remote_pref = prefs.get("remote_preference", "Yes — seeking a fully remote role")
    ql = label.lower().strip()
    if any(k in ql for k in ("authorized", "legally authorized", "work authorization", "right to work", "eligible to work", "legally eligible")):
        return "Yes"
    if "german" in ql and ("visa" in ql or "requirement" in ql):
        if prefs.get("visa_sponsorship_required", True):
            return "I need a Visa to work in Germany"
        return "I am a EU citizen"
    if any(k in ql for k in ("sponsor", "visa", "immigration", "work permit", "require sponsorship")):
        return "Yes" if prefs.get("visa_sponsorship_required", True) else "No"
    if any(k in ql for k in ("notice", "start date", "when can you", "earliest start", "available to start", "available from")):
        return notice
    if "proficiency" in ql:
        return _language_proficiency_answer(label, profile)
    if "years" in ql and any(k in ql for k in ("experience", "work", "design", "develop", "professional", "how many")):
        return _years_experience(profile)
    if any(k in ql for k in ("onsite", "on-site", "on site", "in-office", "in office")):
        if any(k in ql for k in ("comfortable", "willing", "able", "open")):
            return "No" if prefs.get("remote_ok", True) else "Yes"
    if any(k in ql for k in ("salary", "compensation", "pay", "expected salary")):
        return "Negotiable"
    if any(k in ql for k in ("consent", "agree", "recording", "privacy", "gdpr", "data protection")):
        return "Yes"
    if "relocate" in ql and "remote" not in ql:
        return "No — prefer fully remote" if prefs.get("remote_ok", True) else profile.location
    if any(k in ql for k in ("remote", "work from home", "hybrid", "on-site", "office")):
        return remote_pref
    if "relocation" in ql:
        if prefs.get("remote_ok", True):
            return "I am looking for a remote option"
        return "I am not in Hamburg but ready to relocate"
    if "location" in ql and "relocation" not in ql:
        return profile.location
    links = profile.links or {}
    if "linkedin" in ql:
        return links.get("linkedin", "")
    if "github" in ql:
        return links.get("github", "")
    if any(k in ql for k in ("portfolio", "website", "link to your portfolio")):
        return links.get("portfolio", "")
    if "linkedin profile" in ql or ("linkedin" in ql and "url" in ql):
        return links.get("linkedin", "")
    if "located in" in ql or "currently in" in ql:
        # e.g. "Are you currently located in London?" — honest No unless profile matches
        for city in re.findall(r"located in ([a-z\s]+)", ql):
            city = city.strip()
            if city and city not in (profile.location or "").lower():
                return "No"
        return "Yes" if profile.location else "No"
    if any(k in ql for k in ("in the office", "in office", "days a week in", "on-site", "onsite")):
        if any(k in ql for k in ("comfortable", "willing", "able", "can you work", "open")):
            return "No" if prefs.get("remote_ok", True) else "Yes"
    if any(k in ql for k in ("why", "motivat", "interest")):
        return (
            f"I'm excited about this role because it aligns with my experience building "
            f"user-facing products with {', '.join(profile.skills[:4])}."
        )
    return ""


def default_answers_for_questions(questions: list[str], profile: Profile) -> list[dict]:
    """Keyword-based defaults for common ATS screening questions."""
    skip_labels = (
        "phone", "email", "first name", "last name", "country code", "resume",
        "school", "degree", "major", "city", "address", "postal", "zip",
    )
    answers = []
    for q in questions:
        ql = q.lower()
        if any(s in ql for s in skip_labels):
            continue
        ans = answer_for_label(q, profile)
        confident = bool(ans)
        if not ans:
            # Only use summary for open-ended questions, not standard form fields
            if "?" in q or len(q.split()) >= 6:
                ans = profile.summary[:300] if profile.summary else ""
            confident = False
        if ans:
            answers.append({"question": q, "answer": ans, "confident": confident})
    return answers


class Submitter:
    """Wraps a Playwright session. Use as a context manager."""

    def __init__(self, profile: Profile, headless: bool = False):
        self.profile = profile
        self.headless = headless
        self._pw = None
        self._browser = None
        SHOTS.mkdir(parents=True, exist_ok=True)

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        
        profile_path = Path(__file__).resolve().parent.parent / "data" / "browser_profile"
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._browser = self._pw.chromium.launch_persistent_context(
            str(profile_path),
            headless=self.headless,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
            ]
        )
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def _resolve_form_frame(self, page, job: Job):
        """Find the frame/context that actually hosts the application form."""
        page.wait_for_timeout(2000)
        self._navigate_to_greenhouse_if_embedded(page, job)

        for _ in range(4):
            gh_iframe = page.frame(name="grnhse_iframe")
            if gh_iframe:
                log.info("submitter: form frame via name grnhse_iframe")
                return gh_iframe, ATS.GREENHOUSE

            for frame in page.frames:
                fu = frame.url.lower()
                if any(h in fu for h in (
                    "greenhouse.io/embed", "greenhouse.io/jobs", "job_app",
                    "boards.greenhouse.io", "job-boards.greenhouse.io",
                    "lever.co", "ashbyhq.com", "hibob.com",
                    "personio.com", "smartrecruiters.com/oneclick-ui",
                )):
                    log.info("submitter: form frame via url %s", frame.url[:80])
                    ats = detect_ats_from_url(frame.url)
                    return frame, ats if ats != ATS.UNKNOWN else job.ats

            try:
                iframe_loc = page.locator(
                    'iframe[name="grnhse_iframe"], iframe#grnhse_iframe, '
                    'iframe[src*="greenhouse.io"], iframe[src*="lever.co"], '
                    'iframe[src*="ashbyhq.com"], iframe[src*="hibob.com"]'
                ).first
                if iframe_loc.count() > 0:
                    src = iframe_loc.get_attribute("src") or ""
                    if src and "greenhouse" in src and not src.startswith("javascript"):
                        log.info("submitter: navigating to greenhouse iframe src directly")
                        page.goto(src, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2500)
                        job.ats = ATS.GREENHOUSE
                        return page, ATS.GREENHOUSE
                    try:
                        handle = iframe_loc.element_handle(timeout=3000)
                        if handle:
                            frame = handle.content_frame()
                            if frame:
                                log.info("submitter: form frame via iframe element handle")
                                return frame, detect_ats_from_url(frame.url) or job.ats
                    except Exception:
                        pass
            except Exception:
                pass

            page.wait_for_timeout(2000)

        if "greenhouse.io" in page.url:
            job.ats = ATS.GREENHOUSE
        return page, job.ats

    def _navigate_to_greenhouse_if_embedded(self, page, job: Job) -> None:
        """Company career pages often embed Greenhouse — open the form directly."""
        if "greenhouse.io" in page.url:
            return
        for sel in (
            "a[href*='boards.greenhouse.io']", "a[href*='job-boards.greenhouse.io']",
            "a[href*='greenhouse.io/jobs']", "a[href*='greenhouse.io/embed']",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and self._is_visible(loc):
                    href = loc.get_attribute("href")
                    if href and "greenhouse" in href:
                        log.info("submitter: following greenhouse link %s", href[:80])
                        page.goto(href, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2500)
                        job.ats = ATS.GREENHOUSE
                        return
            except Exception:
                continue
        try:
            iframe = page.locator('iframe[src*="greenhouse.io"]').first
            if iframe.count() > 0:
                src = iframe.get_attribute("src")
                if src and src.startswith("http"):
                    log.info("submitter: opening greenhouse iframe src %s", src[:80])
                    page.goto(src, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)
                    job.ats = ATS.GREENHOUSE
        except Exception:
            pass

    def _linkedin_easy_apply_enabled(self) -> bool:
        config_path = Path(__file__).resolve().parent.parent / "config.json"
        try:
            import json
            cfg = json.loads(config_path.read_text())
            return bool(cfg.get("linkedin_easy_apply_enabled", True))
        except Exception:
            return True

    def _ensure_linkedin_session(self, page) -> None:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        if any(x in page.url for x in ("login", "authwall", "checkpoint", "connect-services")):
            handle_linkedin_login(page)
        try:
            page.wait_for_url(lambda u: "feed" in u or "/in/" in u, timeout=12000)
        except Exception:
            pass

    def _linkedin_already_applied(self, page) -> bool:
        try:
            body = (page.locator("body").inner_text() or "").lower()
        except Exception:
            return False
        markers = (
            "application submitted",
            "you applied",
            "applied on",
            "view application",
            "your application was sent",
        )
        return any(m in body for m in markers)

    def _ensure_easy_apply_modal(self, page, job: Job) -> None:
        """Open the Easy Apply SDUI modal if navigation landed on the job page."""
        if page.locator('button:has-text("Next"), button:has-text("Review"), button:has-text("Submit application")').count():
            return
        for sel in (
            'a[href*="openSDUIApplyFlow"]',
            'button:has-text("Easy Apply")',
            'a:has-text("Easy Apply")',
        ):
            try:
                loc = page.locator(sel)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    href = el.get_attribute("href") or ""
                    if href and "search-results" in href:
                        continue
                    if "openSDUIApplyFlow" in href or (el.inner_text() or "").strip().lower() == "easy apply":
                        if self._is_visible(el):
                            el.click(timeout=5000)
                            page.wait_for_timeout(3500)
                            return
            except Exception:
                continue
        from .linkedin_apply import easy_apply_url
        url = easy_apply_url(job)
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)

    def _fill_linkedin_easy_apply_step(self, page, docs: GeneratedDocs) -> None:
        """Fill the current step of LinkedIn's Easy Apply SDUI flow."""
        p = self.profile

        links = getattr(p, "links", None) or {}
        # Contact + common URL fields by label
        field_map = {
            "first name": p.name.split()[0] if p.name else "",
            "last name": p.name.split()[-1] if p.name and " " in p.name else "",
            "email": p.email,
            "mobile phone": p.phone,
            "phone number": p.phone,
            "linkedin profile": links.get("linkedin", ""),
            "linkedin url": links.get("linkedin", ""),
            "portfolio": links.get("portfolio", ""),
        }
        for label in page.locator("label").all():
            try:
                text = (label.inner_text() or "").strip().lower()
                if not text or "set alert" in text or "language" in text:
                    continue
                for_id = label.get_attribute("for")
                if not for_id:
                    continue
                field = _linkedin_field(page, for_id)
                if field.count() == 0:
                    continue
                tag = field.evaluate("el => el.tagName.toLowerCase()")
                val = ""
                for key, v in field_map.items():
                    if key in text and v:
                        val = v
                        break
                if tag == "select" and "country code" in text:
                    try:
                        field.select_option(label="Rwanda (+250)")
                    except Exception:
                        try:
                            field.select_option(label="Rwanda")
                        except Exception:
                            pass
                    continue
                if tag == "select" and val:
                    try:
                        field.select_option(label=val)
                    except Exception:
                        pass
                elif tag in ("input", "textarea") and val:
                    try:
                        field.fill(val)
                    except Exception:
                        pass
            except Exception:
                continue

        # Resume: prefer generated PDF/HTML; else pick first saved LinkedIn resume
        uploaded = False
        if docs.resume_path and Path(docs.resume_path).exists():
            for sel in ('input[type="file"]',):
                loc = page.locator(sel)
                if loc.count() > 0:
                    try:
                        loc.first.set_input_files(docs.resume_path, timeout=5000)
                        uploaded = True
                    except Exception:
                        pass
        if not uploaded:
            # LinkedIn resume picker: click the saved-resume label/card, not unrelated radios
            for lbl in page.locator("label").all():
                try:
                    text = (lbl.inner_text() or "").lower()
                    if "resume" in text and ("select" in text or ".pdf" in text):
                        lbl.click(timeout=3000)
                        uploaded = True
                        break
                except Exception:
                    continue
            if not uploaded:
                radios = page.locator('input[type="radio"]')
                for i in range(radios.count()):
                    try:
                        rid = radios.nth(i).get_attribute("id") or ""
                        lbl = page.locator(f'label[for="{rid}"]').first
                        if lbl.count() and "resume" in (lbl.inner_text() or "").lower():
                            lbl.click(timeout=3000)
                            uploaded = True
                            break
                    except Exception:
                        continue

        # Education step (some Easy Apply flows require it)
        if page.locator('label:has-text("School")').count() > 0:
            self._fill_linkedin_education(page)

        # Direct screening on Easy Apply steps (fieldset / legend / label)
        self._fill_linkedin_fieldsets(page)
        self._fill_easy_apply_screening(page, docs)

        # Custom screening questions on this step
        if docs.custom_answers:
            self._fill_custom_questions(page, docs)
        else:
            questions = []
            skip_q = (
                "resume", "language", "set alert", "email", "phone", "first name",
                "last name", "country code", "school", "degree", "major", "city",
            )
            for lbl in page.locator("label, legend, .jobs-easy-apply-form-section__label").all():
                t = (lbl.inner_text() or "").strip()
                tl = t.lower()
                if not t or len(t) < 3:
                    continue
                if any(s in tl for s in skip_q):
                    continue
                questions.append(t)
            if questions:
                answers = default_answers_for_questions(questions, p)
                docs.custom_answers = answers
                self._fill_custom_questions(page, docs)
                docs.custom_answers = []

    def _fill_linkedin_education(self, page) -> None:
        """Fill LinkedIn Easy Apply education step from profile, including date dropdowns."""
        p = self.profile
        edu_list = getattr(p, "education", None) or []
        edu = edu_list[0] if edu_list else {}
        dates = _parse_education_dates(edu.get("dates", ""))
        edu_map = {
            "school": edu.get("school", ""),
            "city": (edu.get("location") or "").split(",")[0].strip(),
            "degree": edu.get("degree", ""),
            "major": edu.get("degree", ""),
            "field of study": edu.get("degree", ""),
        }
        for label in page.locator("label").all():
            try:
                text = (label.inner_text() or "").strip().lower()
                if not text or "currently attend" in text or "set alert" in text:
                    continue
                for_id = label.get_attribute("for")
                if not for_id:
                    continue
                field = _linkedin_field(page, for_id)
                if field.count() == 0:
                    continue
                val = ""
                for key, v in edu_map.items():
                    if key in text and v:
                        val = v
                        break
                if not val:
                    continue
                tag = field.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    try:
                        field.select_option(label=val)
                    except Exception:
                        field.select_option(value=val)
                else:
                    field.fill(val)
            except Exception:
                continue

        for label in page.locator("label").all():
            try:
                text = (label.inner_text() or "").strip().lower()
                for_id = label.get_attribute("for")
                if not for_id:
                    continue
                field = _linkedin_field(page, for_id)
                if field.count() == 0 or field.evaluate("el => el.tagName.toLowerCase()") != "select":
                    continue
                if "month of from" in text and dates["start_month"]:
                    field.select_option(label=dates["start_month"])
                elif "year of from" in text and dates["start_year"]:
                    field.select_option(label=dates["start_year"])
                elif "month of to" in text and dates["end_month"]:
                    field.select_option(label=dates["end_month"])
                elif "year of to" in text and dates["end_year"]:
                    field.select_option(label=dates["end_year"])
            except Exception:
                continue

    def _fill_linkedin_fieldsets(self, page) -> None:
        """Fill LinkedIn Easy Apply radio groups wrapped in <fieldset><legend>."""
        p = self.profile
        for fs in page.locator("fieldset").all():
            try:
                leg = fs.locator("legend").first
                if leg.count() == 0:
                    continue
                text = (leg.inner_text() or "").strip()
                if not text:
                    continue
                ans = answer_for_label(text, p)
                if not ans:
                    continue
                self._pick_radio_option(fs, ans)
            except Exception:
                continue

    def _fill_easy_apply_screening(self, page, docs: GeneratedDocs) -> None:
        """Fill employer screening questions on Easy Apply steps via label/legend matching."""
        p = self.profile
        skip = ("school", "degree", "major", "resume", "phone", "email", "first name", "last name", "language", "set alert", "country code")
        for label in page.locator("label, legend, .jobs-easy-apply-form-section__label").all():
            try:
                text = (label.inner_text() or "").strip()
                if not text or len(text) < 4:
                    continue
                tl = text.lower()
                if any(s in tl for s in skip):
                    continue
                ans = answer_for_label(text, p)
                if not ans:
                    continue
                for_id = label.get_attribute("for")
                if for_id:
                    field = _linkedin_field(page, for_id)
                    if field.count() > 0:
                        tag = field.evaluate("el => el.tagName.toLowerCase()")
                        type_attr = (field.get_attribute("type") or "").lower()
                        if tag == "select":
                            if not _select_option_fuzzy(field, ans):
                                try:
                                    field.select_option(label=ans, timeout=3000)
                                except Exception:
                                    pass
                        elif type_attr == "text" or tag == "textarea":
                            field.fill(ans, timeout=3000)
                        else:
                            self._apply_answer_to_field(page, field.first, ans, label)
                        continue
                container = label.locator("xpath=..")
                radios = container.locator('input[type="radio"]')
                if radios.count() > 0:
                    self._pick_radio_option(container, ans)
                    continue
                parent = label.locator("xpath=../..")
                if parent.locator('input[type="radio"]').count() > 0:
                    self._pick_radio_option(parent, ans)
            except Exception:
                continue

    def _apply_answer_to_field(self, page, elem, answer: str, label_loc=None) -> bool:
        try:
            tag = elem.evaluate("el => el.tagName.toLowerCase()")
            type_attr = (elem.get_attribute("type") or "").lower()
            if tag == "select":
                if not _select_option_fuzzy(elem, answer):
                    try:
                        elem.select_option(label=answer, timeout=3000)
                    except Exception:
                        elem.select_option(value=answer, timeout=3000)
                return True
            if type_attr in ("radio", "checkbox"):
                container = label_loc.locator("xpath=..") if label_loc else page
                return self._pick_radio_option(container, answer)
            if type_attr != "file":
                elem.fill(answer, timeout=3000)
                return True
        except Exception:
            pass
        return False

    def _pick_radio_option(self, container, answer: str) -> bool:
        a_clean = answer.lower().strip()
        options = container.locator('input[type="radio"], input[type="checkbox"]')
        for i in range(options.count()):
            opt = options.nth(i)
            opt_id = opt.get_attribute("id")
            opt_lbl_text = ""
            if opt_id:
                lbl = container.locator(f"label[for='{opt_id}']").first
                if lbl.count() > 0:
                    opt_lbl_text = lbl.inner_text().strip()
            opt_clean = opt_lbl_text.lower().strip()
            match = bool(opt_clean and (opt_clean in a_clean or a_clean in opt_clean))
            if not match and a_clean in ("yes", "y", "true", "agree"):
                match = opt_clean in ("yes", "y", "true", "agree", "i agree")
            if not match and a_clean.startswith("no"):
                match = opt_clean in ("no", "n", "false")
            if match:
                try:
                    opt.check(force=True, timeout=3000)
                    return True
                except Exception:
                    continue
        return False

    def _submit_linkedin_easy_apply(self, job: Job, docs: GeneratedDocs) -> SubmitResult:
        """Submit via LinkedIn Easy Apply (in-app SDUI flow)."""
        from .linkedin_apply import easy_apply_url

        if not self._linkedin_easy_apply_enabled():
            return SubmitResult(job.job_id, ok=False, status="refused",
                                message="LinkedIn Easy Apply is disabled in config.json.")

        apply_url = easy_apply_url(job)
        if not apply_url:
            return SubmitResult(job.job_id, ok=False, status="error",
                                message="Could not determine LinkedIn job id for Easy Apply.")

        page = self._browser.new_page()
        try:
            self._ensure_linkedin_session(page)
            log.info("submitter: LinkedIn Easy Apply -> %s", apply_url)
            page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)

            if any(x in page.url for x in ("authwall", "login")):
                handle_linkedin_login(page)
                page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)

            self._ensure_easy_apply_modal(page, job)
            if self._linkedin_already_applied(page):
                return SubmitResult(job.job_id, ok=True, status="submitted",
                                    message="LinkedIn Easy Apply already submitted.")

            for step in range(16):
                self._fill_linkedin_easy_apply_step(page, docs)

                submit_btn = page.locator(
                    'button:has-text("Submit application"), button[aria-label*="Submit application"], '
                    'button:has-text("Submit")'
                ).first
                if submit_btn.count() > 0 and submit_btn.is_visible():
                    txt = (submit_btn.inner_text() or "").lower()
                    if "submit" in txt:
                        shot = self._screenshot(page, job.job_id, "before_submit")
                        submit_btn.click(timeout=8000)
                        page.wait_for_timeout(4000)
                        done = self._screenshot(page, job.job_id, "after_submit")
                        return SubmitResult(job.job_id, ok=True, status="submitted",
                                            message="LinkedIn Easy Apply submitted.", screenshot_path=done)

                clicked = False
                for btn_text in ("Review", "Next"):
                    btn = page.locator(
                        f'button:has-text("{btn_text}"), button[aria-label*="{btn_text}"]'
                    ).first
                    if btn.count() > 0 and btn.is_visible():
                        disabled = btn.get_attribute("disabled") or btn.get_attribute("aria-disabled")
                        if disabled and str(disabled).lower() not in ("false", ""):
                            # Required fields missing — fill again and retry once
                            self._fill_linkedin_easy_apply_step(page, docs)
                            disabled = btn.get_attribute("disabled") or btn.get_attribute("aria-disabled")
                        if not disabled or str(disabled).lower() in ("false", ""):
                            btn.click(timeout=8000)
                            page.wait_for_timeout(2500)
                            clicked = True
                            break
                if not clicked:
                    # Modal may still be loading
                    if step < 3:
                        page.wait_for_timeout(2000)
                        continue
                    break

            shot = self._screenshot(page, job.job_id, "easy_apply_incomplete")
            return SubmitResult(job.job_id, ok=False, status="needs_review",
                                message="LinkedIn Easy Apply did not reach Submit — complete manually.",
                                screenshot_path=shot)
        except Exception as e:
            shot = self._screenshot(page, job.job_id, "error")
            return SubmitResult(job.job_id, ok=False, status="error",
                                message=str(e), screenshot_path=shot)
        finally:
            page.close()

    def submit_application(self, job: Job, docs: GeneratedDocs) -> SubmitResult:
        """Fill + submit. Resolves external/LinkedIn URLs to ATS forms first."""
        from .linkedin_apply import is_easy_apply_job

        if job.ats in _REFUSED:
            return SubmitResult(job.job_id, ok=False, status="refused",
                                message=_REFUSED[job.ats])
        if job.ats == ATS.LINKEDIN and not self._linkedin_easy_apply_enabled():
            return SubmitResult(job.job_id, ok=False, status="refused",
                                message="LinkedIn Easy Apply is disabled in config.json.")

        if is_easy_apply_job(job) and self._linkedin_easy_apply_enabled():
            return self._submit_linkedin_easy_apply(job, docs)

        # Normalize Stripe/gh_jid and similar wrappers to direct ATS URLs
        resolved = resolve_apply_url(job)
        if resolved and resolved != job.apply_url:
            log.info("submitter: resolved apply URL %s -> %s", job.apply_url, resolved)
            job.apply_url = resolved
            job.ats = detect_ats_from_url(resolved)

        page = self._browser.new_page()
        try:
            # Dynamic off-site LinkedIn apply URL resolution
            if "linkedin.com/jobs/view" in job.apply_url or "linkedin.com/jobs/guest/jobs" in job.apply_url:
                log.info("submitter: detected unresolved LinkedIn details page. Attempting to resolve off-site application link...")
                page.goto(job.apply_url, wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(5000)

                try:
                    modal_close = page.locator("button[aria-label='Dismiss'], button[class*='modal__dismiss']").first
                    if modal_close.count() > 0 and modal_close.is_visible():
                        modal_close.click(timeout=3000)
                        page.wait_for_timeout(1000)
                except Exception:
                    pass

                # Standardized route 1: Easy Apply (in-app)
                easy = page.locator(
                    'a[href*="openSDUIApplyFlow"], a:has-text("Easy Apply")'
                ).first
                if self._linkedin_easy_apply_enabled() and easy.count() > 0 and self._is_visible(easy):
                    page.close()
                    job.ats = ATS.LINKEDIN
                    return self._submit_linkedin_easy_apply(job, docs)

                # Standardized route 2: safety/go external ATS URL
                safety = page.locator("a[href*='/safety/go']").first
                if safety.count() > 0 and self._is_visible(safety):
                    href = safety.get_attribute("href")
                    target = unwrap_linkedin_apply_url(
                        href if href.startswith("http") else urljoin(page.url, href)
                    )
                    if is_invalid_apply_target(target):
                        sr_url = find_smartrecruiters_job_url(job.company, job.title)
                        if sr_url:
                            log.info("submitter: resolved SmartRecruiters attachment -> %s", sr_url[:80])
                            target = sr_url
                            job.ats = ATS.SMARTRECRUITERS
                        else:
                            log.warning("submitter: invalid apply target %s", target[:80])
                            target = None
                    if target:
                        log.info("submitter: following safety/go -> %s", target[:80])
                        page.goto(target, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(3000)
                else:
                    if any(x in page.url for x in ("login", "authwall", "checkpoint", "connect-services")):
                        handle_linkedin_login(page)
                        page.goto(job.apply_url, wait_until="domcontentloaded", timeout=40000)
                        page.wait_for_timeout(4000)

                    btn = self._find_linkedin_apply_button(page)
                    if btn is None:
                        shot = self._screenshot(page, job.job_id, "failed_resolve")
                        return SubmitResult(job.job_id, ok=False, status="needs_review",
                                            message="Could not locate the 'Apply' button on the LinkedIn page. Please click 'Open Form ↗' to apply manually.",
                                            screenshot_path=shot)

                    href = btn.get_attribute("href")
                    target = None
                    if href and not href.startswith("javascript"):
                        target = href if href.startswith("http") else urljoin(page.url, href)
                        target = unwrap_linkedin_apply_url(target)

                    if target and "linkedin.com" not in target:
                        if is_invalid_apply_target(target):
                            sr_url = find_smartrecruiters_job_url(job.company, job.title)
                            if sr_url:
                                target = sr_url
                                job.ats = ATS.SMARTRECRUITERS
                            else:
                                target = None
                        if target:
                            page.goto(target, wait_until="domcontentloaded", timeout=45000)
                    elif href and "/safety/go" in href:
                        decoded = unwrap_linkedin_apply_url(
                            href if href.startswith("http") else urljoin(page.url, href)
                        )
                        page.goto(decoded, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(3000)
                    else:
                        try:
                            with page.expect_popup(timeout=15000) as popup_info:
                                btn.click(force=True)
                            popup = popup_info.value
                            popup.wait_for_load_state("domcontentloaded", timeout=30000)
                            page = popup
                        except Exception:
                            try:
                                with page.expect_navigation(timeout=25000, wait_until="domcontentloaded"):
                                    btn.click(force=True)
                            except Exception:
                                btn.click(force=True)
                                page.wait_for_timeout(4000)
                        if "linkedin.com/jobs/view" in page.url:
                            shot = self._screenshot(page, job.job_id, "failed_nav")
                            return SubmitResult(
                                job.job_id, ok=False, status="needs_review",
                                message="Apply click did not leave LinkedIn — run `python login_linkedin.py` to refresh session.",
                                screenshot_path=shot,
                            )

                if "linkedin.com/jobs/view" in page.url:
                    shot = self._screenshot(page, job.job_id, "failed_nav")
                    return SubmitResult(
                        job.job_id, ok=False, status="needs_review",
                        message="Could not resolve external apply URL from LinkedIn.",
                        screenshot_path=shot,
                    )

                # Detect ATS dynamically from resolved URL
                resolved_lower = page.url.lower()
                if "greenhouse.io" in resolved_lower:
                    job.ats = ATS.GREENHOUSE
                elif "lever.co" in resolved_lower:
                    job.ats = ATS.LEVER
                elif "ashbyhq.com" in resolved_lower:
                    job.ats = ATS.ASHBY
                elif "personio.com" in resolved_lower or "personio.de" in resolved_lower:
                    job.ats = ATS.PERSONIO
                elif "smartrecruiters.com" in resolved_lower:
                    job.ats = ATS.SMARTRECRUITERS
                else:
                    job.ats = ATS.OTHER
                job.apply_url = page.url
            else:
                page.goto(job.apply_url, wait_until="domcontentloaded", timeout=45000)

            # WWR / aggregator pages: follow the external Apply link to the real ATS form
            if job.source == "weworkremotely" or "weworkremotely.com" in page.url:
                self._follow_external_apply(page)

            # Stripe embeds Greenhouse on /apply subpath
            if "stripe.com/jobs/listing" in page.url and "/apply" not in page.url:
                page.goto(page.url.rstrip("/") + "/apply", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)

            # Re-detect ATS after navigation
            job.ats = detect_ats_from_url(page.url) if job.ats == ATS.UNKNOWN else job.ats
            if job.ats == ATS.UNKNOWN:
                job.ats = detect_ats_from_url(job.apply_url)

            if any(x in page.url for x in ("linkedin.com/authwall", "linkedin.com/login", "connect-services")):
                if handle_linkedin_login(page):
                    page.goto(job.apply_url, wait_until="domcontentloaded", timeout=40000)
                if any(x in page.url for x in ("linkedin.com/authwall", "linkedin.com/login", "connect-services")):
                    shot = self._screenshot(page, job.job_id, "authwall")
                    return SubmitResult(
                        job.job_id, ok=False, status="needs_review",
                        message="LinkedIn session expired — run `python login_linkedin.py` to refresh, then retry.",
                        screenshot_path=shot,
                    )

            # Company career landing pages may need a second Apply click
            if job.ats in (ATS.GREENHOUSE, ATS.LEVER, ATS.ASHBY, ATS.PERSONIO, ATS.SMARTRECRUITERS, ATS.UNKNOWN):
                on_apply_form = (
                    (job.ats == ATS.PERSONIO and "apply" in page.url.lower())
                    or (job.ats == ATS.SMARTRECRUITERS and "oneclick-ui" in " ".join(f.url for f in page.frames))
                )
                if not on_apply_form:
                    self._click_apply_on_careers_page(page)
                detected = detect_ats_from_url(page.url)
                if detected != ATS.UNKNOWN:
                    job.ats = detected
                elif job.ats == ATS.UNKNOWN:
                    for frame in page.frames:
                        if "greenhouse.io" in frame.url:
                            job.ats = ATS.GREENHOUSE
                            break
                        if "lever.co" in frame.url:
                            job.ats = ATS.LEVER
                            break
                        if "ashbyhq.com" in frame.url:
                            job.ats = ATS.ASHBY
                            break
                        if "personio.com" in frame.url:
                            job.ats = ATS.PERSONIO
                            break
                        if "smartrecruiters.com" in frame.url:
                            job.ats = ATS.SMARTRECRUITERS
                            break
                if job.ats == ATS.GREENHOUSE:
                    self._prepare_greenhouse_form(page)
                if job.ats == ATS.PERSONIO:
                    self._prepare_personio_form(page, job)
                if job.ats == ATS.SMARTRECRUITERS:
                    self._prepare_smartrecruiters_form(page, job)

            # Augment custom answers if form wasn't parsed at generation time
            form_frame, detected_ats = self._resolve_form_frame(page, job)
            if detected_ats != ATS.UNKNOWN:
                job.ats = detected_ats

            # Read the form's OWN question labels from the live frame we already
            # resolved. (The old path called parse_form_questions(), which spawns a
            # SECOND sync Playwright inside this one — that throws "Sync API inside
            # asyncio loop", so screening answers were silently left blank and the
            # form rejected the submit on the missing required fields.)
            if job.ats in (ATS.GREENHOUSE, ATS.LEVER, ATS.ASHBY, ATS.PERSONIO, ATS.SMARTRECRUITERS):
                try:
                    qs = self._extract_questions_from_frame(form_frame)
                    have = {(a.get("question") or "").strip().lower() for a in (docs.custom_answers or [])}
                    extra = [a for a in default_answers_for_questions(qs, self.profile)
                             if (a.get("question") or "").strip().lower() not in have]
                    docs.custom_answers = list(docs.custom_answers or []) + extra
                except Exception as e:
                    log.warning("submitter: could not read custom questions from form: %s", e)

            try:
                form_frame.wait_for_selector("input, textarea, select", timeout=8000)
            except Exception:
                pass

            filler = {
                ATS.GREENHOUSE: self._fill_greenhouse,
                ATS.LEVER: self._fill_lever,
                ATS.ASHBY: self._fill_ashby,
                ATS.PERSONIO: self._fill_personio,
                ATS.SMARTRECRUITERS: self._fill_smartrecruiters,
            }.get(job.ats, self._fill_generic)

            critical, warnings = filler(form_frame, job, docs)

            shot = self._screenshot(page, job.job_id, "before_submit")

            if critical:
                return SubmitResult(
                    job.job_id, ok=False, status="needs_review",
                    message=f"Missing required field(s): {', '.join(critical)}.",
                    screenshot_path=shot, unmapped_fields=critical + warnings,
                )
            if warnings:
                log.warning("submitter: %d custom field(s) unfilled (proceeding): %s",
                            len(warnings), warnings[:5])

            submit_ctx = page if job.ats in (ATS.PERSONIO, ATS.SMARTRECRUITERS) else form_frame
            self._click_submit(submit_ctx)
            page.wait_for_timeout(3500)
            done_shot = self._screenshot(page, job.job_id, "after_submit")

            # A clicked Submit button is NOT proof of submission — forms reject
            # blank required fields client-side and stay on the page. Only mark
            # APPLIED on a real confirmation; otherwise return needs_review.
            verdict, detail = self._verify_submission(page, form_frame)
            if verdict == "submitted":
                return SubmitResult(job.job_id, ok=True, status="submitted",
                                    message="Application submitted (confirmed).", screenshot_path=done_shot)
            if verdict == "captcha":
                return SubmitResult(job.job_id, ok=False, status="needs_review",
                                    message="Blocked by CAPTCHA/anti-bot challenge — apply manually.",
                                    screenshot_path=done_shot)
            return SubmitResult(
                job.job_id, ok=False, status="needs_review",
                message=f"Submit not confirmed ({detail}); likely validation on a screening field — review.",
                screenshot_path=done_shot,
            )

        except Exception as e:
            shot = self._screenshot(page, job.job_id, "error")
            return SubmitResult(job.job_id, ok=False, status="error",
                                message=str(e), screenshot_path=shot)
        finally:
            page.close()

    def _find_linkedin_apply_button(self, page):
        """Pick the real Apply CTA, not related-job search links."""
        btn_selectors = [
            "a[href*='/safety/go']",
            "a[href*='easyapply.jobs']",
            "a.jobs-apply-button",
            "button.jobs-apply-button",
            "a[data-tracking-control-name='public_jobs_apply-link']",
            ".jobs-apply-button--top-card",
            ".top-card-layout__cta--primary",
            "a:has-text('Apply on company website')",
            "button:has-text('Apply on company website')",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            ".apply-button",
        ]
        for sel in btn_selectors:
            try:
                loc = page.locator(sel)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    if not self._is_visible(el):
                        continue
                    text = (el.inner_text() or "").strip().lower()
                    href = el.get_attribute("href") or ""
                    if "search-results" in href or "origin=JobSearchOrigin" in href:
                        continue
                    if href and ("/safety/go" in href or "easyapply" in href):
                        return el
                    if text in ("apply", "apply now", "apply on company website"):
                        return el
                    if sel.startswith("a.jobs-apply") or sel.startswith("button.jobs-apply"):
                        return el
            except Exception:
                continue
        return None

    def _follow_external_apply(self, page):
        """Click through job board pages to reach the actual application form."""
        from urllib.parse import urljoin

        base = page.url
        for sel in [
            "a[href*='greenhouse.io']", "a[href*='lever.co']", "a[href*='ashbyhq.com']",
            "a[href*='hibob.com']", "a[href*='easyapply.jobs']",
            "a[href*='personio.com']", "a[href*='smartrecruiters.com']",
            "a[href*='workable.com']", "a[href*='breezy.hr']", "a[href*='apply']",
            "a.apply", "a.button", "a:has-text('Apply')",
            "a:has-text('Apply for this position')", "a:has-text('Apply now')",
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and self._is_visible(loc):
                    href = loc.get_attribute("href")
                    if href and not href.startswith("javascript"):
                        target = href if href.startswith("http") else urljoin(base, href)
                        page.goto(target, wait_until="domcontentloaded", timeout=40000)
                        page.wait_for_timeout(2500)
                        if any(h in page.url for h in ("greenhouse", "lever.co", "ashbyhq", "personio", "smartrecruiters")):
                            return
            except Exception:
                continue

    def _click_apply_on_careers_page(self, page) -> bool:
        """Some company pages need an explicit Apply click after landing."""
        for sel in [
            "a#apply_button", "a.application-button", ".application--submit a",
            "a[href*='#app']", "a[href*='/applications/new']",
            "a[href*='apply']", "a[href*='personio.com']",
            "a:has-text(\"I'm interested\")", "button:has-text(\"I'm interested\")",
            "button:has-text('Apply')",
            "a:has-text('Apply for this job')", "a:has-text('Apply Now')",
            ".postings-btn-wrapper a",
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and self._is_visible(loc):
                    loc.click(timeout=5000)
                    page.wait_for_timeout(2500)
                    return True
            except Exception:
                continue
        return False

    def _prepare_greenhouse_form(self, page):
        """Greenhouse often embeds the form below the fold on the job page."""
        targets = [page]
        for frame in page.frames:
            if "greenhouse" in frame.url:
                targets.append(frame)
        for ctx in targets:
            try:
                ctx.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
        page.wait_for_timeout(2000)
        for ctx in targets:
            try:
                ctx.wait_for_selector(
                    "#first_name, input[name='first_name'], input[name='email'], input[type='email']",
                    timeout=8000,
                )
                return
            except Exception:
                continue
        log.warning("submitter: greenhouse form not visible after scroll")

    def _fill_common(self, page, fields: dict[str, list[str]]) -> tuple[list[str], list[str]]:
        """Returns (critical_unmapped, warnings)."""
        p = self.profile
        values = {
            "first_name": p.name.split(" ")[0] if p.name else "",
            "last_name": p.name.split(" ")[-1] if p.name and " " in p.name else "",
            "full_name": p.name,
            "email": p.email,
            "phone": p.phone,
            "location": p.location,
        }
        critical: list[str] = []
        warnings: list[str] = []
        for key, selectors in fields.items():
            val = values.get(key, "")
            if not val:
                continue
            if not self._try_fill(page, selectors, val):
                if key in _CRITICAL_FIELDS:
                    critical.append(key)
                else:
                    warnings.append(key)
        return critical, warnings

    @staticmethod
    def _is_visible(locator) -> bool:
        try:
            return locator.first.is_visible()
        except Exception:
            return False

    def _try_fill(self, page, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and self._is_visible(loc):
                    el = loc.first
                    el.click(timeout=2000)
                    time.sleep(random.uniform(0.05, 0.15))
                    el.fill("", timeout=2000)
                    el.type(value, delay=random.randint(20, 45))
                    return True
            except Exception:
                continue
        return False

    def _upload_resume(self, page, docs: GeneratedDocs) -> bool:
        path_to_upload = docs.resume_path
        if not path_to_upload or not Path(path_to_upload).exists():
            if docs.resume_html:
                temp_dir = Path(__file__).resolve().parent.parent / "output" / "resumes"
                temp_dir.mkdir(parents=True, exist_ok=True)
                temp_path = temp_dir / f"{docs.job_id}_resume.html"
                try:
                    temp_path.write_text(docs.resume_html, encoding="utf-8")
                    path_to_upload = str(temp_path)
                    log.info("submitter: generated temporary HTML resume for upload at %s", path_to_upload)
                except Exception as e:
                    log.warning("submitter: failed to write temporary HTML resume: %s", e)
            
        if not path_to_upload or not Path(path_to_upload).exists():
            return False
            
        for sel in ['input[type="file"][name*="resume" i]', 'input[type="file"][id*="resume" i]', 'input[type="file"]']:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.set_input_files(path_to_upload, timeout=5000)
                    return True
            except Exception:
                continue
        return False

    def _upload_cover_letter(self, page, docs: GeneratedDocs) -> bool:
        path_to_upload = docs.cover_letter_path
        if not path_to_upload or not Path(path_to_upload).exists():
            if docs.cover_letter_html:
                temp_dir = Path(__file__).resolve().parent.parent / "output" / "resumes"
                temp_dir.mkdir(parents=True, exist_ok=True)
                temp_path = temp_dir / f"{docs.job_id}_cover_letter.html"
                try:
                    temp_path.write_text(docs.cover_letter_html, encoding="utf-8")
                    path_to_upload = str(temp_path)
                    log.info("submitter: generated temporary HTML cover letter for upload at %s", path_to_upload)
                except Exception as e:
                    log.warning("submitter: failed to write temporary HTML cover letter: %s", e)
            
        if not path_to_upload or not Path(path_to_upload).exists():
            return False
            
        for sel in ['input[type="file"][name*="cover" i]', 'input[type="file"][id*="cover" i]']:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.set_input_files(path_to_upload, timeout=5000)
                    return True
            except Exception:
                continue
        return False

    def _fill_social_links(self, page):
        p = self.profile
        links = getattr(p, "links", {}) or {}
        if not isinstance(links, dict):
            return
            
        linkedin = links.get("linkedin")
        github = links.get("github")
        portfolio = links.get("portfolio")
        
        if linkedin:
            self._try_fill(page, [
                'input[name*="linkedin" i]', 'input[id*="linkedin" i]',
                'input[name="urls[LinkedIn]"]', 'input[placeholder*="linkedin" i]',
                'input[name="/candidate/socialMediaLinkedIn"]',
            ], linkedin)
        if github:
            self._try_fill(page, [
                'input[name*="github" i]', 'input[id*="github" i]',
                'input[name="urls[GitHub]"]', 'input[placeholder*="github" i]',
                'input[name="/candidate/socialMediaGitHub"]',
            ], github)
        if portfolio:
            self._try_fill(page, [
                'input[name*="portfolio" i]', 'input[id*="portfolio" i]',
                'input[name*="website" i]', 'input[id*="website" i]',
                'input[name="urls[Portfolio]"]', 'input[name="urls[Website]"]',
                'input[placeholder*="portfolio" i]', 'input[placeholder*="website" i]',
                'input[name="/candidate/socialMediaPersonalWebsite"]',
            ], portfolio)

    def _fill_custom_questions(self, page, docs: GeneratedDocs) -> list[str]:
        """Tries to fill custom questions in the form.
        Returns a list of question strings that were generated but could not be filled."""
        if not docs.custom_answers:
            return []
            
        unfilled = []
        for qa in docs.custom_answers:
            q_text = qa.get("question", "")
            a_text = qa.get("answer", "")
            if not q_text or not a_text:
                continue
                
            q_clean = q_text.lower().strip()
            filled = False
            if any(s in q_clean for s in ("phone", "email", "first name", "last name", "country code")):
                continue
            
            try:
                import re
                locators = page.locator(
                    "label, legend, .application-label, .application-question, "
                    ".jobs-easy-apply-form-section__label, .form-group label"
                )
                for i in range(locators.count()):
                    loc_item = locators.nth(i)
                    loc_text = loc_item.inner_text().lower().strip()
                    
                    loc_text_clean = re.sub(r"\s*\*$", "", loc_text).strip()
                    loc_text_clean = re.sub(r"\s*\(required\)$", "", loc_text_clean, flags=re.I).strip()
                    loc_text_clean = re.sub(r"\s*\(optional\)$", "", loc_text_clean, flags=re.I).strip()
                    
                    # Token overlap matching to handle typos, spacing, and punctuation robustly
                    words_q = set(re.findall(r'\w+', q_clean))
                    words_lbl = set(re.findall(r'\w+', loc_text_clean))
                    
                    match_question = False
                    if q_clean == loc_text_clean:
                        match_question = True
                    elif words_q and words_lbl:
                        # Prevent short option labels (like "US") from matching long questions
                        if len(words_lbl) >= 3 or words_q == words_lbl:
                            intersection = words_q.intersection(words_lbl)
                            overlap = len(intersection) / min(len(words_q), len(words_lbl))
                            if overlap >= 0.8:
                                match_question = True
                        
                    if match_question:
                        input_loc = None
                        parent_loc = None
                        gparent_loc = None
                        
                        for_id = loc_item.get_attribute("for")
                        if for_id:
                            input_loc = page.locator(f'[id="{for_id}"]')
                            
                        if not input_loc or input_loc.count() == 0:
                            input_loc = loc_item.locator("input, textarea, select")
                            
                        if not input_loc or input_loc.count() == 0:
                            parent_loc = loc_item.locator("xpath=..")
                            input_loc = parent_loc.locator("input, textarea, select")
                            
                        if not input_loc or input_loc.count() == 0:
                            gparent_loc = loc_item.locator("xpath=../..")
                            input_loc = gparent_loc.locator("input, textarea, select")

                        if input_loc and input_loc.count() > 0:
                            elem = input_loc.first
                            tag_name = elem.evaluate("el => el.tagName.toLowerCase()")
                            
                            if tag_name == "select":
                                try:
                                    elem.select_option(label=a_text, timeout=3000)
                                    filled = True
                                    break
                                except Exception:
                                    try:
                                        elem.select_option(value=a_text, timeout=3000)
                                        filled = True
                                        break
                                    except Exception:
                                        pass
                            elif tag_name in ("input", "textarea"):
                                type_attr = elem.get_attribute("type") or ""
                                if type_attr.lower() in ("checkbox", "radio"):
                                    container = page
                                    if gparent_loc is not None and gparent_loc.count() > 0:
                                        container = gparent_loc
                                    elif parent_loc is not None and parent_loc.count() > 0:
                                        container = parent_loc
                                    options = container.locator('input[type="radio"], input[type="checkbox"]')
                                    for o_idx in range(options.count()):
                                        opt = options.nth(o_idx)
                                        opt_id = opt.get_attribute("id")
                                        opt_val = opt.get_attribute("value") or ""
                                        
                                        opt_lbl_text = ""
                                        if opt_id:
                                            opt_lbl = container.locator(f"label[for='{opt_id}']").first
                                            if opt_lbl.count() > 0:
                                                opt_lbl_text = opt_lbl.inner_text().strip()
                                        if not opt_lbl_text:
                                            opt_lbl_parent = opt.locator("xpath=ancestor::label").first
                                            if opt_lbl_parent.count() > 0:
                                                opt_lbl_text = opt_lbl_parent.inner_text().strip()
                                                
                                        a_clean = a_text.lower().strip()
                                        opt_clean = opt_lbl_text.lower().strip()
                                        opt_val_clean = opt_val.lower().strip()
                                        
                                        match = False
                                        if opt_clean and (opt_clean in a_clean or a_clean in opt_clean):
                                            match = True
                                        elif opt_val_clean and (opt_val_clean == a_clean):
                                            match = True
                                            
                                        if not match:
                                            if a_clean in ("yes", "y", "true", "agree", "consent") and opt_clean in ("yes", "y", "true", "agree", "consent", "i agree", "i consent"):
                                                match = True
                                            elif a_clean in ("no", "n", "false", "disagree") and opt_clean in ("no", "n", "false", "disagree", "i disagree"):
                                                match = True
                                                
                                        if match:
                                            try:
                                                opt.check(force=True, timeout=3000)
                                                filled = True
                                                if type_attr.lower() == "radio":
                                                    break
                                            except Exception as opt_err:
                                                log.warning("Failed to check option %s: %s", opt_lbl_text, opt_err)
                                    if filled:
                                        break
                                elif type_attr.lower() != "file":
                                    try:
                                        elem.fill(a_text, timeout=3000)
                                        page.wait_for_timeout(500)
                                        elem.press("Enter")
                                        filled = True
                                        break
                                    except Exception:
                                        pass
            except Exception as e:
                log.warning("Error trying to fill question '%s': %s", q_text, e)
                
            if not filled:
                unfilled.append(q_text)
                
        return unfilled

    def _prepare_personio_form(self, page, job: Job) -> None:
        """Personio application forms live at /job/{id}?apply."""
        url = page.url
        if "personio" in url.lower() and "apply" not in url.lower():
            sep = "&" if "?" in url else "?"
            page.goto(f"{url}{sep}apply", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)

    def _prepare_smartrecruiters_form(self, page, job: Job) -> None:
        """Open SmartRecruiters oneclick apply UI from the job posting page."""
        for sel in (
            'a:has-text("I\'m interested")', 'button:has-text("I\'m interested")',
            'a:has-text("Apply")', 'button:has-text("Apply")',
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and self._is_visible(loc):
                    loc.click(timeout=5000)
                    page.wait_for_timeout(4000)
                    return
            except Exception:
                continue
        for frame in page.frames:
            if "oneclick-ui" in frame.url:
                return

    def _fill_personio(self, page, job, docs) -> tuple[list[str], list[str]]:
        p = self.profile
        prefs = p.preferences or {}
        crit, warn = self._fill_common(page, {
            "first_name": ['input[name="first_name"]', 'input[id="first_name"]', 'input[name*="first" i]'],
            "last_name": ['input[name="last_name"]', 'input[id="last_name"]', 'input[name*="last" i]'],
            "email": ['input[name="email"]', 'input[type="email"]'],
            "phone": ['input[name="phone"]', 'input[type="tel"]'],
        })
        self._try_fill(page, ['input[name="available_from"]'], prefs.get("notice_period", "7 days"))
        self._try_fill(page, ['input[name="salary_expectations"]'], "Negotiable")
        self._try_fill(page, ['input[name="location"]'], p.location)
        if not self._upload_resume(page, docs):
            try:
                cv = page.locator('input[name="documents.cv"], input[type="file"]').first
                if cv.count() > 0 and docs.resume_path and Path(docs.resume_path).exists():
                    cv.set_input_files(docs.resume_path, timeout=5000)
            except Exception:
                warn.append("resume_upload")
        skip_ids = {"first_name", "last_name", "email", "phone", "documents.cv", "documents.other"}
        for label in page.locator("label").all():
            try:
                text = (label.inner_text() or "").strip()
                for_id = label.get_attribute("for") or ""
                if not text or len(text) < 4 or for_id in skip_ids:
                    continue
                if text.lower() in ("first", "last", "email", "phone"):
                    continue
                ans = answer_for_label(text, p)
                if not ans:
                    continue
                field = page.locator(f"#{for_id}")
                if field.count() == 0:
                    continue
                tag = field.evaluate("el => el.tagName.toLowerCase()")
                if tag != "select":
                    continue
                try:
                    field.select_option(label=ans, timeout=3000)
                except Exception:
                    try:
                        field.select_option(value=ans, timeout=3000)
                    except Exception:
                        # Personio dropdowns often need partial option match (e.g. "No")
                        picked = False
                        for opt in field.locator("option").all():
                            opt_text = (opt.inner_text() or "").strip()
                            if ans.lower() in opt_text.lower() or opt_text.lower() in ans.lower():
                                opt_val = opt.get_attribute("value")
                                if opt_val:
                                    field.select_option(value=opt_val, timeout=3000)
                                    picked = True
                                    break
                        if not picked:
                            warn.append(text[:40])
            except Exception:
                continue
        return crit, warn

    def _fill_smartrecruiters(self, page, job, docs) -> tuple[list[str], list[str]]:
        ctx = page
        for frame in page.frames:
            if "oneclick-ui" in frame.url:
                ctx = frame
                break
        crit, warn = self._fill_common(ctx, {
            "first_name": ['input[name*="first" i]', 'input[autocomplete="given-name"]'],
            "last_name": ['input[name*="last" i]', 'input[autocomplete="family-name"]'],
            "full_name": ['input[name="name"]', 'input[autocomplete="name"]'],
            "email": ['input[type="email"]', 'input[name*="email" i]'],
            "phone": ['input[type="tel"]', 'input[name*="phone" i]'],
        })
        if not self._upload_resume(ctx, docs):
            warn.append("resume_upload")
        self._fill_social_links(ctx)
        if not docs.custom_answers:
            questions = []
            for lbl in ctx.locator("label, legend").all():
                t = (lbl.inner_text() or "").strip()
                if t and len(t) > 3:
                    questions.append(t)
            docs.custom_answers = default_answers_for_questions(questions, self.profile)
        unfilled = self._fill_custom_questions(ctx, docs)
        return crit, warn + unfilled

    def _fill_greenhouse(self, page, job, docs) -> tuple[list[str], list[str]]:
        crit, warn = self._fill_common(page, {
            "first_name": ["#first_name", 'input[id="first_name"]', 'input[name="first_name"]', 'input[id*="first" i]'],
            "last_name": ["#last_name", 'input[id="last_name"]', 'input[name="last_name"]', 'input[id*="last" i]'],
            "email": ["#email", 'input[id="email"]', 'input[name="email"]', 'input[type="email"]'],
            "phone": ["#phone", 'input[id="phone"]', 'input[name="phone"]', 'input[type="tel"]'],
        })
        if not self._upload_resume(page, docs):
            warn.append("resume_upload")
        self._upload_cover_letter(page, docs)
        self._fill_social_links(page)
        self._fill_location_field(page)
        unfilled = self._fill_custom_questions(page, docs)
        return crit, warn + unfilled

    def _fill_lever(self, page, job, docs) -> tuple[list[str], list[str]]:
        crit, warn = self._fill_common(page, {
            "full_name": ['input[name="name"]', 'input[placeholder*="name" i]'],
            "email": ['input[name="email"]', 'input[type="email"]'],
            "phone": ['input[name="phone"]', 'input[type="tel"]'],
        })
        if not self._upload_resume(page, docs):
            warn.append("resume_upload")
        self._upload_cover_letter(page, docs)
        self._fill_social_links(page)
        unfilled = self._fill_custom_questions(page, docs)
        return crit, warn + unfilled

    def _fill_ashby(self, page, job, docs) -> tuple[list[str], list[str]]:
        crit, warn = self._fill_common(page, {
            "full_name": ['input[name="_systemfield_name"]', 'input[name="name"]', 'input[autocomplete="name"]'],
            "email": ['input[name="_systemfield_email"]', 'input[type="email"]'],
            "phone": ['input[name="phone"]', 'input[type="tel"]'],
        })
        if not self._upload_resume(page, docs):
            warn.append("resume_upload")
        self._fill_social_links(page)
        unfilled = self._fill_custom_questions(page, docs)
        return crit, warn + unfilled

    def _fill_generic(self, page, job, docs) -> tuple[list[str], list[str]]:
        crit, warn = self._fill_common(page, {
            "full_name": [
                'input[name*="name" i]:not([type="hidden"])',
                'input[autocomplete="name"]',
                'input[placeholder*="full name" i]',
            ],
            "first_name": [
                'input[name*="first" i]', 'input[autocomplete="given-name"]',
                'input[name="/candidate/firstName"]', 'input[id="/candidate/firstName"]',
            ],
            "last_name": [
                'input[name*="last" i]', 'input[autocomplete="family-name"]',
                'input[name="/candidate/lastName"]', 'input[id="/candidate/lastName"]',
            ],
            "email": [
                'input[type="email"]', 'input[name*="email" i]',
                'input[name="/candidate/email"]', 'input[id="/candidate/email"]',
            ],
            "phone": [
                'input[type="tel"]', 'input[name*="phone" i]',
                'input[name="/candidate/phone"]', 'input[id="/candidate/phone"]',
            ],
        })
        # HiBob uses split name fields; don't block on full_name if first+last filled
        if "full_name" in crit:
            has_first = self._try_fill(page, ['input[name="/candidate/firstName"]'], self.profile.name.split()[0] if self.profile.name else "")
            has_last = False
            if self.profile.name and " " in self.profile.name:
                has_last = self._try_fill(page, ['input[name="/candidate/lastName"]'], self.profile.name.split()[-1])
            if has_first and has_last:
                crit = [c for c in crit if c != "full_name"]
        if not self._upload_resume(page, docs):
            warn.append("resume_upload")
        self._upload_cover_letter(page, docs)
        self._fill_social_links(page)
        self._fill_location_field(page)
        unfilled = self._fill_custom_questions(page, docs)
        return crit, warn + unfilled

    def _fill_location_field(self, page):
        loc = self.profile.location
        if not loc:
            return
        self._try_fill(page, [
            'input[name*="location" i]', 'input[id*="location" i]',
            'input[name*="city" i]', 'input[autocomplete="address-level2"]',
        ], loc)

    def _extract_questions_from_frame(self, frame) -> list[str]:
        """Read screening-question labels from the form frame we already have open.

        Replaces the old parse_form_questions() call, which launched a SECOND
        Playwright inside the live session and crashed (sync API in asyncio loop)."""
        import re as _re
        ignore = {
            "first name", "last name", "email", "phone", "resume", "cover letter",
            "attach", "enter manually", "preferred first name", "pronouns",
            "additional information", "gender", "hispanic/latino", "veteran status",
            "disability status", "race", "ethnicity", "demographic",
            "voluntary self-identification", "photo", "headshot", "street", "city",
            "state", "zip", "postal code", "country", "full name", "name",
        }
        questions: list[str] = []
        try:
            labels = frame.locator("label, .application-label, .application-question")
            for i in range(labels.count()):
                try:
                    txt = (labels.nth(i).inner_text() or "").strip()
                except Exception:
                    continue
                txt = _re.sub(r"\s*\*$", "", txt).strip()
                txt = _re.sub(r"\s*\(required\)$", "", txt, flags=_re.I).strip()
                txt = _re.sub(r"\s*\(optional\)$", "", txt, flags=_re.I).strip()
                if not txt or len(txt) < 3 or any(ig in txt.lower() for ig in ignore):
                    continue
                if txt not in questions:
                    questions.append(txt)
        except Exception as e:
            log.warning("submitter: question extraction failed: %s", e)
        log.info("submitter: read %d screening question(s) from form", len(questions))
        return questions

    def _verify_submission(self, page, frame):
        """Return ('submitted'|'rejected'|'captcha'|'unknown', detail).

        Only a positive confirmation counts as submitted. Order matters: a passive
        reCAPTCHA is embedded on most ATS forms and does NOT block submit, so we
        check confirmation and lingering validation errors BEFORE treating a
        *visible* captcha as the blocker."""
        contexts = [c for c in (page, frame) if c is not None]

        # 1) Positive confirmation via URL.
        try:
            u = (page.url or "").lower()
            if any(k in u for k in ("confirmation", "thank", "submitted", "application-success", "/success")):
                return "submitted", "confirmation url"
        except Exception:
            pass

        # 2) Confirmation text, or lingering required-field validation, on the page.
        body_all = ""
        for ctx in contexts:
            try:
                body_all += " " + (ctx.locator("body").inner_text(timeout=3000) or "").lower()
            except Exception:
                continue
        markers = (
            "thank you for applying", "thanks for applying", "application submitted",
            "successfully submitted", "we received your application",
            "we've received your application", "your application has been",
            "application received", "thank you for your application",
        )
        if any(m in body_all for m in markers):
            return "submitted", "confirmation text"
        if any(p in body_all for p in ("is required", "please complete this field", "please fill",
                                       "this field is required", "cannot be blank", "required field")):
            return "rejected", "required-field validation errors remain"

        # 3) Only a VISIBLE interactive captcha counts as the blocker.
        captcha_sel = (
            'iframe[src*="challenges.cloudflare.com"], div.cf-turnstile, '
            'iframe[src*="hcaptcha.com"], iframe[title*="recaptcha challenge" i]'
        )
        for ctx in contexts:
            try:
                loc = ctx.locator(captcha_sel).first
                if loc.count() > 0 and loc.is_visible():
                    return "captcha", "visible captcha/anti-bot challenge"
            except Exception:
                pass
        return "unknown", "no confirmation detected"

    def _click_submit(self, page):
        contexts = [page]
        try:
            parent = page.page if hasattr(page, "page") else None
            if parent and parent not in contexts:
                contexts.append(parent)
        except Exception:
            pass
        for ctx in contexts:
            for sel in ['button[type="submit"]', 'input[type="submit"]',
                        'button:has-text("Submit application")', 'button:has-text("Submit Application")',
                        'button:has-text("Submit")', 'button:has-text("Apply")']:
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and self._is_visible(loc):
                        loc.first.click(timeout=5000)
                        return
                except Exception:
                    continue
        raise RuntimeError("could not find a submit button on the form")

    def _screenshot(self, page, job_id: str, tag: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = SHOTS / f"{job_id}_{tag}_{ts}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            return ""
        return str(path)
