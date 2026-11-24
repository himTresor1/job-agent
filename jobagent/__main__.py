"""Command-line entry point for the foundation.

    python -m jobagent run        # discover + score
    python -m jobagent list       # show top scored jobs
    python -m jobagent status     # status counts
    python -m jobagent skip <id>  # mark a job skipped
    python -m jobagent blacklist <company>

Flags: --config, --profile, --db, --min-score
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .database import Database
from .models import Status
from .pipeline import Pipeline
from .profile import Profile

ROOT = Path(__file__).resolve().parent.parent


def _load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"config not found: {p} (copy config.example.json)", file=sys.stderr)
        sys.exit(1)
    return json.loads(p.read_text())


def _load_profile(path: str) -> Profile:
    p = Path(path)
    if not p.exists():
        print(f"profile not found: {p} (copy profile.example.json)", file=sys.stderr)
        sys.exit(1)
    return Profile.load(p)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jobagent")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--profile", default=str(ROOT / "profile.json"))
    parser.add_argument("--db", default=None)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    p_list = sub.add_parser("list")
    p_list.add_argument("--limit", type=int, default=15)
    p_skip = sub.add_parser("skip"); p_skip.add_argument("job_id")
    p_bl = sub.add_parser("blacklist"); p_bl.add_argument("company")
    sub.add_parser("login-linkedin")
    sub.add_parser("auto-submit")
    sub.add_parser("linkedin-run")
    sub.add_parser("linkedin-apply")
    sub.add_parser("daily-apply")

    import os
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Automatically set API keys from config if available
    config_path = Path(args.config)
    if config_path.exists():
        try:
            config_data = json.loads(config_path.read_text())
            if "gemini_api_key" in config_data and config_data["gemini_api_key"]:
                os.environ["GEMINI_API_KEY"] = config_data["gemini_api_key"]
            if "openai_api_key" in config_data and config_data["openai_api_key"]:
                os.environ["OPENAI_API_KEY"] = config_data["openai_api_key"]
            if "anthropic_api_key" in config_data and config_data["anthropic_api_key"]:
                os.environ["ANTHROPIC_API_KEY"] = config_data["anthropic_api_key"]
        except Exception as e:
            sys.stderr.write(f"Warning: failed to load config for API keys: {e}\n")

    db = Database(args.db) if args.db else Database()

    if args.cmd == "status":
        print(json.dumps(db.counts_by_status(), indent=2))
        return

    if args.cmd == "skip":
        db.update_status(args.job_id, Status.SKIPPED)
        print(f"skipped {args.job_id}")
        return

    if args.cmd == "blacklist":
        db.blacklist_company(args.company)
        print(f"blacklisted {args.company}")
        return

    if args.cmd == "list":
        min_score = args.min_score
        jobs = db.get_jobs(min_score=min_score, order_by_score=True, limit=args.limit)
        if not jobs:
            print("no jobs yet — run `python -m jobagent run` first")
            return
        for j in jobs:
            score = f"{j.score:.0f}" if j.score is not None else " ?"
            print(f"[{score:>3}%] {j.title}  @ {j.company}  ({j.ats.value})")
            if j.score_rationale:
                print(f"        {j.score_rationale}")
            print(f"        {j.job_id}  {j.apply_url}")
        return

    if args.cmd == "run":
        config = _load_config(args.config)
        profile = _load_profile(args.profile)
        pipeline = Pipeline(db, profile, config)
        result = pipeline.run()
        print(json.dumps(result, indent=2))
        return

    if args.cmd == "login-linkedin":
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        profile_path = ROOT / "data" / "browser_profile"
        profile_path.mkdir(parents=True, exist_ok=True)
        print("Launching Chromium in headful mode. Please log in to LinkedIn...")
        print("Close the browser window when you are done to save your login session.")
        context = pw.chromium.launch_persistent_context(str(profile_path), headless=False)
        page = context.new_page()
        page.goto("https://www.linkedin.com/login")
        # Wait until browser is closed
        try:
            while context.pages:
                page.wait_for_timeout(1000)
        except Exception:
            pass
        finally:
            context.close()
            pw.stop()
            print("Session saved successfully.")
        return

    if args.cmd == "linkedin-run":
        config = _load_config(args.config)
        profile = _load_profile(args.profile)
        pipeline = Pipeline(db, profile, config)
        max_apply = int(config.get("linkedin_apply_count", 15))
        result = pipeline.linkedin_run(max_apply=max_apply)
        print(json.dumps({
            "discovered": result["discovered"],
            "scored": result["scored"],
            "submitted": result["submitted"],
            "linkedin_jobs_in_db": len(result["linkedin_total"]),
            "status_counts": result["status_counts"],
        }, indent=2))
        return

    if args.cmd == "linkedin-apply":
        import os
        os.environ.setdefault("JOBAGENT_SKIP_FORM_PARSE", "1")
        config = _load_config(args.config)
        profile = _load_profile(args.profile)
        pipeline = Pipeline(db, profile, config)
        max_apply = int(config.get("linkedin_apply_count", 15))
        min_score = args.min_score or float(config.get("linkedin_apply_min_score", 75))
        result = pipeline.linkedin_auto_submit(max_apply=max_apply, min_score=min_score)
        print(json.dumps(result, indent=2))
        return

    if args.cmd == "daily-apply":
        import os
        os.environ.setdefault("JOBAGENT_SKIP_FORM_PARSE", "1")
        config = _load_config(args.config)
        profile = _load_profile(args.profile)
        pipeline = Pipeline(db, profile, config)
        result = pipeline.daily_apply()
        print(json.dumps(result, indent=2, default=str))
        return

    if args.cmd == "auto-submit":
        import os
        os.environ.setdefault("JOBAGENT_SKIP_FORM_PARSE", "1")
        config = _load_config(args.config)
        profile = _load_profile(args.profile)
        min_score = args.min_score or config.get("auto_submit_score_threshold", 80.0)
        remote_only = config.get("auto_submit_remote_only", True)
        headless = config.get("auto_submit_headless", True)
        allow_unconfident = config.get("auto_submit_unconfident", True)
        max_apps = int(os.environ.get("JOBAGENT_MAX_APPLY", config.get("auto_submit_max_per_run", 25)))

        from .generator import get_generator
        from .submitter import Submitter

        jobs = db.get_jobs(
            status=Status.SCORED,
            min_score=min_score,
            order_by_score=True,
            remote_only=remote_only,
        )[:max_apps]
        print(f"Found {len(jobs)} remote jobs at or above {min_score}% for auto-submission.")

        gen = get_generator()
        results = {"attempted": 0, "submitted": 0, "failed": 0, "skipped": 0, "details": []}

        with Submitter(profile, headless=headless) as sub:
            for job in jobs:
                results["attempted"] += 1
                print(f"Auto-submitting {job.title} @ {job.company} ({job.score:.0f}%)...")
                try:
                    docs = gen.generate(job, profile)
                except Exception as e:
                    results["failed"] += 1
                    db.update_status(job.job_id, Status.ERROR, notes=f"generate failed: {e}")
                    results["details"].append({"job_id": job.job_id, "ok": False, "error": str(e)})
                    continue

                if docs.needs_attention and not allow_unconfident:
                    results["skipped"] += 1
                    print("  -> Skipped: unconfident custom answers")
                    continue

                try:
                    result = sub.submit_application(job, docs)
                except Exception as e:
                    results["failed"] += 1
                    db.update_status(job.job_id, Status.ERROR, notes=str(e))
                    results["details"].append({"job_id": job.job_id, "ok": False, "error": str(e)})
                    print(f"  -> Error: {e}")
                    continue

                if result.ok:
                    results["submitted"] += 1
                    db.update_status(job.job_id, Status.APPLIED, notes=result.message)
                    print("  -> Submitted!")
                else:
                    results["failed"] += 1
                    db.update_status(job.job_id, Status.ERROR, notes=result.message)
                    print(f"  -> Failed: {result.message}")
                results["details"].append({
                    "job_id": job.job_id,
                    "ok": result.ok,
                    "status": result.status,
                    "message": result.message,
                })

        print(f"Auto-submission complete: {results['submitted']}/{results['attempted']} submitted.")
        print(json.dumps(results, indent=2))
        return


if __name__ == "__main__":
    main()
