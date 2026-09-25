"""
linkedin_login.py
==================

Shared login helper used by all pipeline scripts.

- Loads credentials from .env (LINKEDIN_EMAIL / LINKEDIN_PASSWORD)
- Auto-fills the login form using stable selectors
- Saves browser session state (cookies + localStorage) to a JSON file
  so later pipeline steps can skip login entirely
- Loads a previously saved session to restore login state
- CHECKS THAT A SAVED SESSION IS YOURS before reusing it, so a session
  file that came from someone else's machine/account can never silently
  sign you into their LinkedIn account.

SESSION OWNERSHIP
-----------------
Every session file written by this module carries an extra `bot_meta`
block alongside Playwright's `cookies` / `origins`:

    "bot_meta": {
        "saved_by_email": "you@example.com",   # LINKEDIN_EMAIL at save time
        "machine":        "YOUR-PC",           # socket.gethostname()
        "saved_at":       "2026-09-25T18:00:00",
        "logged_in_as":   "your-profile-handle"   # from linkedin.com/in/<handle>/
    }

A saved session is only reused when that block matches *this* machine and
*this* .env account. A session file with no `bot_meta` at all (for example
one that was committed to a public repo) is treated as foreign, deleted,
and replaced by a fresh login. `session.json` is gitignored -- it holds
live cookies and is strictly per-device; never commit or share it.

Set LINKEDIN_ACCOUNT (your profile handle or /in/ URL) in .env to also
assert *which* account a restored session must belong to.

USAGE (from any script):
    from linkedin_login import load_env_credentials, auto_login, save_session, load_session
"""

import os
import json
import re
import socket
import time
from datetime import datetime
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
# Session ownership  (who does a saved session.json belong to?)
# --------------------------------------------------------------------------

def _now_iso() -> str:
    """Current local time as an ISO-8601 string (seconds precision)."""
    return datetime.now().isoformat(timespec="seconds")


def _normalise_handle(value):
    """
    Reduce a profile URL, a bare handle, or None to a comparable lowercase
    handle, e.g. 'https://www.linkedin.com/in/Yash-Shah/' -> 'yash-shah'.
    Returns None if there is nothing usable.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    match = re.search(r"/in/([^/?#]+)", text)
    if match:
        text = match.group(1)
    text = text.strip("/ \t\r\n")
    return text or None


def read_session_meta(session_file: str = DEFAULT_SESSION_FILE):
    """Return the `bot_meta` ownership block of a saved session, or None."""
    path = Path(session_file)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    meta = data.get("bot_meta")
    return meta if isinstance(meta, dict) else None


def session_belongs_to_this_user(session_file: str = DEFAULT_SESSION_FILE, logger=None) -> bool:
    """
    Decide whether an existing session file may be reused ON THIS MACHINE.

    Returns False (the caller must then log in fresh) when:
      * the file has no `bot_meta` block -- i.e. it was written by an older
        version, or downloaded/copied from somebody else, or
      * it was saved with a different LINKEDIN_EMAIL than the .env on this
        machine, or
      * it was saved on a different machine (hostname mismatch).

    Set LINKEDIN_ALLOW_FOREIGN_SESSION=1 to bypass this check (only useful if
    you deliberately copy your own session between your own devices).
    """
    if str(os.environ.get("LINKEDIN_ALLOW_FOREIGN_SESSION", "")).strip().lower() in {"1", "true", "yes"}:
        if logger:
            logger.warning(
                "LINKEDIN_ALLOW_FOREIGN_SESSION is set -- skipping the session "
                "ownership check (use only for your own sessions)."
            )
        return True

    meta = read_session_meta(session_file)
    if not meta:
        if logger:
            logger.warning(
                f"{session_file} has no ownership record (bot_meta) -- treating it as a "
                "session copied from another machine/account."
            )
        return False

    saved_email = str(meta.get("saved_by_email") or "").strip().lower()
    current_email = str(load_env_credentials()[0] or "").strip().lower()
    if saved_email and current_email and saved_email != current_email:
        if logger:
            logger.warning(
                f"{session_file} was saved for {saved_email} but .env says {current_email} "
                "-- refusing to reuse it."
            )
        return False

    saved_machine = str(meta.get("machine") or "").strip().lower()
    this_machine = socket.gethostname().strip().lower()
    if saved_machine and saved_machine != this_machine:
        if logger:
            logger.warning(
                f"{session_file} was created on machine {saved_machine!r} (this is "
                f"{this_machine!r}) -- refusing to reuse it."
            )
        return False

    return True


def expected_account_handle(logger=None):
    """
    The profile handle a restored session must belong to, from the optional
    LINKEDIN_ACCOUNT env var (a handle or a full /in/ URL). None if unset.
    """
    raw = str(os.environ.get("LINKEDIN_ACCOUNT") or "").strip()
    if not raw:
        return None
    if "@" in raw and "/in/" not in raw:
        if logger:
            logger.info(
                "LINKEDIN_ACCOUNT looks like an email address -- it must be your "
                "LinkedIn profile handle or /in/ URL. Ignoring it."
            )
        return None
    return _normalise_handle(raw)


def get_logged_in_handle(page, logger=None):
    """
    Best-effort lookup of the LinkedIn profile handle (/in/<handle>) of the
    account CURRENTLY signed in. Returns None when it can't be determined --
    callers should treat None as "unknown", not as "wrong account".
    """
    # 1) Cheapest: read the link straight out of the global nav of the page we
    #    are already on (no extra navigation needed). The "Me" menu container
    #    is checked first because it is guaranteed to be the signed-in member.
    try:
        handle = page.evaluate(
            """() => {
                const scopes = [
                    '.global-nav__me',
                    '[class*="global-nav__me"]',
                    'header',
                    'nav',
                    '[class*="global-nav"]'
                ];
                for (const sel of scopes) {
                    for (const scope of document.querySelectorAll(sel)) {
                        for (const a of scope.querySelectorAll('a[href*="/in/"]')) {
                            const m = (a.getAttribute('href') || '').match(/\\/in\\/([^\\/?#]+)/);
                            if (m && m[1] && m[1] !== 'me') return decodeURIComponent(m[1]);
                        }
                    }
                }
                return null;
            }"""
        )
        handle = _normalise_handle(handle)
        if handle:
            return handle
    except Exception:
        pass

    # 2) Fallback: /in/me/ redirects to the signed-in member's own profile.
    try:
        page.goto("https://www.linkedin.com/in/me/", wait_until="domcontentloaded", timeout=20000)
        handle = _normalise_handle(page.url)
        if handle and handle != "me":
            return handle
    except Exception as e:
        if logger:
            logger.info(f"Could not determine the signed-in profile handle: {e}")

    return None


def _session_account_matches(page, session_file: str = DEFAULT_SESSION_FILE, logger=None) -> bool:
    """
    Confirm that the account a just-restored session is signed in as is the
    account that session (and LINKEDIN_ACCOUNT, if set) claims. Returns False
    only when we positively read a DIFFERENT handle.
    """
    expected_from_env = expected_account_handle(logger)
    meta = read_session_meta(session_file) or {}
    expected_from_file = _normalise_handle(meta.get("logged_in_as"))

    if not expected_from_env and not expected_from_file:
        return True  # nothing to compare against

    actual = get_logged_in_handle(page, logger)
    if not actual:
        if logger:
            logger.info("Could not read the signed-in profile handle -- skipping the account check.")
        return True

    if logger:
        logger.info(f"Signed in as LinkedIn profile handle {actual!r}.")

    for label, wanted in (("LINKEDIN_ACCOUNT", expected_from_env),
                          ("the saved session file", expected_from_file)):
        if wanted and wanted != actual:
            if logger:
                logger.warning(
                    f"{label} says this session belongs to {wanted!r}, but LinkedIn is "
                    f"signed in as {actual!r}."
                )
            return False

    return True


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


def _has_auth_cookie(page) -> bool:
    """
    True if the browser context currently holds a LinkedIn auth cookie.
    `li_at` is the real auth cookie; `li_rm` is the "remember me" cookie
    LinkedIn uses to re-establish `li_at`. Either one means a login landed.
    """
    try:
        context = page if hasattr(page, "cookies") else page.context
        names = {c.get("name") for c in context.cookies() if c.get("value")}
    except Exception:
        return False
    return bool(names & {"li_at", "li_rm"})


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
      2. Fill credentials from .env (or pause for a purely manual login if
         there is no .env on this machine)
      3. Submit and wait for the feed / auth cookies
      4. If 2FA/CAPTCHA appears, pause for manual intervention
      5. Save the session on success

    Returns True if login succeeded (landed on the feed or got an auth cookie).
    """
    email, password = load_env_credentials()

    if logger:
        logger.info("Navigating to LinkedIn login page...")
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

    if not email or not password:
        # No credentials on this machine (e.g. a fresh clone with no .env):
        # don't fail -- just let whoever is at the keyboard sign in.
        if logger:
            logger.warning(
                "LINKEDIN_EMAIL / LINKEDIN_PASSWORD not found in the environment. "
                "Copy .env.example to .env and add your own LinkedIn account to have "
                "the form filled in automatically, or just log in by hand now."
            )
        input(
            "\n[PAUSED] No credentials in .env -- log into LinkedIn by hand in the "
            "browser window, then press Enter here to continue.\n"
        )
    elif not _fill_login_form(page, email, password, logger):
        if logger:
            logger.warning(
                "Auto-fill did not complete. Falling back to manual pause."
            )
        input("\n[PAUSED] Please log in manually in the browser window, then press Enter here.\n")

    # Wait for either the feed or a checkpoint
    if logger:
        logger.info("Waiting for LinkedIn to finish login redirect...")

    # Give LinkedIn up to 15 seconds to redirect to the feed / set auth cookies
    for _ in range(30):
        time.sleep(0.5)
        if _is_on_feed(page) or _has_auth_cookie(page):
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

    if _is_on_feed(page) or _has_auth_cookie(page):
        if logger:
            logger.info(f"Login successful ({page.url}).")
        save_session(page, session_file, logger)
        return True

    if logger:
        logger.warning(
            f"Did not land on the feed (currently at {page.url}). "
            "Continuing anyway -- some pages may still work."
        )
    # Do NOT save a session file here: an unauthenticated state is not worth
    # persisting, and writing it would make the next run "restore" nothing.
    return False


# --------------------------------------------------------------------------
# Session persistence
# --------------------------------------------------------------------------

def save_session(page, session_file: str = DEFAULT_SESSION_FILE, logger=None, handle=None):
    """
    Save the browser context's storage state (cookies + localStorage) together
    with a `bot_meta` ownership block, so this device can restore the session
    later without ever picking up somebody else's saved login.
    """
    try:
        state = page.context.storage_state()
        if handle is None:
            handle = get_logged_in_handle(page, logger)
        state["bot_meta"] = {
            "saved_by_email": (os.environ.get("LINKEDIN_EMAIL") or "").strip() or None,
            "machine": socket.gethostname(),
            "saved_at": _now_iso(),
            "logged_in_as": _normalise_handle(handle),
        }
        Path(session_file).write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if logger:
            who = state["bot_meta"]["logged_in_as"] or "unknown profile"
            logger.info(f"Session saved to {session_file} (signed in as {who}).")
    except Exception as e:
        if logger:
            logger.warning(f"Could not save session: {e}")


def clear_session(page=None, session_file: str = DEFAULT_SESSION_FILE, logger=None):
    """
    Delete a saved session file and, when a page/context is given, drop its
    cookies too -- so the next navigation cannot silently reuse the account
    that session belonged to.
    """
    try:
        path = Path(session_file)
        if path.exists():
            path.unlink()
            if logger:
                logger.info(f"Removed saved session file {session_file}.")
    except Exception as e:
        if logger:
            logger.warning(f"Could not delete {session_file}: {e}")

    if page is not None:
        try:
            context = page if hasattr(page, "clear_cookies") else page.context
            context.clear_cookies()
            if logger:
                logger.info("Cleared cookies from the discarded session.")
        except Exception as e:
            if logger:
                logger.warning(f"Could not clear cookies: {e}")


def load_session(context_or_page, session_file: str = DEFAULT_SESSION_FILE, logger=None) -> bool:
    """
    Load a previously saved session into a browser context.
    Call this BEFORE navigating to any LinkedIn page.

    `context_or_page` can be either a BrowserContext or a Page.
    Returns True if the session file was loaded successfully.

    NOTE: this only restores cookies. Whether the file is *allowed* to be
    reused on this machine/account is decided by session_belongs_to_this_user()
    / login_or_restore() -- use those rather than calling this directly.
    """
    path = Path(session_file)
    if not path.exists():
        if logger:
            logger.info(f"No session file found at {session_file} -- will need to log in.")
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
    Restore the saved session if -- and only if -- it belongs to this machine
    and this .env account; otherwise log in fresh.

    Flow:
      1. If a session file exists but came from another machine/account (or
         has no ownership record at all), delete it and clear its cookies so
         we can't accidentally browse LinkedIn as its owner.
      2. Otherwise restore it and confirm LinkedIn actually accepts it AND
         that the signed-in profile handle is the one the file claims.
      3. Anything else -> full login.
    """
    path = Path(session_file)

    if path.exists() and not session_belongs_to_this_user(session_file, logger):
        if logger:
            logger.warning(
                "Saved session does not belong to this device/account -- discarding it "
                "and logging in fresh."
            )
        clear_session(page, session_file, logger)

    if path.exists() and load_session(page, session_file, logger):
        # Quick check: navigate to the feed and see if we're still logged in
        if logger:
            logger.info("Testing restored session...")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")

        # Give it a moment to either load the feed or redirect to login
        time.sleep(2)

        if _is_on_feed(page):
            if _session_account_matches(page, session_file, logger):
                if logger:
                    logger.info("Session restored -- already logged in.")
                return True
            if logger:
                logger.warning(
                    "The restored session belongs to a different LinkedIn account -- "
                    "discarding it and logging in fresh."
                )
            clear_session(page, session_file, logger)
            return auto_login(page, logger, session_file)

        if logger:
            logger.info("Saved session expired -- doing fresh login.")
        clear_session(page, session_file, logger)

    return auto_login(page, logger, session_file)
