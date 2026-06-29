import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        profile_path = Path(__file__).resolve().parent / "data" / "browser_profile"
        print(f"Launching Chromium with profile: {profile_path}")
        print("----------------------------------------------------------------")
        print("A Chromium browser window will open. Please log in to LinkedIn.")
        print("Once logged in, you can close the browser window or let the script finish.")
        print("The script will keep the session open for 5 minutes, saving your login cookies.")
        print("----------------------------------------------------------------")
        
        context = p.chromium.launch_persistent_context(
            str(profile_path),
            headless=False,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
            ]
        )
        page = context.new_page()
        page.goto("https://www.linkedin.com/feed")
        
        # Try automatic login using credentials if present in config.json
        try:
            from jobagent.submitter import handle_linkedin_login
            if handle_linkedin_login(page):
                print("Auto-login submitted successfully. Checking session status...")
                page.wait_for_timeout(3000)
                if "feed" in page.url:
                    print("Successfully logged in via credentials!")
        except Exception as e:
            print(f"Auto-login check skipped/failed: {e}")
        
        # Wait for 300 seconds or until closed
        try:
            for i in range(300):
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nInterrupt received. Closing...")
        finally:
            context.close()
            print("Session saved successfully!")

if __name__ == "__main__":
    run()
