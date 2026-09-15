"""
linkedin_login.py
==================

Shared login helper used by all pipeline scripts.

- Loads credentials from .env (LINKEDIN_EMAIL / LINKEDIN_PASSWORD)
- Auto-fills the login form using stable selectors
- Saves browser session state (cookies + localStorage) to a JSON file
  so later pipeline steps can skip login entirely
- Loads a previously saved session to restore login state

USAGE (from any script):
    from linkedin_login import load_env_credentials, auto_login, save_session, load_session
"""

import os
import json
import time
from pathlib import Path

# Try to load .env automatically; if python-dotenv isn't installed,
# fall back to raw environment variables (they might already be set).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


# --------------------------------------------------------------------------
# Login form selectors
# --------------------------------------------------------------------------
# These target stable attributes (type / autocomplete) rather than the
# auto-generated id="«R77vvcjksop9h9j6»"-style ids React assigns via
# useId(), which change on every page load and can't be hardcoded.
#
# :visible matters here: LinkedIn's sign-in page can have more than one
# DOM node matching these attributes at once (e.g. alternate/responsive
# layouts, hidden helper elements for the Google/Apple SSO widgets), and
# only one copy is ever actually on screen.
EMAIL_INPUT_SELECTORS = [
    'input[autocomplete="username webauthn"]:visible',
    'input[type="email"]:visible',
    '#username:visible',
]
PASSWORD_INPUT_SELECTORS = [
    'input[autocomplete="current-password"]:visible',
    'input[type="password"]:visible',
    '#password:visible',
]

# Where the session file lives by default (next to this module).
DEFAULT_SESSION_FILE = str(Path(__file__).resolve().parent / "session.json")


# --------------------------------------------------------------------------
# Credential helpers
# --------------------------------------------------------------------------

def load_env_credentials():
    """Return (email, password) from environment / .env, or (None, None)."""
    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")
    return email, password


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

def _fill_login_form(page, email: str, password: str, logger=None) -> bool:
    """
    Fill the LinkedIn login form and click 'Sign in'.
    Returns True if all steps (email, password, click) succeeded.
    """
    if logger:
        logger.info("Auto-filling LinkedIn login credentials...")

    email_locator = page.locator(", ".join(EMAIL_INPUT_SELECTORS)).first
    password_locator = page.locator(", ".join(PASSWORD_INPUT_SELECTORS)).first

    try:
        email_locator.wait_for(state="visible", timeout=15000)
    except Exception:
        if logger:
            logger.warning("Could not find the email input field.")
        return False

    try:
        password_locator.wait_for(state="visible", timeout=8000)
    except Exception:
        if logger:
            logger.warning("Could not find the password input field.")
        return False

    try:
        email_locator.click()
        email_locator.fill("")
        email_locator.type(email, delay=30)

        password_locator.click()
        password_locator.fill("")
        password_locator.type(password, delay=30)
    except Exception as e:
        if logger:
            logger.warning(f"Failed to fill login form: {e}")
        return False

    # Click the Sign in button (use role to avoid hitting the heading)
    try:
        sign_in_button = page.get_by_role("button", name="Sign in", exact=True).first
        sign_in_button.wait_for(state="visible", timeout=6000)
        sign_in_button.click()
    except Exception as e:
        if logger:
            logger.warning(f"Could not find/click the 'Sign in' button: {e}")
        return False

    if logger:
        logger.info("Submitted LinkedIn login form.")
    return True


def _is_on_feed(page) -> bool:
    """Check if the browser is currently on the LinkedIn feed."""
    url = page.url.lower()
    return "/feed" in url


def _needs_manual_intervention(page) -> bool:
    """
    Check if LinkedIn is showing a security checkpoint, CAPTCHA, or 2FA
    that requires manual intervention.
    """
    url = page.url.lower()
    checkpoint_indicators = [
        "checkpoint",
        "challenge",
        "captcha",
        "two-step-verification",
        "two-factor",
    ]
    return any(indicator in url for indicator in checkpoint_indicators)


def auto_login(page, logger=None, session_file: str = DEFAULT_SESSION_FILE):
    """
    Full auto-login flow:
      1. Navigate to LinkedIn login
      2. Fill credentials from .env
      3. Submit and wait for feed
      4. If 2FA/CAPTCHA appears, pause for manual intervention
      5. Save session on success

    Returns True if login succeeded (reached the feed).
    """
    email, password = load_env_credentials()
    if not email or not password:
        if logger:
            logger.error(
                "LINKEDIN_EMAIL / LINKEDIN_PASSWORD not found in environment. "
                "Check that your .env file exists."
            )
        return False

    if logger:
        logger.info("Navigating to LinkedIn login page...")
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

    # Fill and submit
    if not _fill_login_form(page, email, password, logger):
        if logger:
            logger.warning(
                "Auto-fill did not complete. Falling back to manual pause."
            )
        input("\n[PAUSED] Please log in manually in the browser window, then press Enter here.\n")

    # Wait for either the feed or a checkpoint
    if logger:
        logger.info("Waiting for LinkedIn to finish login redirect...")

    # Give LinkedIn up to 15 seconds to redirect to the feed
    for _ in range(30):
        time.sleep(0.5)
        if _is_on_feed(page):
            break
        if _needs_manual_intervention(page):
            if logger:
                logger.info(
                    "LinkedIn is showing a security checkpoint / 2FA / CAPTCHA. "
                    "Please solve it manually in the browser window."
                )
            input(
                "\n[PAUSED] Solve the checkpoint in the browser, then press Enter here to continue.\n"
            )
            # After manual intervention, wait a bit more for the feed
            try:
                page.wait_for_url("**/feed/**", timeout=15000)
            except Exception:
                pass
            break

    if _is_on_feed(page):
        if logger:
            logger.info(f"Login successful — on the feed ({page.url}).")
        save_session(page, session_file, logger)
        return True
    else:
        if logger:
            logger.warning(
                f"Did not land on the feed (currently at {page.url}). "
                "Continuing anyway — some pages may still work."
            )
        # Still save whatever session state we have
        save_session(page, session_file, logger)
        return False


# --------------------------------------------------------------------------
# Session persistence
# --------------------------------------------------------------------------

def save_session(page, session_file: str = DEFAULT_SESSION_FILE, logger=None):
    """Save the browser context's storage state (cookies + localStorage)."""
    try:
        state = page.context.storage_state()
        Path(session_file).write_text(json.dumps(state, indent=2), encoding="utf-8")
        if logger:
            logger.info(f"Session saved to {session_file}")
    except Exception as e:
        if logger:
            logger.warning(f"Could not save session: {e}")


def load_session(context_or_page, session_file: str = DEFAULT_SESSION_FILE, logger=None) -> bool:
    """
    Load a previously saved session into a browser context.
    Call this BEFORE navigating to any LinkedIn page.

    `context_or_page` can be either a BrowserContext or a Page.
    Returns True if the session file was loaded successfully.
    """
    path = Path(session_file)
    if not path.exists():
        if logger:
            logger.info(f"No session file found at {session_file} — will need to log in.")
        return False

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        # Add cookies from the saved state
        cookies = state.get("cookies", [])
        if cookies:
            context = context_or_page if hasattr(context_or_page, "add_cookies") else context_or_page.context
            context.add_cookies(cookies)
            if logger:
                logger.info(f"Loaded {len(cookies)} cookie(s) from {session_file}")
            return True
        else:
            if logger:
                logger.info("Session file had no cookies.")
            return False
    except Exception as e:
        if logger:
            logger.warning(f"Could not load session from {session_file}: {e}")
        return False


def login_or_restore(page, logger=None, session_file: str = DEFAULT_SESSION_FILE):
    """
    Try to restore a saved session first. If that fails or the session
    is expired, do a full auto-login.
    """
    if load_session(page, session_file, logger):
        # Quick check: navigate to the feed and see if we're still logged in
        if logger:
            logger.info("Testing restored session...")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")

        # Give it a moment to either load the feed or redirect to login
        time.sleep(2)
        if _is_on_feed(page):
            if logger:
                logger.info("Session restored — already logged in.")
            return True
        else:
            if logger:
                logger.info("Saved session expired — doing fresh login.")

    return auto_login(page, logger, session_file)
