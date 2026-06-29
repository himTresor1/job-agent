"""One-page review dashboard.

Run:  python -m jobagent.dashboard   (or: flask --app jobagent.dashboard run)
Then open http://127.0.0.1:5000

The page shows scored jobs above a threshold, each with its generated resume,
cover letter, and any custom-question answers. Three actions per job:

  approve  -> generate docs if needed, then submit via Playwright, immediately.
  skip     -> mark skipped, never shown again.
  blacklist-> hide the whole company.

"approve = submit immediately" is honored literally. Because there's no second
confirmation, the page deliberately shows the FULL rendered documents and every
custom answer (guessed answers flagged) BEFORE you can click — the review is the
safeguard, so everything you'd want to check is on screen first.

Generated docs are cached per job so the review you approve is exactly what gets
submitted — no regeneration between looking and sending.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

import json
from flask import Flask, jsonify, render_template_string, request

from .database import Database
from .generator import GeneratedDocs, get_generator
from .scorer import get_scorer
from .models import Status
from .profile import Profile
from .submitter import Submitter

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent

# In-memory cache of generated docs, keyed by job_id, so the artifact you
# reviewed is byte-for-byte the artifact that gets submitted.
_DOC_CACHE: dict[str, GeneratedDocs] = {}
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_RUNNING = False


def run_discovery_background(db: Database, profile: Profile, config: dict):
    global _DISCOVERY_RUNNING
    with _DISCOVERY_LOCK:
        if _DISCOVERY_RUNNING:
            return
        _DISCOVERY_RUNNING = True
        
    try:
        log.info("Starting background discovery run...")
        from .pipeline import Pipeline
        pipeline = Pipeline(db, profile, config)
        pipeline.run()
        log.info("Background discovery run completed successfully!")
    except Exception as e:
        log.error("Background discovery run failed: %s", e)
    finally:
        with _DISCOVERY_LOCK:
            _DISCOVERY_RUNNING = False


def _html_to_text(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def create_app(db: Database | None = None, profile: Profile | None = None,
               min_score: float = 70.0) -> Flask:
    app = Flask(__name__)
    
    # Load config and set environment variables
    config_path = ROOT / "config.json"
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if "gemini_api_key" in config and config["gemini_api_key"]:
                os.environ["GEMINI_API_KEY"] = config["gemini_api_key"]
            if "anthropic_api_key" in config and config["anthropic_api_key"]:
                os.environ["ANTHROPIC_API_KEY"] = config["anthropic_api_key"]
            if "openai_api_key" in config and config["openai_api_key"]:
                os.environ["OPENAI_API_KEY"] = config["openai_api_key"]
        except Exception as e:
            log.warning("failed to load config in dashboard: %s", e)

    app.config["CONFIG"] = config
    app.config["DB"] = db or Database()
    app.config["PROFILE"] = profile or Profile.load(ROOT / "profile.json")
    app.config["MIN_SCORE"] = min_score
    app.config["GENERATOR"] = get_generator()
    app.config["SCORER"] = get_scorer()

    @app.route("/")
    def index():
        db = app.config["DB"]
        profile = app.config["PROFILE"]
        config = app.config["CONFIG"]
        threading.Thread(target=run_discovery_background, args=(db, profile, config), daemon=True).start()
        return render_template_string(_PAGE)

    @app.route("/api/discovery/status")
    def api_discovery_status():
        global _DISCOVERY_RUNNING
        return jsonify({"running": _DISCOVERY_RUNNING})

    @app.route("/api/jobs")
    def api_jobs():
        """Jobs filtered by status."""
        d: Database = app.config["DB"]
        
        status_str = request.args.get("status", "scored").lower()
        try:
            status = Status(status_str)
        except ValueError:
            status = Status.SCORED

        min_score_val = request.args.get("min_score")
        if min_score_val is not None:
            try:
                min_score = float(min_score_val)
            except ValueError:
                min_score = app.config["MIN_SCORE"]
        else:
            min_score = app.config["MIN_SCORE"]

        if status == Status.SCORED:
            # Score filtering only applies to SCORED status
            jobs = d.get_jobs(status=Status.SCORED, min_score=min_score if min_score > 0 else None, order_by_score=True)
            new_jobs = d.get_jobs(status=Status.NEW)
            jobs = new_jobs + jobs
        else:
            jobs = d.get_jobs(status=status, order_by_score=True)
            
        import html
        out = []
        for j in jobs:
            docs = _DOC_CACHE.get(j.job_id)
            out.append({
                "job_id": j.job_id,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "score": j.score,
                "rationale": j.score_rationale,
                "ats": j.ats.value,
                "apply_url": j.apply_url,
                "description": html.unescape(j.description or ""),
                "has_docs": docs is not None,
                "resume_html": docs.resume_html if docs else "",
                "cover_letter_html": docs.cover_letter_html if docs else "",
                "cover_letter_text": _html_to_text(docs.cover_letter_html) if docs else "",
                "custom_answers": docs.custom_answers if docs else [],
                "needs_attention": docs.needs_attention if docs else False,
            })
        return jsonify(out)

    @app.route("/api/jobs/<job_id>/score", methods=["POST"])
    def api_job_score(job_id):
        d: Database = app.config["DB"]
        profile = Profile.load(ROOT / "profile.json")
        scorer = app.config["SCORER"]
        
        job = d.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "message": "job not found"}), 404
            
        score, rationale = scorer.score(job, profile)
        if score < 60.0:
            d.update_status(job_id, Status.SKIPPED, score=score, score_rationale=rationale)
        else:
            d.save_score(job_id, score, rationale)
            
        # Reload updated job
        job = d.get_job(job_id)
        return jsonify({
            "score": job.score,
            "rationale": job.score_rationale
        })

    @app.route("/api/unskip", methods=["POST"])
    def api_unskip():
        d: Database = app.config["DB"]
        job_id = request.json.get("job_id")
        d.update_status(job_id, Status.SCORED)
        _DOC_CACHE.pop(job_id, None)
        return jsonify({"ok": True})

    @app.route("/api/jobs/<job_id>/docs")
    def api_job_docs(job_id):
        d: Database = app.config["DB"]
        profile = Profile.load(ROOT / "profile.json")
        gen = app.config["GENERATOR"]
        
        job = d.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "message": "job not found"}), 404
            
        docs = _DOC_CACHE.get(job_id)
        if docs is None:
            docs = gen.generate(job, profile)
            _DOC_CACHE[job_id] = docs
            
        return jsonify({
            "resume_html": docs.resume_html,
            "cover_letter_html": docs.cover_letter_html,
            "cover_letter_text": _html_to_text(docs.cover_letter_html),
            "custom_answers": docs.custom_answers,
            "needs_attention": docs.needs_attention,
        })

    @app.route("/api/jobs/<job_id>/regenerate", methods=["POST"])
    def api_job_regenerate(job_id):
        _DOC_CACHE.pop(job_id, None)
        d: Database = app.config["DB"]
        profile = Profile.load(ROOT / "profile.json")
        gen = app.config["GENERATOR"]
        
        job = d.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "message": "job not found"}), 404
            
        docs = gen.generate(job, profile)
        _DOC_CACHE[job_id] = docs
        
        return jsonify({
            "resume_html": docs.resume_html,
            "cover_letter_html": docs.cover_letter_html,
            "cover_letter_text": _html_to_text(docs.cover_letter_html),
            "custom_answers": docs.custom_answers,
            "needs_attention": docs.needs_attention,
        })

    @app.route("/api/approve", methods=["POST"])
    def api_approve():
        """Approve = submit immediately. The reviewed, cached docs are sent."""
        job_id = request.json.get("job_id")
        edited_answers = request.json.get("custom_answers")
        edited_cover = request.json.get("cover_letter")
        
        d: Database = app.config["DB"]
        profile = Profile.load(ROOT / "profile.json")
        job = d.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "message": "job not found"}), 404

        docs = _DOC_CACHE.get(job_id)
        if docs is None:
            docs = app.config["GENERATOR"].generate(job, profile)
            
        if edited_answers is not None:
            docs.custom_answers = edited_answers
            
        if edited_cover is not None:
            paras = [p.strip() for p in edited_cover.split("\n\n") if p.strip()]
            docs.cover_letter_html = '\n<div class="doc cover">\n' + "".join(f"  <p>{p}</p>\n" for p in paras) + '</div>'

        headless = os.environ.get("JOBAGENT_HEADLESS", "0") == "1"
        try:
            with Submitter(profile, headless=headless) as sub:
                result = sub.submit_application(job, docs)
        except Exception as e:
            log.error("Failed to run Submitter: %s", e)
            return jsonify({
                "ok": False,
                "status": "error",
                "message": f"Browser session conflict: {e}. If you have 'login_linkedin.py' running in your terminal or a Chromium window open, please close it first to release the profile lock."
            })

        if result.ok:
            d.update_status(job_id, Status.APPLIED, notes=result.message)
        elif result.status in ("needs_review", "refused"):
            d.update_status(job_id, Status.ERROR, notes=result.message)

        return jsonify({
            "ok": result.ok, "status": result.status, "message": result.message,
            "screenshot": result.screenshot_path,
            "unmapped_fields": result.unmapped_fields,
        })

    @app.route("/api/approve_bulk", methods=["POST"])
    def api_approve_bulk():
        """Launches background submissions for a list of job IDs."""
        job_ids = request.json.get("job_ids", [])
        if not job_ids:
            return jsonify({"ok": False, "message": "No jobs selected"}), 400

        edits = request.json.get("edits", {})
        d: Database = app.config["DB"]
        profile = Profile.load(ROOT / "profile.json")
        gen = app.config["GENERATOR"]

        def run_bulk_submissions():
            headless = os.environ.get("JOBAGENT_HEADLESS", "0") == "1"
            for job_id in job_ids:
                job = d.get_job(job_id)
                if not job:
                    continue
                docs = _DOC_CACHE.get(job_id)
                if docs is None:
                    docs = gen.generate(job, profile)
                if job_id in edits:
                    job_edit = edits[job_id]
                    if "cover_letter" in job_edit:
                        paras = [p.strip() for p in job_edit["cover_letter"].split("\n\n") if p.strip()]
                        docs.cover_letter_html = '\n<div class="doc cover">\n' + "".join(f"  <p>{p}</p>\n" for p in paras) + '</div>'
                    if "custom_answers" in job_edit:
                        docs.custom_answers = job_edit["custom_answers"]
                
                try:
                    with Submitter(profile, headless=headless) as sub:
                        result = sub.submit_application(job, docs)
                    if result.ok:
                        d.update_status(job_id, Status.APPLIED, notes=result.message)
                    elif result.status in ("needs_review", "refused"):
                        d.update_status(job_id, Status.ERROR, notes=result.message)
                except Exception as e:
                    d.update_status(job_id, Status.ERROR, notes=str(e))

        threading.Thread(target=run_bulk_submissions, daemon=True).start()
        return jsonify({"ok": True, "message": "Bulk submission started in background."})

    @app.route("/api/skip", methods=["POST"])
    def api_skip():
        d: Database = app.config["DB"]
        d.update_status(request.json["job_id"], Status.SKIPPED)
        _DOC_CACHE.pop(request.json["job_id"], None)
        return jsonify({"ok": True})

    @app.route("/api/blacklist", methods=["POST"])
    def api_blacklist():
        d: Database = app.config["DB"]
        d.blacklist_company(request.json["company"])
        return jsonify({"ok": True})

    return app


# The single page. Self-contained: HTML + CSS + JS inline, no build step.
_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JobAgent — Application Workstation</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Lora:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
<style>
  :root {
    --ink: #1c1b19;
    --paper: #fbfaf7;
    --line: #e4e1d8;
    --muted: #6b6960;
    --accent: #2e4d36;
    --accent-soft: #eaf1ec;
    --flag: #9a4a2f;
    --flag-soft: #fbf0ea;
    --shadow: 0 1px 2px rgba(0,0,0,.03), 0 4px 12px rgba(0,0,0,.04);
    --sidebar-bg: #ffffff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: 'Inter', system-ui, sans-serif;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .topbar {
    height: 60px;
    background: #fff;
    border-bottom: 1px solid var(--line);
    padding: 0 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
  }
  .topbar h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--accent);
  }
  .topbar .count {
    font-size: 13px;
    color: var(--muted);
    font-weight: 500;
  }
  .main-container {
    display: flex;
    flex: 1;
    overflow: hidden;
  }
  
  /* COLUMN 1: Sidebar (20%) */
  .sidebar {
    width: 23%;
    border-right: 1px solid var(--line);
    background: var(--sidebar-bg);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .status-tabs {
    display: flex;
    border-bottom: 1px solid var(--line);
    background: #faf9f6;
    padding: 0 8px;
  }
  .status-tab {
    flex: 1;
    text-align: center;
    padding: 10px 0;
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all 0.15s ease;
  }
  .status-tab:hover {
    color: var(--ink);
  }
  .status-tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }
  .sidebar-header {
    padding: 16px;
    border-bottom: 1px solid var(--line);
    background: #faf9f6;
  }
  .search-input {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 12px;
    font-family: inherit;
    outline: none;
  }
  .search-input:focus {
    border-color: var(--accent);
  }
  .bulk-controls {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
  }
  .bulk-controls label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
    cursor: pointer;
  }
  .sidebar-list {
    flex: 1;
    overflow-y: auto;
  }
  .job-item {
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    display: flex;
    align-items: flex-start;
    gap: 10px;
    transition: background 0.15s ease;
  }
  .job-item:hover {
    background: #fbfbf9;
  }
  .job-item.active {
    background: var(--accent-soft);
  }
  .job-item input[type="checkbox"] {
    margin-top: 3px;
    cursor: pointer;
  }
  .job-item-details {
    flex: 1;
    min-width: 0;
  }
  .job-item-title {
    font-size: 13.5px;
    font-weight: 600;
    margin-bottom: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .job-item-company {
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .job-item-score {
    font-size: 11px;
    font-weight: 700;
    color: var(--accent);
    background: rgba(46, 77, 54, 0.1);
    padding: 2px 6px;
    border-radius: 4px;
    margin-top: 4px;
    display: inline-block;
  }
  
  /* COLUMN 2: Details (37%) */
  .details-pane {
    width: 37%;
    border-right: 1px solid var(--line);
    overflow-y: auto;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    background: #fcfbf9;
  }
  .details-header h2 {
    margin: 0 0 6px;
    font-size: 20px;
    font-weight: 700;
    line-height: 1.3;
  }
  .details-header .meta {
    font-size: 13.5px;
    color: var(--muted);
    font-weight: 500;
  }
  .callout-score {
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 16px;
    box-shadow: var(--shadow);
  }
  .callout-score-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
  }
  .callout-score-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--muted);
  }
  .callout-score-val {
    font-size: 20px;
    font-weight: 700;
    color: var(--accent);
  }
  .callout-score-rationale {
    font-size: 12.5px;
    line-height: 1.5;
    color: var(--muted);
    font-style: italic;
  }
  .desc-section h3 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    margin: 0 0 10px;
  }
  .desc-content {
    font-size: 13.5px;
    line-height: 1.6;
    white-space: pre-wrap;
    font-family: inherit;
    color: #333;
  }
  .desc-content.has-html {
    white-space: normal;
  }
  .desc-content p, .desc-content ul, .desc-content ol, .desc-content li, .desc-content h1, .desc-content h2, .desc-content h3, .desc-content h4, .desc-content div {
    white-space: normal;
  }
  .desc-content h1, .desc-content h2, .desc-content h3, .desc-content h4 {
    margin-top: 18px;
    margin-bottom: 8px;
    font-weight: 600;
    color: var(--ink);
  }
  .desc-content h1 { font-size: 16px; }
  .desc-content h2 { font-size: 15px; }
  .desc-content h3 { font-size: 14px; }
  .desc-content h4 { font-size: 13px; }
  .desc-content p { margin-top: 0; margin-bottom: 12px; }
  .desc-content ul, .desc-content ol { margin-top: 0; margin-bottom: 12px; padding-left: 20px; }
  .desc-content li { margin-bottom: 6px; }

  /* COLUMN 3: Workstation (40%) */
  .workstation-pane {
    width: 40%;
    overflow-y: auto;
    padding: 24px;
    background: #fff;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .workstation-title {
    font-size: 15px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
    color: var(--muted);
    border-bottom: 2px solid var(--line);
    padding-bottom: 8px;
    margin: 0;
  }
  .workstation-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .workstation-section h4 {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--ink);
  }
  .workstation-textarea {
    width: 100%;
    min-height: 180px;
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-family: 'Lora', Georgia, serif;
    font-size: 14px;
    line-height: 1.6;
    resize: vertical;
    outline: none;
  }
  .workstation-textarea:focus {
    border-color: var(--accent);
  }
  
  .qa-block {
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 14px;
    margin-bottom: 12px;
  }
  .qa-block label {
    display: block;
    font-size: 12.5px;
    font-weight: 600;
    margin-bottom: 8px;
  }
  .qa-block .flag-badge {
    color: var(--flag);
    background: var(--flag-soft);
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 6px;
    border-radius: 4px;
    margin-left: 6px;
    display: inline-block;
  }
  .qa-textarea {
    width: 100%;
    font-size: 13px;
    font-family: inherit;
    padding: 8px 10px;
    border: 1px solid var(--line);
    border-radius: 6px;
    resize: vertical;
    outline: none;
  }
  .qa-textarea:focus {
    border-color: var(--accent);
  }
  .qa-textarea.flagged {
    border-color: var(--flag);
  }
  
  /* Resume bullet preview style */
  .resume-preview {
    font-family: 'Lora', Georgia, serif;
    font-size: 13px;
    line-height: 1.5;
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 16px;
    max-height: 250px;
    overflow-y: auto;
  }
  .resume-preview h1 { font-size: 15px; margin: 0 0 4px; font-weight: 700; }
  .resume-preview h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--accent); margin: 12px 0 6px; font-weight: 600;}
  .resume-preview ul { margin: 4px 0 8px; padding-left: 16px; }
  .resume-preview li { margin-bottom: 4px; }
  
  /* Toolbar styling */
  .toolbar {
    display: flex;
    gap: 8px;
    align-items: center;
    border-top: 1px solid var(--line);
    padding-top: 16px;
    background: #fff;
    position: sticky;
    bottom: 0;
  }
  .toolbar-spacer { flex: 1; }
  
  button {
    font-size: 13px;
    font-weight: 600;
    border: 1px solid var(--line);
    background: #fff;
    color: var(--ink);
    padding: 10px 16px;
    border-radius: 8px;
    cursor: pointer;
    font-family: inherit;
    transition: all 0.1s ease;
  }
  button:hover { background: #f5f4f0; }
  button:active { transform: translateY(1px); }
  button.btn-primary {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
  }
  button.btn-primary:hover { background: #233c2a; }
  button.btn-danger {
    color: var(--flag);
    border-color: var(--flag);
  }
  button.btn-danger:hover {
    background: var(--flag-soft);
  }
  
  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }
  .sync-spinner {
    display: inline-block;
    animation: spin 2s linear infinite;
  }
  
  .toast {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%);
    background: var(--ink);
    color: #fff;
    padding: 12px 20px;
    border-radius: 8px;
    font-size: 13.5px;
    font-weight: 500;
    box-shadow: 0 4px 20px rgba(0,0,0,0.15);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.25s ease;
    z-index: 100;
  }
  .toast.show { opacity: 1; }
  .toast.warn { background: var(--flag); }
  
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    flex: 1;
    color: var(--muted);
    font-style: italic;
    padding: 40px;
    text-align: center;
  }
</style>
</head>
<body>
  <div class="topbar">
    <div style="display: flex; align-items: center; gap: 12px;">
      <h1>JobAgent Application Workstation</h1>
      <span id="sync-indicator" style="font-size: 12px; color: var(--accent); background: var(--accent-soft); padding: 2px 8px; border-radius: 12px; font-weight: 500; display: none; align-items: center; gap: 6px;">
        <span class="sync-spinner">🔄</span> Syncing postings...
      </span>
    </div>
    <span class="count" id="count">Loading...</span>
  </div>
  <div class="main-container">
    <div class="sidebar">
      <div class="status-tabs">
        <div class="status-tab active" data-status="scored" onclick="switchStatus('scored')">Review</div>
        <div class="status-tab" data-status="applied" onclick="switchStatus('applied')">Applied</div>
        <div class="status-tab" data-status="error" onclick="switchStatus('error')">Errors</div>
        <div class="status-tab" data-status="skipped" onclick="switchStatus('skipped')">Skipped</div>
      </div>
      <div class="sidebar-header">
        <input type="text" class="search-input" id="search" placeholder="Search title or company..." oninput="onSearch()">
        <div id="score-filter-row" style="display: flex; gap: 8px; margin-bottom: 12px; align-items: center;">
          <label style="font-size: 12px; font-weight: 600; color: var(--muted); white-space: nowrap;">Min Fit Score:</label>
          <select id="score-filter" onchange="onScoreFilterChange()" style="flex: 1; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px; font-size: 12px; font-family: inherit; background: #fff; outline: none; cursor: pointer;">
            <option value="0">Show All</option>
            <option value="50">50%+</option>
            <option value="70" selected>70%+</option>
            <option value="80">80%+</option>
            <option value="90">90%+</option>
          </select>
        </div>
        <div class="bulk-controls">
          <label><input type="checkbox" id="select-all" onchange="toggleSelectAll()"> Select All</label>
          <button onclick="bulkApprove()">Approve Selected</button>
        </div>
      </div>
      <div class="sidebar-list" id="sidebar-list"></div>
    </div>
    
    <!-- COL 2: Details -->
    <div class="details-pane" id="details-pane">
      <div class="empty-state">Select a job from the sidebar to review details.</div>
    </div>
    
    <!-- COL 3: Workstation -->
    <div class="workstation-pane" id="workstation-pane">
      <div class="empty-state">Select a job from the sidebar to edit your application.</div>
    </div>
  </div>
  
  <div class="toast" id="toast"></div>

<script>
const $ = (s, el=document) => el.querySelector(s);
let JOBS = [];
let CURRENT_JOB_ID = null;
let SELECTED_JOBS = new Set();
let EDITS = {}; // job_id -> { cover_letter: "", custom_answers: [] }
let CURRENT_STATUS = "scored";

const STATUS_LABELS = {
  "scored": "awaiting review",
  "applied": "applied",
  "error": "with errors",
  "skipped": "skipped"
};

function toast(msg, warn) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (warn ? " warn" : "");
  setTimeout(() => t.className = "toast", 4000);
}

async function load() {
  try {
    const minScore = $("#score-filter") ? $("#score-filter").value : "70";
    const r = await fetch(`/api/jobs?status=${CURRENT_STATUS}&min_score=${minScore}`);
    JOBS = await r.json();
    
    // Initialize edits store
    JOBS.forEach(j => {
      if (!EDITS[j.job_id]) {
        EDITS[j.job_id] = {
          cover_letter: j.cover_letter_text || "",
          custom_answers: JSON.parse(JSON.stringify(j.custom_answers || []))
        };
      }
    });
    
    onSearch(); // Filters and renders
    
    if (JOBS.length > 0) {
      if (!CURRENT_JOB_ID || !JOBS.some(x => x.job_id === CURRENT_JOB_ID)) {
        selectJob(JOBS[0].job_id);
      } else {
        selectJob(CURRENT_JOB_ID);
      }
    } else {
      CURRENT_JOB_ID = null;
      const label = STATUS_LABELS[CURRENT_STATUS] || CURRENT_STATUS;
      $("#count").textContent = `0 jobs ${label}`;
      $("#details-pane").innerHTML = `<div class="empty-state">No jobs ${label}.</div>`;
      $("#workstation-pane").innerHTML = '<div class="empty-state">Workspace empty.</div>';
    }
  } catch (e) {
    toast("Failed to load jobs: " + e.message, true);
  }
}

function switchStatus(status) {
  CURRENT_STATUS = status;
  
  // Update tab UI
  document.querySelectorAll(".status-tab").forEach(tab => {
    if (tab.dataset.status === status) {
      tab.classList.add("active");
    } else {
      tab.classList.remove("active");
    }
  });
  
  // Show/hide score filter row
  const filterRow = $("#score-filter-row");
  if (filterRow) {
    filterRow.style.display = status === "scored" ? "flex" : "none";
  }
  
  // Clear selection
  $("#select-all").checked = false;
  SELECTED_JOBS.clear();
  
  load();
}

function onScoreFilterChange() {
  load();
}

function onSearch() {
  const query = $("#search").value.toLowerCase();
  const filtered = JOBS.filter(j => 
    j.title.toLowerCase().includes(query) || 
    j.company.toLowerCase().includes(query)
  );
  
  const label = STATUS_LABELS[CURRENT_STATUS] || CURRENT_STATUS;
  $("#count").textContent = `${JOBS.length} jobs ${label}`;
  
  const list = $("#sidebar-list");
  if (filtered.length === 0) {
    list.innerHTML = '<div style="padding:16px; color:var(--muted); font-size:12.5px; text-align:center; font-style:italic;">No matches</div>';
    return;
  }
  
  list.innerHTML = filtered.map(j => {
    const isSelected = SELECTED_JOBS.has(j.job_id) ? "checked" : "";
    const isActive = j.job_id === CURRENT_JOB_ID ? "active" : "";
    const scoreText = j.score !== null && j.score !== undefined ? `${Math.round(j.score)}% fit` : "No score";
    return `<div class="job-item ${isActive}" onclick="selectJob('${j.job_id}', event)">
      <input type="checkbox" ${isSelected} onclick="toggleSelect('${j.job_id}', event)">
      <div class="job-item-details">
        <div class="job-item-title">${esc(j.title)}</div>
        <div class="job-item-company">${esc(j.company)} · ${esc(j.location || "Remote")}</div>
        <div class="job-item-score">${scoreText} (${esc(j.ats)})</div>
      </div>
    </div>`;
  }).join("");
}

function selectJob(jobId, event) {
  // If clicked inside checkbox, do not select card
  if (event && event.target.type === "checkbox") return;
  
  // Save current values before switching
  saveCurrentWorkstationEdits();
  
  CURRENT_JOB_ID = jobId;
  
  // Highlight active
  onSearch(); // Re-render sidebar items to highlight active
  
  const job = JOBS.find(j => j.job_id === jobId);
  if (!job) return;
  
  // On-the-fly Scoring if job has no score yet
  if (job.score === null || job.score === undefined) {
    $("#details-pane").innerHTML = `
      <div class="empty-state">
        <span class="sync-spinner">🔄</span> Analyzing job compatibility with Gemini...
      </div>
    `;
    $("#workstation-pane").innerHTML = `
      <h3 class="workstation-title">Application Workstation</h3>
      <div class="empty-state">
        Waiting for compatibility analysis...
      </div>
    `;
    
    fetch(`/api/jobs/${jobId}/score`, { method: "POST" })
      .then(r => r.json())
      .then(res => {
        if (CURRENT_JOB_ID !== jobId) return;
        job.score = res.score;
        job.rationale = res.rationale;
        if (res.score < 60.0) {
          toast("Auto-skipped: score " + Math.round(res.score) + "% is below 60%", false);
          removeCard(jobId);
        } else {
          onSearch();
          selectJob(jobId);
        }
      })
      .catch(err => {
        if (CURRENT_JOB_ID !== jobId) return;
        $("#details-pane").innerHTML = `
          <div class="empty-state" style="color: var(--flag);">
            Failed to score job: ${err.message}
            <button style="margin-top:12px;" onclick="selectJob('${jobId}')">Retry</button>
          </div>
        `;
      });
    return;
  }
  
  // Render Details (Pane 2)
  const details = $("#details-pane");
  const scoreText = job.score !== null && job.score !== undefined ? `${Math.round(job.score)}%` : "No score";
  const hasHtml = /<[a-z][\s\S]*>/i.test(job.description || "");
  const descClass = hasHtml ? "desc-content has-html" : "desc-content";
  details.innerHTML = `
    <div class="details-header">
      <h2>${esc(job.title)}</h2>
      <div class="meta">${esc(job.company)} · ${esc(job.location)} · ${esc(job.ats.toUpperCase())}</div>
    </div>
    <div class="callout-score">
      <div class="callout-score-header">
        <span class="callout-score-label">MATCH SCORE</span>
        <span class="callout-score-val">${scoreText}</span>
      </div>
      <div class="callout-score-rationale">${esc(job.rationale || "No rationale")}</div>
    </div>
    <div class="desc-section">
      <h3>Job Description &amp; Requirements</h3>
      <div class="${descClass}">${job.description || "No description provided."}</div>
    </div>
  `;
  
  // Render Workstation (Pane 3)
  const workstation = $("#workstation-pane");
  
  if (!job.has_docs) {
    workstation.innerHTML = `
      <h3 class="workstation-title">Application Workstation</h3>
      <div class="empty-state">
        <span class="sync-spinner">🔄</span> Tailoring application via Gemini...
      </div>
    `;
    
    fetch(`/api/jobs/${jobId}/docs`)
      .then(r => r.json())
      .then(res => {
        // Double check we are still on the same job
        if (CURRENT_JOB_ID !== jobId) return;
        
        job.has_docs = true;
        job.resume_html = res.resume_html;
        job.cover_letter_html = res.cover_letter_html;
        job.cover_letter_text = res.cover_letter_text;
        job.custom_answers = res.custom_answers;
        job.needs_attention = res.needs_attention;
        
        EDITS[jobId] = {
          cover_letter: res.cover_letter_text || "",
          custom_answers: JSON.parse(JSON.stringify(res.custom_answers || []))
        };
        
        selectJob(jobId);
      })
      .catch(err => {
        if (CURRENT_JOB_ID !== jobId) return;
        workstation.innerHTML = `
          <h3 class="workstation-title">Application Workstation</h3>
          <div class="empty-state" style="color: var(--flag);">
            Failed to generate tailored application: ${err.message}
            <button style="margin-top:12px;" onclick="selectJob('${jobId}')">Retry</button>
          </div>
        `;
      });
    return;
  }
  
  const jobEdits = EDITS[jobId];
  const qaHtml = (jobEdits.custom_answers || []).map((qa, i) => `
    <div class="qa-block">
      <label>${esc(qa.question)}${qa.confident === false ? '<span class="flag-badge">guessed</span>' : ''}</label>
      <textarea class="qa-textarea ${qa.confident === false ? 'flagged' : ''}" 
                data-idx="${i}" rows="2">${esc(qa.answer || "")}</textarea>
    </div>
  `).join("");
  
  let submitBtn = `<button class="btn-primary" onclick="approveCurrent()">Approve &amp; Submit</button>`;
  let skipBtn = `<button onclick="skipCurrent()">Skip</button>`;
  let regenBtn = `<button onclick="regenerateCurrent()">Regenerate Docs</button>`;
  
  if (CURRENT_STATUS === "applied") {
    submitBtn = `<button class="btn-primary" disabled style="opacity: 0.6; cursor: not-allowed;">Already Applied</button>`;
    skipBtn = "";
  } else if (CURRENT_STATUS === "skipped") {
    submitBtn = "";
    skipBtn = `<button class="btn-primary" onclick="unskipCurrent()">Unskip (Restore)</button>`;
    regenBtn = "";
  }
  
  workstation.innerHTML = `
    <h3 class="workstation-title">Application Workstation</h3>
    
    <div class="workstation-section">
      <h4>Cover Letter</h4>
      <textarea class="workstation-textarea" id="ws-cover-letter">${esc(jobEdits.cover_letter)}</textarea>
    </div>
    
    ${qaHtml ? `<div class="workstation-section">
      <h4>ATS Form Questions</h4>
      <div>${qaHtml}</div>
    </div>` : ''}
    
    <div class="workstation-section">
      <h4>Tailored Resume Bullets Preview</h4>
      <div class="resume-preview">${job.resume_html || "<em>No resume generated</em>"}</div>
    </div>
    
    <div class="toolbar">
      ${submitBtn}
      ${skipBtn}
      ${regenBtn}
      <div class="toolbar-spacer"></div>
      <a href="${esc(job.apply_url)}" target="_blank" style="font-size:12.5px; font-weight:600; color:var(--muted); text-decoration:none;">Open Form ↗</a>
      <button class="btn-danger" onclick="blacklistCurrent('${esc(job.company)}')">Block Company</button>
    </div>
  `;
}

function saveCurrentWorkstationEdits() {
  if (!CURRENT_JOB_ID) return;
  const coverEl = $("#ws-cover-letter");
  if (coverEl) {
    EDITS[CURRENT_JOB_ID].cover_letter = coverEl.value;
  }
  document.querySelectorAll(".qa-textarea").forEach(ta => {
    const idx = +ta.dataset.idx;
    if (EDITS[CURRENT_JOB_ID].custom_answers[idx]) {
      EDITS[CURRENT_JOB_ID].custom_answers[idx].answer = ta.value;
    }
  });
}

function toggleSelect(jobId, event) {
  event.stopPropagation();
  if (SELECTED_JOBS.has(jobId)) {
    SELECTED_JOBS.delete(jobId);
  } else {
    SELECTED_JOBS.add(jobId);
  }
  onSearch();
}

function toggleSelectAll() {
  const isChecked = $("#select-all").checked;
  if (isChecked) {
    JOBS.forEach(j => SELECTED_JOBS.add(j.job_id));
  } else {
    SELECTED_JOBS.clear();
  }
  onSearch();
}

function approveCurrent() {
  saveCurrentWorkstationEdits();
  const job = JOBS.find(j => j.job_id === CURRENT_JOB_ID);
  if (!job) return;
  
  const edits = EDITS[CURRENT_JOB_ID];
  const btn = $(".btn-primary");
  btn.disabled = true;
  btn.textContent = "Submitting...";
  
  fetch("/api/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_id: CURRENT_JOB_ID,
      cover_letter: edits.cover_letter,
      custom_answers: edits.custom_answers
    })
  })
  .then(r => r.json())
  .then(res => {
    if (res.ok) {
      toast("Submitted application successfully!", false);
      removeCard(CURRENT_JOB_ID);
    } else {
      toast("Error: " + res.message, true);
      btn.disabled = false;
      btn.textContent = "Approve & Submit";
    }
  })
  .catch(e => {
    toast("Request failed: " + e.message, true);
    btn.disabled = false;
    btn.textContent = "Approve & Submit";
  });
}

function skipCurrent() {
  fetch("/api/skip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: CURRENT_JOB_ID })
  })
  .then(() => {
    toast("Skipped listing", false);
    removeCard(CURRENT_JOB_ID);
  });
}

function unskipCurrent() {
  fetch("/api/unskip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: CURRENT_JOB_ID })
  })
  .then(r => r.json())
  .then(res => {
    if (res.ok) {
      toast("Restored job to active review", false);
      removeCard(CURRENT_JOB_ID);
    } else {
      toast("Failed to restore job: " + res.message, true);
    }
  })
  .catch(err => {
    toast("Request failed: " + err.message, true);
  });
}

function regenerateCurrent() {
  if (!CURRENT_JOB_ID) return;
  const ws = $("#workstation-pane");
  ws.innerHTML = `
    <h3 class="workstation-title">Application Workstation</h3>
    <div class="empty-state">
      <span class="sync-spinner">🔄</span> Regenerating application via Gemini...
    </div>
  `;
  
  fetch(`/api/jobs/${CURRENT_JOB_ID}/regenerate`, { method: "POST" })
    .then(r => r.json())
    .then(res => {
      const job = JOBS.find(j => j.job_id === CURRENT_JOB_ID);
      if (job) {
        job.resume_html = res.resume_html;
        job.cover_letter_html = res.cover_letter_html;
        job.cover_letter_text = res.cover_letter_text;
        job.custom_answers = res.custom_answers;
        job.needs_attention = res.needs_attention;
        
        EDITS[CURRENT_JOB_ID] = {
          cover_letter: res.cover_letter_text || "",
          custom_answers: JSON.parse(JSON.stringify(res.custom_answers || []))
        };
      }
      selectJob(CURRENT_JOB_ID);
      toast("Regenerated docs successfully", false);
    })
    .catch(err => {
      toast("Regeneration failed: " + err.message, true);
      selectJob(CURRENT_JOB_ID);
    });
}

function blacklistCurrent(company) {
  fetch("/api/blacklist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ company })
  })
  .then(() => {
    toast("Blocked company: " + company, false);
    load();
  });
}

function bulkApprove() {
  saveCurrentWorkstationEdits();
  if (SELECTED_JOBS.size === 0) {
    toast("Select at least one job for bulk approval.", true);
    return;
  }
  
  const selectedList = Array.from(SELECTED_JOBS);
  const bulkEdits = {};
  selectedList.forEach(id => {
    if (EDITS[id]) {
      bulkEdits[id] = EDITS[id];
    }
  });
  
  toast(`Initiating bulk approval for ${SELECTED_JOBS.size} jobs...`, false);
  
  fetch("/api/approve_bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_ids: selectedList,
      edits: bulkEdits
    })
  })
  .then(r => r.json())
  .then(res => {
    if (res.ok) {
      toast("Bulk submission triggered. Progress updates in console/logs.", false);
      selectedList.forEach(id => removeCard(id));
      SELECTED_JOBS.clear();
      $("#select-all").checked = false;
    } else {
      toast("Bulk trigger failed: " + res.message, true);
    }
  })
  .catch(e => {
    toast("Request failed: " + e.message, true);
  });
}

function removeCard(jobId) {
  JOBS = JOBS.filter(x => x.job_id !== jobId);
  SELECTED_JOBS.delete(jobId);
  delete EDITS[jobId];
  
  const label = STATUS_LABELS[CURRENT_STATUS] || CURRENT_STATUS;
  $("#count").textContent = `${JOBS.length} jobs ${label}`;
  
  if (JOBS.length > 0) {
    if (CURRENT_JOB_ID === jobId) {
      selectJob(JOBS[0].job_id);
    } else {
      onSearch();
    }
  } else {
    CURRENT_JOB_ID = null;
    $("#details-pane").innerHTML = `<div class="empty-state">No jobs ${label}.</div>`;
    $("#workstation-pane").innerHTML = '<div class="empty-state">Workspace empty.</div>';
    onSearch();
  }
}

function esc(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

let IS_SYNCING = false;
function checkDiscoveryStatus() {
  fetch("/api/discovery/status")
    .then(r => r.json())
    .then(res => {
      const ind = $("#sync-indicator");
      if (res.running) {
        ind.style.display = "inline-flex";
        IS_SYNCING = true;
      } else {
        ind.style.display = "none";
        if (IS_SYNCING) {
          IS_SYNCING = false;
          toast("Background job sync completed!", false);
          load();
        }
      }
    })
    .catch(e => console.error("Failed to check sync status:", e));
}

load();
checkDiscoveryStatus();
setInterval(checkDiscoveryStatus, 4000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    min_score = float(os.environ.get("JOBAGENT_MIN_SCORE", "70"))
    port = int(os.environ.get("PORT", "5005"))
    create_app(min_score=min_score).run(debug=False, port=port)
