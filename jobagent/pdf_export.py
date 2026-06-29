"""Render generated HTML documents to PDF for ATS uploads."""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "resumes"

_PDF_WORKER = """
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

html_path, pdf_path = sys.argv[1], sys.argv[2]
html = Path(html_path).read_text(encoding="utf-8")
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content(html, wait_until="load")
    page.pdf(path=pdf_path, format="Letter", print_background=True)
    browser.close()
"""


def html_to_pdf(html: str, out_path: Path) -> str:
    """Convert HTML fragment to PDF via an isolated subprocess (avoids asyncio conflicts)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; font-size: 11pt;
         line-height: 1.45; color: #111; margin: 0.6in; }}
  h1 {{ font-size: 18pt; margin: 0 0 4px; }}
  h2 {{ font-size: 10pt; text-transform: uppercase; letter-spacing: 0.04em;
        color: #2e4d36; margin: 14px 0 6px; }}
  .meta {{ font-size: 10pt; color: #444; margin-bottom: 12px; }}
  .entry-head {{ font-weight: 600; margin-bottom: 2px; }}
  .dates {{ color: #666; font-weight: 400; }}
  ul {{ margin: 4px 0 10px; padding-left: 18px; }}
  li {{ margin-bottom: 3px; }}
  p {{ margin: 0 0 10px; }}
</style></head><body>{html}</body></html>"""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp:
            tmp.write(doc)
            tmp_path = tmp.name
        proc = subprocess.run(
            [sys.executable, "-c", _PDF_WORKER, tmp_path, str(out_path)],
            capture_output=True, text=True, timeout=60,
        )
        Path(tmp_path).unlink(missing_ok=True)
        if proc.returncode == 0 and out_path.exists():
            return str(out_path)
        log.warning("pdf subprocess failed: %s", (proc.stderr or proc.stdout)[:300])
    except Exception as e:
        log.warning("pdf export failed (%s); falling back to HTML", e)
    html_path = out_path.with_suffix(".html")
    html_path.write_text(doc, encoding="utf-8")
    return str(html_path)


def ensure_pdf_paths(docs, job_id: str) -> None:
    if docs.resume_html:
        docs.resume_path = html_to_pdf(docs.resume_html, OUTPUT_DIR / f"{job_id}_resume.pdf")
    if docs.cover_letter_html:
        docs.cover_letter_path = html_to_pdf(
            docs.cover_letter_html, OUTPUT_DIR / f"{job_id}_cover_letter.pdf"
        )
