"""Log in to X once and save the session for the browser collector.

Usage:
    python scripts/login_social.py x            # log in inside the opened window
    python scripts/login_social.py x --cookies  # if the login page stays blank:
                                                # transfer cookies from your normal Chrome

Automated access violates X's terms of service - use a throwaway
account, never your main one.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.x_browser import launch_chrome

LOGIN_URLS = {"x": "https://x.com/login"}
PROFILE_DIRS = {"x": ROOT / "data" / "browser-profiles" / "x"}


def _cookie_login(page) -> None:
    """Seed the profile with an existing session, skipping the login page.

    X's login page refuses to render in automated browsers; logging in on a
    normal browser and copying the session cookies sidesteps it entirely.
    The cookie values are taken on stdin only - they are never written to a
    tracked file, just into the gitignored browser profile.
    """
    print(
        "In your NORMAL Chrome: log in to x.com with the throwaway account, then open\n"
        "F12 -> Application -> Cookies -> https://x.com and copy these two values:"
    )
    auth_token = input("auth_token: ").strip()
    ct0 = input("ct0: ").strip()
    if not auth_token or not ct0:
        raise SystemExit("both cookies are required")
    page.goto("https://x.com", wait_until="domcontentloaded", timeout=60_000)
    page.context.add_cookies(
        [
            {"name": "auth_token", "value": auth_token, "domain": ".x.com", "path": "/", "httpOnly": True, "secure": True},
            {"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/", "secure": True},
        ]
    )
    page.reload(wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(5_000)
    input("If you can see your logged-in feed in the window, press Enter to save... ")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    use_cookies = "--cookies" in sys.argv[1:]
    platform = args[0] if args else ""
    if platform not in LOGIN_URLS:
        print(__doc__)
        return 2
    from playwright.sync_api import sync_playwright

    profile = PROFILE_DIRS[platform]
    profile.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = launch_chrome(pw, str(profile), headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if use_cookies:
            _cookie_login(page)
        else:
            page.goto(LOGIN_URLS[platform], timeout=60_000)
            input(
                f"Log in to {platform} in the opened window (throwaway account; "
                "solve any checkpoint), confirm you can see your feed, then press Enter here... "
            )
        # X's own post-login redirect interrupts goto(), so just wait and check.
        page.wait_for_timeout(4_000)
        if page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').count() == 0:
            print("WARNING: x.com still looks logged out - the session was NOT saved.")
            return 1
        print(f"session saved in {profile}")
    finally:
        ctx.close()
        pw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
