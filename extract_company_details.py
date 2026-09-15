#!/usr/bin/env python3
"""
browser_agent.py
=================

Logs into LinkedIn (email + password are auto-filled; you handle any 2FA
or security checkpoint by hand), pauses so you can inspect page elements,
then searches a list of company names one by one.

FLOW
----
1. Go to the LinkedIn login page and auto-fill the email and password
   fields, then click "Sign in".
2. PAUSE so you can manually solve any 2FA prompt / security checkpoint /
   CAPTCHA that LinkedIn shows after submitting the form (the script does
   not attempt to solve or bypass any of those itself).
3. Confirm the session actually reached the feed.
4. Pause for a long, fixed duration (default 999999s) so you can poke
   around the page / copy element HTML before anything else runs.
5. For each company name in the given file (one per line): type it into
   the search bar, submit, switch to the "Companies" results tab, click
   the first result, wait for the company page to load, then extract
   generic details (title, meta tags, headings, visible text) from it.
6. Write all collected details out to results.json (updated after every
   company, not just at the end).

SECURITY NOTE
-------------
LINKEDIN_EMAIL / LINKEDIN_PASSWORD below are stored in plaintext in this
file. Do not commit this file to version control or share it with those
values filled in. Before doing anything beyond local testing, replace the
two constants with:

    import os
    LINKEDIN_EMAIL = os.environ["LINKEDIN_EMAIL"]
    LINKEDIN_PASSWORD = os.environ["LINKEDIN_PASSWORD"]

and set the values in your shell / a local .env file instead. Also note
that automated login is against LinkedIn's user agreement and can trigger
extra security checkpoints or account restrictions -- use at your own
discretion and expect to occasionally need to intervene manually.

INSTALL
-------
    pip install playwright
    playwright install --with-deps chromium

RUN
---
    python3 browser_agent.py companies.txt
    python3 browser_agent.py companies.txt --pause-seconds 30
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from linkedin_login import (
    login_or_restore, save_session, load_session,
    DEFAULT_SESSION_FILE, EMAIL_INPUT_SELECTORS, PASSWORD_INPUT_SELECTORS,
)


# --------------------------------------------------------------------------
# Login credentials — loaded from .env via the shared linkedin_login module.
# The constants below are kept for backward compatibility with any code
# that still references them directly.
# --------------------------------------------------------------------------
import os
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

LINKEDIN_EMAIL = os.environ.get("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.environ.get("LINKEDIN_PASSWORD", "")

# --------------------------------------------------------------------------
# Search / navigation selectors
# --------------------------------------------------------------------------

# The global typeahead search box (top nav bar).
SEARCH_INPUT_SELECTORS = [
    'input[data-testid="typeahead-input"]',
    'input[placeholder="Search"]',
]

# A company result card in the search-results list -- anchor on
# `role="listitem"` (structural, not a hashed class) and then look for the
# company-profile link within it.
SEARCH_RESULT_ITEM_SELECTOR = 'div[role="listitem"]'
COMPANY_LINK_WITHIN_ITEM_SELECTOR = 'a[href*="/company/"]'

# --------------------------------------------------------------------------
# "About" tab selectors, in priority order
# --------------------------------------------------------------------------
# Note there is NO `:visible` here. `:visible` was the main reason the old
# code failed: it makes a non-matching selector indistinguishable from a
# matching-but-not-yet-painted one, so every attempt just timed out with no
# information. Now we match on structure only, then inspect visibility
# ourselves and log exactly what we found.
#
# Each entry is (label, selector):
#   1. The nav-scoped anchor -- the real tab strip (`<nav aria-label=
#      "Organization’s page navigation">`). Matched via the stable
#      `org-page-navigation__item-anchor` class and the href, not the
#      auto-generated `id="ember76"` which changes every render.
#   2. Same thing keyed off the nav landmark itself, in case the class
#      names churn.
#   3. The "Show all details" footer link in the Overview card
#      (`aria-label="See all details about <Company>"`) -- same
#      destination, different place on the page, and it is often rendered
#      before/instead of the tab strip on narrow viewports.
#   4/5. Progressively looser href matches as a safety net.
ABOUT_TAB_SELECTORS = [
    ("nav-anchor-class", 'a.org-page-navigation__item-anchor[href*="/about"]'),
    ("nav-landmark", 'nav[aria-label*="navigation"] a[href*="/about"]'),
    ("see-all-details", 'a[aria-label*="See all details"]'),
    ("module-card-footer", 'a.org-module-card__footer-hoverable[href*="/about"]'),
    ("href-endswith", 'a[href$="/about/"]'),
    ("href-contains", 'a[href*="/about"]'),
]

# Runs in the page and reports every anchor that could plausibly be the
# About link, plus enough page state to tell "not rendered yet" apart from
# "rendered but my selector is wrong".
ABOUT_DIAGNOSTIC_JS = """
() => {
  const anchors = [];
  document.querySelectorAll('a').forEach((a) => {
    const href = a.getAttribute('href') || '';
    const text = (a.innerText || a.textContent || '').trim();
    const label = a.getAttribute('aria-label') || '';
    const interesting =
      href.includes('/about') ||
      /^about$/i.test(text) ||
      label.toLowerCase().includes('see all details');
    if (!interesting) return;
    const r = a.getBoundingClientRect();
    const cs = getComputedStyle(a);
    anchors.push({
      href: href,
      text: text.slice(0, 60),
      id: a.id || null,
      aria_label: label || null,
      cls: String(a.className || '').slice(0, 140),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      display: cs.display,
      visibility: cs.visibility,
      in_nav: !!a.closest('nav'),
      nav_label: a.closest('nav') ? a.closest('nav').getAttribute('aria-label') : null
    });
  });
  return {
    url: location.href,
    ready_state: document.readyState,
    total_anchors: document.querySelectorAll('a').length,
    nav_labels: Array.from(document.querySelectorAll('nav'))
                     .map((n) => n.getAttribute('aria-label')),
    candidates: anchors.slice(0, 40)
  };
}
"""

# --------------------------------------------------------------------------
# Direct About-page URLs
# --------------------------------------------------------------------------
# Every company's About page lives at a completely predictable URL, so the
# whole search -> Companies tab -> click first result -> click About tab
# dance is unnecessary. We just navigate straight there.
#
# The one catch: the SLUG IS NOT THE COMPANY NAME. "Aura Learn" lives at
# /company/auralearn-in/, not /company/aura-learn/. Slugs are chosen by
# whoever made the page and often carry suffixes (-in, -india, -hq, -inc)
# or are shortened/misspelled. So:
#   * If a line in companies.txt is a URL or an obvious slug, we use it
#     verbatim -- zero guessing, zero failure modes.
#   * If it's a display name, we try a couple of mechanical guesses, and
#     if those 404 we fall back to LinkedIn search, but only to READ the
#     real slug out of the first result's href. We never click through --
#     reading an href can't miss the way a click on an SPA can.
COMPANY_ABOUT_URL = "https://www.linkedin.com/company/{slug}/about/"

# The per-company inspection pause has been removed -- collect_company()
# now runs straight through from one company to the next. The only pause
# left is the optional one at the very end of main(), controlled by
# --pause-seconds; pass --pause-seconds 0 to have the script exit as soon
# as it's finished.

# Signals that a /company/<slug>/ URL didn't resolve to a real page.
NOT_FOUND_MARKERS = [
    "/404",
    "linkedin.com/company/unavailable",
    "authwall",
]


def slug_from_text(entry: str) -> str:
    """
    Pulls a company slug out of whatever a line in companies.txt contains:
    a full URL, a bare slug, or a display name.

      "https://www.linkedin.com/company/auralearn-in/about/" -> "auralearn-in"
      "linkedin.com/company/auralearn-in"                    -> "auralearn-in"
      "auralearn-in"                                         -> "auralearn-in"
      "Aura Learn"                                           -> "aura-learn"  (a guess)
    """
    e = (entry or "").strip()
    if "/company/" in e:
        return e.partition("/company/")[2].split("?")[0].split("/")[0]
    if " " not in e:
        return e.strip("/")
    return re.sub(r"[^a-z0-9]+", "-", e.lower()).strip("-")


def slug_candidates(entry: str):
    """
    Returns the slug(s) worth trying for `entry`, best first.

    A URL or bare slug yields exactly one candidate -- it's not a guess, so
    there's nothing to fall back to. A multi-word display name yields the
    hyphenated form and the squashed form ("Aura Learn" -> "aura-learn",
    "auralearn"), which covers the common cases; anything more exotic
    (auralearn-in) is left to the search fallback rather than brute-forced.
    """
    e = (entry or "").strip()
    if "/company/" in e or " " not in e:
        return [slug_from_text(e)]

    hyphen = re.sub(r"[^a-z0-9]+", "-", e.lower()).strip("-")
    squashed = re.sub(r"[^a-z0-9]+", "", e.lower())
    out = [hyphen]
    if squashed and squashed != hyphen:
        out.append(squashed)
    return out


# --------------------------------------------------------------------------
# "Overview" / About-details section selectors, in priority order
# --------------------------------------------------------------------------
# The <h2> on this card carries a hashed class
# (`oozLHgTmSAYvDBxVVtkePLJhowDQrzxtxBpIA`) that is regenerated on every
# build, so it is useless as an anchor. What IS stable:
#   * the BEM-ish module classes `org-about-module__margin-bottom` and
#     `org-page-details-module__card-spacing` on the <section>,
#   * the fact that the card is an `artdeco-card` containing a <dl>
#     (definition list) of label/value pairs -- that dt/dd shape is the
#     actual data structure and is what we key on as a fallback.
ABOUT_SECTION_SELECTORS = [
    ("about-module", "section.org-about-module__margin-bottom"),
    ("page-details-card", "section.org-page-details-module__card-spacing"),
    ("artdeco-card-with-dl", "section.artdeco-card:has(dl)"),
    ("main-section-with-dl", "main section:has(dl)"),
    ("any-section-with-dl", "section:has(dl)"),
]

# --------------------------------------------------------------------------
# Visible-text extraction, run inside the page
# --------------------------------------------------------------------------
# There are only a handful of ways text actually reaches the user's screen,
# so this hits all of them for the given section:
#   1. dt/dd pairs        -> the labelled fields ("Industry" -> "Technology,
#                            Information and Internet"), kept in DOM order
#                            and grouped so one <dt> can own several <dd>s.
#   2. headings (h1-h6)   -> the card title ("Overview") and field labels.
#   3. paragraphs/list    -> <p>, <li>, and free-standing text nodes that
#      items                live outside the <dl> (e.g. the company blurb).
#   4. links              -> anchor text plus its resolved href (website
#                            links, "Show all…" links).
#   5. images             -> alt text, which is on-screen text for anyone
#                            using a screen reader / when images fail.
#   6. aria-label /       -> text that is announced but not rendered; kept
#      title attributes      separately so it never pollutes visible_text.
#   7. innerText          -> the browser's own render-aware flattening of
#                            the section, which is the closest thing to
#                            "exactly what the user sees", split into lines.
#
# Everything is filtered through isVisible(): display/visibility/opacity,
# aria-hidden, and a non-zero bounding box. textContent is deliberately NOT
# used as the primary source, because it happily returns text from
# display:none nodes that the user never sees.
VISIBLE_TEXT_JS = """
(root) => {
  const clean = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();

  const isVisible = (el) => {
    if (!el || el.nodeType !== 1) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (parseFloat(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const text = (el) => clean(el.innerText !== undefined ? el.innerText : el.textContent);

  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  };

  // ---- 1. dt/dd label-value pairs -------------------------------------
  const fields = [];
  root.querySelectorAll('dl').forEach((dl, dlIndex) => {
    let current = null;
    Array.from(dl.children).forEach((child) => {
      const tag = child.tagName.toLowerCase();
      if (tag === 'dt') {
        if (current && current.label) fields.push(current);
        const heading = child.querySelector('h1,h2,h3,h4,h5,h6');
        current = {
          dl_index: dlIndex,
          label: text(heading || child),
          label_tag: heading ? heading.tagName.toLowerCase() : tag,
          label_visible: isVisible(child),
          values: [],
          rect: rect(child)
        };
      } else if (tag === 'dd' && current) {
        const v = text(child);
        if (v) {
          current.values.push({
            text: v,
            visible: isVisible(child),
            link: child.querySelector('a') ? child.querySelector('a').href : null
          });
        }
      }
    });
    if (current && current.label) fields.push(current);
  });

  // ---- 2. headings ------------------------------------------------------
  const headings = [];
  root.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach((h) => {
    const t = text(h);
    if (t) headings.push({ tag: h.tagName.toLowerCase(), text: t, visible: isVisible(h) });
  });

  // ---- 3. paragraphs / list items outside the <dl> ----------------------
  const paragraphs = [];
  root.querySelectorAll('p, li, span').forEach((el) => {
    if (el.closest('dl')) return;             // already captured as a field
    if (el.querySelector('p, li, span')) return;  // keep leaf nodes only
    const t = text(el);
    if (t && isVisible(el)) paragraphs.push(t);
  });

  // ---- 4. links ---------------------------------------------------------
  const links = [];
  root.querySelectorAll('a').forEach((a) => {
    links.push({
      text: text(a),
      href: a.href || a.getAttribute('href'),
      aria_label: a.getAttribute('aria-label'),
      visible: isVisible(a)
    });
  });

  // ---- 5. image alt text ------------------------------------------------
  const images = [];
  root.querySelectorAll('img').forEach((img) => {
    const alt = clean(img.getAttribute('alt'));
    if (alt) images.push({ alt: alt, src: img.currentSrc || img.src || null });
  });

  // ---- 6. accessible-only text (announced, not necessarily rendered) ----
  const aria = [];
  root.querySelectorAll('[aria-label], [title]').forEach((el) => {
    const v = clean(el.getAttribute('aria-label') || el.getAttribute('title'));
    if (v) aria.push({ tag: el.tagName.toLowerCase(), value: v, visible: isVisible(el) });
  });

  // ---- 7. the browser's own render of the section ------------------------
  const raw = root.innerText || '';
  const lines = raw.split('\\n').map((l) => clean(l)).filter((l) => l.length > 0);

  return {
    section_visible: isVisible(root),
    section_rect: rect(root),
    section_classes: String(root.className || '').slice(0, 300),
    section_tag: root.tagName.toLowerCase(),
    heading: headings.length ? headings[0].text : null,
    fields: fields,
    headings: headings,
    paragraphs: paragraphs,
    links: links,
    images: images,
    aria_text: aria,
    visible_lines: lines,
    visible_text: lines.join('\\n'),
    char_count: raw.length,
    outer_html_snippet: root.outerHTML.slice(0, 4000)
  };
}
"""


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("browser_agent")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger


# --------------------------------------------------------------------------
# The agent itself
# --------------------------------------------------------------------------

class BrowserAgent:
    def __init__(self, headless: bool = False, channel: str = None, verbose: bool = False):
        self.logger = setup_logging(verbose=verbose)
        self.headless = headless
        self.channel = channel  # e.g. "chrome" to use installed Chrome, not bundled Chromium

        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    # -- lifecycle -----------------------------------------------------

    def start(self):
        self.logger.info(
            f"Starting browser (headless={self.headless}, "
            f"channel={self.channel or 'bundled chromium'}) -- fresh, logged-out profile."
        )
        self._playwright = sync_playwright().start()

        launch_kwargs = {"headless": self.headless, "args": ["--disable-ipv6"]}
        if self.channel:
            launch_kwargs["channel"] = self.channel

        self.browser = self._playwright.chromium.launch(**launch_kwargs)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

        self.logger.info("Browser started.")

    def close(self):
        self.logger.info("Closing browser.")
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()

    # -- login -----------------------------------------------------------

    def pause_for_user(self, message: str = "Press Enter in this terminal to continue..."):
        """
        Blocks the script and waits for you to press Enter in the terminal.
        Solve any login form / 2FA / security checkpoint / CAPTCHA by hand
        in the visible browser window, then come back here and press Enter.
        This script does not attempt to solve or bypass any of those itself.
        """
        self.logger.info(f"PAUSED: {message}")
        input(f"\n[PAUSED] {message}\n")
        self.logger.info("Resumed by user.")

    def _fill_login_credentials(self) -> bool:
        """
        Fills the LinkedIn login form using LINKEDIN_EMAIL / LINKEDIN_PASSWORD
        and clicks the "Sign in" button. Returns True if all three steps
        (email, password, click) succeeded.

        Uses Locators rather than ElementHandle/query_selector: a Locator
        re-resolves against the live DOM on every action, so if the visible
        input isn't there yet (still hydrating) it keeps retrying instead of
        latching onto whatever matched first and possibly being invisible.
        """
        self.logger.info("Filling in LinkedIn login credentials...")

        email_locator = self.page.locator(", ".join(EMAIL_INPUT_SELECTORS)).first
        password_locator = self.page.locator(", ".join(PASSWORD_INPUT_SELECTORS)).first

        try:
            email_locator.wait_for(state="visible", timeout=15000)
        except Exception:
            self.logger.warning("Could not find the email/phone input field.")
            return False

        try:
            password_locator.wait_for(state="visible", timeout=8000)
        except Exception:
            self.logger.warning("Could not find the password input field.")
            return False

        try:
            email_locator.click()
            email_locator.fill("")
            email_locator.type(LINKEDIN_EMAIL, delay=30)

            password_locator.click()
            password_locator.fill("")
            password_locator.type(LINKEDIN_PASSWORD, delay=30)
        except Exception as e:
            self.logger.warning(f"Failed to fill login form: {e}")
            return False

        # The page also shows a plain "Sign in" heading above the form, so
        # an exact-text match (get_by_text) could hit that instead of the
        # actual submit button. get_by_role targets the <button> specifically
        # by its accessible name, which the heading doesn't have a role for.
        try:
            sign_in_button = self.page.get_by_role("button", name="Sign in", exact=True).first
            sign_in_button.wait_for(state="visible", timeout=6000)
            sign_in_button.click()
        except Exception as e:
            self.logger.warning(f"Could not find/click the 'Sign in' button: {e}")
            return False

        self.logger.info("Submitted LinkedIn login form.")
        return True

    def login_linkedin(self, session_file: str = DEFAULT_SESSION_FILE):
        """
        Auto-login using credentials from .env, or restore a previously
        saved session.  Only pauses if LinkedIn shows a 2FA / CAPTCHA
        checkpoint that needs manual intervention.
        """
        login_or_restore(self.page, logger=self.logger, session_file=session_file)

    def pause_for_inspection(self, seconds: int = 0):
        """
        Sleeps for a fixed duration so you can poke around the page (open
        devtools, copy element HTML, etc.) without the script doing anything
        else. Press Ctrl+C in the terminal to cut this short.

        `seconds <= 0` returns immediately, which is the default now that
        the per-company pause is gone -- so the script finishes and exits on
        its own instead of hanging.
        """
        if seconds <= 0:
            self.logger.info("No inspection pause requested; continuing.")
            return

        self.logger.info(
            f"Pausing for {seconds} second(s) so you can inspect the page. "
            f"Press Ctrl+C in this terminal to stop waiting early."
        )
        time.sleep(seconds)
        self.logger.info("Resuming after pause.")

    # -- element-lookup helpers -------------------------------------------

    def _find_first(self, selectors, root=None):
        """Return (element, selector) for the first selector (from a list)
        that currently matches something. `root` can be the page or an
        element handle. Returns (None, None) if nothing matches."""
        scope = root or self.page
        for sel in selectors:
            try:
                el = scope.query_selector(sel)
                if el:
                    return el, sel
            except Exception:
                continue
        return None, None

    def _click_exact_text(self, text: str, timeout: int = 5000) -> bool:
        """Click the first visible element whose whole (trimmed) text
        content is exactly `text`. Returns True on success."""
        try:
            locator = self.page.get_by_text(text, exact=True).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.click()
            return True
        except Exception:
            return False

    def _log_about_diagnostics(self, when: str = ""):
        """
        Dumps everything relevant about the current page's About link(s) to
        the log: page URL/readyState, every <nav> landmark's aria-label,
        every anchor that looks About-ish (href, text, id, aria-label,
        classes, bounding box, computed display/visibility, and whether it
        sits inside a <nav>), plus a per-selector match count for each entry
        in ABOUT_TAB_SELECTORS.

        This is what tells you *why* a lookup failed: zero candidates means
        the page hadn't rendered the nav yet, whereas candidates present but
        with a zero-size rect means it rendered but is collapsed/hidden.
        """
        self.logger.info(f"--- About-tab diagnostics {when} ---")

        # More than one page in the context means a click opened a new tab
        # and `self.page` may be pointing at the stale one.
        try:
            pages = self.context.pages
            if len(pages) > 1:
                self.logger.warning(
                    f"Context has {len(pages)} pages open; self.page is "
                    f"{self.page.url!r}. Others: {[p.url for p in pages if p is not self.page]}"
                )
        except Exception as e:
            self.logger.debug(f"Could not enumerate context pages: {e}")

        try:
            info = self.page.evaluate(ABOUT_DIAGNOSTIC_JS)
        except Exception as e:
            self.logger.warning(f"Diagnostic JS failed to run: {e}")
            return

        self.logger.info(
            f"page url={info['url']!r} readyState={info['ready_state']!r} "
            f"total <a> on page={info['total_anchors']}"
        )
        self.logger.info(f"<nav> aria-labels present: {info['nav_labels']}")

        cands = info["candidates"]
        if not cands:
            self.logger.warning(
                "ZERO About-ish anchors in the DOM right now -- the company "
                "page nav has not rendered yet (or the page is gated/redirected)."
            )
        for i, c in enumerate(cands):
            self.logger.info(
                f"  candidate[{i}] href={c['href']!r} text={c['text']!r} "
                f"id={c['id']!r} aria_label={c['aria_label']!r} in_nav={c['in_nav']} "
                f"nav_label={c['nav_label']!r} rect={c['rect']} "
                f"display={c['display']} visibility={c['visibility']}"
            )
            self.logger.debug(f"  candidate[{i}] class={c['cls']!r}")

        for label, sel in ABOUT_TAB_SELECTORS:
            try:
                total = self.page.locator(sel).count()
                vis = self.page.locator(f"{sel} >> visible=true").count()
                self.logger.info(f"  selector[{label}] {sel!r} -> {total} match(es), {vis} visible")
            except Exception as e:
                self.logger.info(f"  selector[{label}] {sel!r} -> ERROR: {e}")

        self.logger.info("--- end diagnostics ---")

    def _try_click_locator(self, locator, label: str) -> bool:
        """
        Attempts to click `locator` three escalating ways, logging each:
          1. normal click (scrolled into view first -- the tab strip is a
             horizontally scrollable container, so the anchor can sit
             outside the scrollport),
          2. force click (skips the "is something covering it" check --
             LinkedIn's sticky global header and message-overlay bubble
             routinely sit on top of elements),
          3. a raw DOM .click() via JS (bypasses Playwright actionability
             entirely; works even if the element is transparent/covered).
        """
        try:
            locator.scroll_into_view_if_needed(timeout=3000)
        except Exception as e:
            self.logger.debug(f"[{label}] scroll_into_view_if_needed failed (continuing): {e}")

        try:
            locator.click(timeout=5000)
            self.logger.info(f"[{label}] normal click succeeded.")
            return True
        except Exception as e:
            self.logger.warning(f"[{label}] normal click failed: {type(e).__name__}: {e}")

        try:
            locator.click(timeout=5000, force=True)
            self.logger.info(f"[{label}] force click succeeded.")
            return True
        except Exception as e:
            self.logger.warning(f"[{label}] force click failed: {type(e).__name__}: {e}")

        try:
            locator.evaluate("el => el.click()")
            self.logger.info(f"[{label}] JS .click() dispatched.")
            return True
        except Exception as e:
            self.logger.warning(f"[{label}] JS click failed: {type(e).__name__}: {e}")

        return False

    def _click_about_tab(self, timeout: int = 25000) -> bool:
        """
        Clicks the "About" tab on a company page, with real logging at every
        step so a failure says what actually went wrong instead of just
        "could not find".

        Strategy:
          1. Poll (up to `timeout` ms) until ANY selector in
             ABOUT_TAB_SELECTORS matches something in the DOM. The old code
             waited on `:visible` with a 6s budget, which conflated "not
             rendered yet" with "wrong selector" and gave up long before
             LinkedIn's Ember app had painted the nav -- that is almost
             certainly what you were hitting.
          2. Walk the matches in priority order and try clicking each one
             three ways (normal / force / JS), logging every attempt.
          3. Verify the URL actually became a .../about/ URL afterwards --
             a click that "succeeds" but routes nowhere is still a failure.
          4. If every click fails, fall back to navigating straight to
             <company-url>/about/, which is where the tab points anyway.
        """
        self.logger.info("Looking for the company 'About' tab...")

        deadline = time.monotonic() + (timeout / 1000.0)
        found_label, found_sel = None, None
        polls = 0

        while time.monotonic() < deadline:
            polls += 1
            for label, sel in ABOUT_TAB_SELECTORS:
                try:
                    if self.page.locator(sel).count() > 0:
                        found_label, found_sel = label, sel
                        break
                except Exception as e:
                    self.logger.debug(f"selector[{label}] raised while polling: {e}")
            if found_sel:
                break
            time.sleep(0.25)

        waited = round(timeout / 1000.0 - max(0.0, deadline - time.monotonic()), 1)
        if not found_sel:
            self.logger.warning(
                f"No About selector matched after {waited}s / {polls} polls."
            )
            self._log_about_diagnostics(when="(nothing matched)")
        else:
            self.logger.info(
                f"First match after {waited}s / {polls} polls: "
                f"selector[{found_label}] {found_sel!r}"
            )
            self._log_about_diagnostics(when="(before clicking)")

        url_before = self.page.url

        # Try every selector, and every match within a selector -- e.g. the
        # nav anchor may be present but zero-size while the "Show all
        # details" card footer is perfectly clickable.
        for label, sel in ABOUT_TAB_SELECTORS:
            try:
                count = self.page.locator(sel).count()
            except Exception as e:
                self.logger.warning(f"selector[{label}] count() failed: {e}")
                continue
            if count == 0:
                continue

            for i in range(count):
                loc = self.page.locator(sel).nth(i)
                try:
                    href = loc.get_attribute("href")
                    visible = loc.is_visible()
                except Exception:
                    href, visible = None, None
                tag = f"{label}#{i}"
                self.logger.info(f"[{tag}] trying href={href!r} visible={visible}")

                if not self._try_click_locator(loc, tag):
                    continue

                # A click only counts if it actually routed to /about/.
                try:
                    self.page.wait_for_url(lambda u: "/about" in u, timeout=8000)
                    self.logger.info(f"[{tag}] navigated to About: {self.page.url}")
                    return True
                except Exception:
                    self.logger.warning(
                        f"[{tag}] click registered but URL is still "
                        f"{self.page.url!r} (was {url_before!r}); trying next candidate."
                    )

        # Last resort: go directly to the About URL. This is the same
        # destination the tab links to, so nothing is lost except the click.
        try:
            base = self.page.url.split("?")[0].rstrip("/")
            # Strip any trailing sub-tab (posts/, jobs/, people/, ...) so we
            # build <.../company/slug>/about/ and not <.../posts>/about/.
            if "/company/" in base:
                head, _, tail = base.partition("/company/")
                slug = tail.split("/")[0]
                about_url = f"{head}/company/{slug}/about/"
                self.logger.warning(
                    f"All click attempts failed; navigating directly to {about_url}"
                )
                self.page.goto(about_url, wait_until="domcontentloaded")
                self.logger.info(f"Direct navigation landed at {self.page.url}")
                return True
        except Exception as e:
            self.logger.warning(f"Direct navigation to the About URL failed: {e}")

        self._log_about_diagnostics(when="(after all attempts failed)")
        return False

    # -- generic detail extraction ----------------------------------------

    def extract_generic_details(self, root_selector: str = None) -> dict:
        """
        Pulls generic, structurally-anchored details from the current page
        (or from a specific container if `root_selector` is given) and
        returns them as a dict. Designed to be resilient to hashed/obfuscated
        CSS class names -- it relies on tag semantics, ARIA roles, and meta
        tags rather than specific class names.

        Returns a dict like:
        {
            "url": "...",
            "title": "...",
            "meta_description": "...",
            "og_title": "...",
            "og_description": "...",
            "og_image": "...",
            "headings": ["...", "..."],
            "visible_text": "...",   # innerText of the root/body
        }
        """
        details = {
            "url": self.page.url,
            "title": None,
            "meta_description": None,
            "og_title": None,
            "og_description": None,
            "og_image": None,
            "headings": [],
            "visible_text": None,
        }

        try:
            details["title"] = self.page.title()
        except Exception:
            pass

        def _meta(name=None, prop=None):
            sel = f'meta[name="{name}"]' if name else f'meta[property="{prop}"]'
            try:
                el = self.page.query_selector(sel)
                return el.get_attribute("content") if el else None
            except Exception:
                return None

        details["meta_description"] = _meta(name="description")
        details["og_title"] = _meta(prop="og:title")
        details["og_description"] = _meta(prop="og:description")
        details["og_image"] = _meta(prop="og:image")

        # Locator to scope heading/text extraction to, if a selector was
        # given (e.g. a specific section on the profile page), otherwise
        # fall back to the whole <body>.
        #
        # NOTE: we deliberately use Playwright's Locator API here (not
        # query_selector / ElementHandle) because Page.inner_text() requires
        # a selector argument -- calling self.page.inner_text() with no
        # selector raises a TypeError, which the old code silently caught
        # and turned into `visible_text: null` on every single page. A
        # Locator (page.locator(...)) exposes a no-argument .inner_text()
        # that behaves consistently whether it's scoped to "body" or to a
        # specific root_selector.
        locator = self.page.locator(root_selector) if root_selector else self.page.locator("body")

        # Headings (h1-h3) give a decent "generic" outline of a page's content.
        try:
            heading_els = locator.locator("h1, h2, h3").all()
            details["headings"] = [
                (h.inner_text() or "").strip()
                for h in heading_els
                if (h.inner_text() or "").strip()
            ]
        except Exception:
            pass

        # Raw visible text as a fallback / catch-all.
        try:
            details["visible_text"] = (locator.inner_text() or "").strip()
        except Exception:
            pass

        self.logger.info(f"Extracted generic details from {details['url']}")
        return details

    # -- search a single company and open its result -----------------------

    def extract_about_sections(self, timeout: int = 15000) -> dict:
        """
        Extracts every on-screen piece of text from the company About page's
        detail card(s) -- the "Overview" section with its Industry /
        Company size / Website / Founded style dt-dd pairs -- and returns it
        as a JSON-ready dict with metadata.

        Works through ABOUT_SECTION_SELECTORS in priority order, waiting
        (polling) for one to appear, then runs VISIBLE_TEXT_JS inside the
        page against every matching <section>. Sections are de-duplicated by
        their rendered text so overlapping selectors (e.g. a section that
        matches both `org-about-module__margin-bottom` and
        `artdeco-card:has(dl)`) don't produce the same card twice.

        Returned shape:
        {
          "extracted_at": "2026-09-10T18:35:43",
          "url": "...", "page_title": "...", "company_slug": "auralearn-in",
          "viewport": {"width": 1280, "height": 720},
          "selector_used": "about-module",
          "section_count": 1,
          "fields": {"Industry": "Technology, Information and Internet",
                     "Company size": "0-1 employees"},
          "visible_text": "Overview\\nIndustry\\n...",
          "sections": [ {...full per-section detail...} ]
        }
        """
        about = {
            "extracted_at": datetime.now().isoformat(timespec="seconds"),
            "url": self.page.url,
            "page_title": None,
            "company_slug": None,
            "viewport": None,
            "selector_used": None,
            "selectors_matched": [],
            "section_count": 0,
            "fields": {},
            "visible_text": None,
            "visible_lines": [],
            "sections": [],
        }

        try:
            about["page_title"] = self.page.title()
        except Exception:
            pass
        try:
            about["viewport"] = self.page.viewport_size
        except Exception:
            pass
        if "/company/" in about["url"]:
            about["company_slug"] = about["url"].partition("/company/")[2].split("/")[0]

        # Poll until at least one section selector matches -- same reasoning
        # as the About tab: the card is rendered by Ember after the initial
        # paint, so a one-shot query can easily run too early.
        deadline = time.monotonic() + (timeout / 1000.0)
        matched = []
        polls = 0
        while time.monotonic() < deadline and not matched:
            polls += 1
            for label, sel in ABOUT_SECTION_SELECTORS:
                try:
                    n = self.page.locator(sel).count()
                except Exception as e:
                    self.logger.debug(f"section selector[{label}] raised: {e}")
                    continue
                if n > 0:
                    matched.append((label, sel, n))
            if not matched:
                time.sleep(0.25)

        if not matched:
            self.logger.warning(
                f"No Overview/About section matched any of "
                f"{[s for _, s in ABOUT_SECTION_SELECTORS]} after {polls} polls."
            )
            self._log_about_diagnostics(when="(no About section found)")
            return about

        about["selectors_matched"] = [
            {"label": l, "selector": s, "count": n} for l, s, n in matched
        ]
        about["selector_used"] = matched[0][0]
        for label, sel, n in matched:
            self.logger.info(f"section selector[{label}] {sel!r} -> {n} match(es)")

        seen_text = set()
        for label, sel, count in matched:
            for i in range(count):
                try:
                    data = self.page.locator(sel).nth(i).evaluate(VISIBLE_TEXT_JS)
                except Exception as e:
                    self.logger.warning(f"[{label}#{i}] extraction JS failed: {e}")
                    continue

                key = (data.get("visible_text") or "").strip()
                if not key:
                    self.logger.debug(f"[{label}#{i}] produced no visible text; skipping.")
                    continue
                if key in seen_text:
                    self.logger.debug(f"[{label}#{i}] duplicate of an earlier section; skipping.")
                    continue
                seen_text.add(key)

                data["matched_by"] = label
                data["matched_selector"] = sel
                data["match_index"] = i
                about["sections"].append(data)

                self.logger.info(
                    f"[{label}#{i}] heading={data.get('heading')!r} "
                    f"fields={len(data.get('fields', []))} "
                    f"lines={len(data.get('visible_lines', []))} "
                    f"chars={data.get('char_count')} visible={data.get('section_visible')}"
                )
                for f in data.get("fields", []):
                    vals = " | ".join(v["text"] for v in f.get("values", []))
                    self.logger.info(f"    {f['label']!r}: {vals!r}")

        about["section_count"] = len(about["sections"])

        # Flattened label -> value map across every section, for the common
        # case where you just want about["fields"]["Industry"].
        # Hidden dt/dd pairs stay in about["sections"] (they're useful when
        # debugging why something didn't show up) but are deliberately kept
        # OUT of this map: `fields` is meant to mirror what is actually on
        # screen, matching visible_text. LinkedIn ships display:none <dd>s
        # for fields the company left blank, and without this filter they'd
        # show up here as real values.
        hidden_skipped = 0
        for sec in about["sections"]:
            for f in sec.get("fields", []):
                label = f.get("label")
                values = [
                    v["text"] for v in f.get("values", [])
                    if v.get("text") and v.get("visible")
                ]
                if not label or not values or not f.get("label_visible"):
                    hidden_skipped += 1
                    continue
                about["fields"].setdefault(label, values[0] if len(values) == 1 else values)
        if hidden_skipped:
            self.logger.debug(f"Skipped {hidden_skipped} hidden/empty field(s) in the flat map.")

        all_lines = []
        for sec in about["sections"]:
            all_lines.extend(sec.get("visible_lines", []))
        about["visible_lines"] = all_lines
        about["visible_text"] = "\n".join(all_lines)

        self.logger.info(
            f"Captured {about['section_count']} section(s), "
            f"{len(about['fields'])} field(s), {len(all_lines)} visible line(s)."
        )
        return about

    def _scroll_through_page(self, steps: int = 8, pause: float = 0.35):
        """
        Scrolls to the bottom in increments, then back to the top.

        LinkedIn defers rendering some About-page modules until they're near
        the viewport, so a page that is never scrolled genuinely has less
        text in its DOM. innerText already includes below-the-fold content,
        so this isn't about visibility -- it's about forcing the lazy
        modules to exist at all before we read them.
        """
        try:
            for i in range(steps):
                self.page.evaluate("(i) => window.scrollTo(0, document.body.scrollHeight * (i / 8))", i + 1)
                time.sleep(pause)
            self.page.evaluate("() => window.scrollTo(0, 0)")
            time.sleep(pause)
        except Exception as e:
            self.logger.debug(f"Scroll pass failed (continuing): {e}")

    def _about_page_loaded(self, timeout: int = 12000) -> bool:
        """
        True if the current page looks like a real company About page.

        Checks, in order: the URL still points at /company/ and wasn't
        bounced to a 404 / authwall, then polls for any actual About content
        (a detail card, a <dl>, the org nav, or an <h1>). A slug that doesn't
        exist redirects rather than erroring, so the URL check alone isn't
        enough.
        """
        url = self.page.url
        for marker in NOT_FOUND_MARKERS:
            if marker in url:
                self.logger.warning(f"URL bounced to {url!r} (matched {marker!r}).")
                return False
        if "/company/" not in url:
            self.logger.warning(f"URL is no longer a company page: {url!r}")
            return False

        try:
            title = (self.page.title() or "").lower()
            if "page not found" in title or "not found" in title:
                self.logger.warning(f"Page title says not found: {title!r}")
                return False
        except Exception:
            pass

        probe = ", ".join(
            [sel for _, sel in ABOUT_SECTION_SELECTORS]
            + ['nav[aria-label*="navigation"]', "dl", "h1"]
        )
        deadline = time.monotonic() + (timeout / 1000.0)
        while time.monotonic() < deadline:
            try:
                if self.page.locator(probe).count() > 0:
                    return True
            except Exception:
                pass
            time.sleep(0.25)

        self.logger.warning(f"No About content appeared at {url!r} within {timeout}ms.")
        return False

    def _slug_via_search(self, company_name: str, pause: float = 1.5):
        """
        Falls back to LinkedIn search to discover a company's real slug when
        the guessed one didn't resolve.

        Deliberately does NOT click the result -- it reads the href off the
        first company link and returns the slug. Reading an attribute can't
        fail the way a click on an Ember SPA can, and it means we still end
        up navigating by URL like everything else.
        """
        self.logger.info(f"Falling back to search to find the slug for {company_name!r}...")

        try:
            self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        except Exception as e:
            self.logger.warning(f"Could not return to the feed before searching: {e}")

        search_box, _ = self._find_first(SEARCH_INPUT_SELECTORS)
        if not search_box:
            self.logger.warning("Search box not found; cannot resolve the slug.")
            return None

        try:
            search_box.click()
            search_box.fill("")
            search_box.type(company_name, delay=30)
            search_box.press("Enter")
        except Exception as e:
            self.logger.warning(f"Failed to submit search for {company_name!r}: {e}")
            return None

        time.sleep(pause)
        if not self._click_exact_text("Companies", timeout=6000):
            self.logger.info("No 'Companies' tab; reading company links off the all-results page.")

        time.sleep(pause)
        try:
            self.page.wait_for_selector(COMPANY_LINK_WITHIN_ITEM_SELECTOR, timeout=8000)
        except Exception:
            self.logger.warning(f"No company links in the results for {company_name!r}.")
            return None

        try:
            href = self.page.locator(COMPANY_LINK_WITHIN_ITEM_SELECTOR).first.get_attribute("href")
        except Exception as e:
            self.logger.warning(f"Could not read the first result's href: {e}")
            return None

        if not href or "/company/" not in href:
            self.logger.warning(f"First result href looks wrong: {href!r}")
            return None

        slug = slug_from_text(href)
        self.logger.info(f"Search resolved {company_name!r} -> slug {slug!r} (from {href!r})")
        return slug

    def open_company_about(self, entry: str, pause: float = 1.5) -> bool:
        """
        Navigates straight to a company's About page.

        Tries each candidate slug in turn, then the search fallback. Returns
        True once a real About page is loaded and scrolled.
        """
        tried = []

        for slug in slug_candidates(entry):
            url = COMPANY_ABOUT_URL.format(slug=slug)
            tried.append(slug)
            self.logger.info(f"Navigating directly to {url}")
            try:
                self.page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                self.logger.warning(f"Navigation to {url} failed: {e}")
                continue

            if self._about_page_loaded():
                self.logger.info(f"About page loaded for slug {slug!r} ({self.page.url})")
                self._scroll_through_page()
                return True
            self.logger.warning(f"Slug {slug!r} did not resolve to an About page.")

        self.logger.info(f"Guessed slug(s) {tried} didn't work for {entry!r}.")
        slug = self._slug_via_search(entry, pause=pause)
        if not slug:
            return False
        if slug in tried:
            self.logger.warning(f"Search returned the same slug {slug!r} that already failed.")
            return False

        url = COMPANY_ABOUT_URL.format(slug=slug)
        self.logger.info(f"Navigating to the searched slug: {url}")
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            self.logger.warning(f"Navigation to {url} failed: {e}")
            return False

        if self._about_page_loaded():
            self._scroll_through_page()
            return True
        return False

    def extract_about_page(self, timeout: int = 15000) -> dict:
        """
        Captures everything visible on the About page.

        Two layers, because they answer different questions:
          * page-level  -- innerText of <main> (falling back to <body>),
            which is literally what a person reading the screen sees, in
            reading order, however much or little that happens to be.
          * section-level -- the per-card breakdown from
            extract_about_sections(), which turns dt/dd pairs into labelled
            fields where that structure exists.

        Nothing here assumes a fixed set of fields or a minimum amount of
        text. A company with only Industry and Company size produces two
        fields; one with a description, website, specialties and locations
        produces all of them. Anything the company never filled in simply
        isn't in the DOM and so isn't in the output.
        """
        about = self.extract_about_sections(timeout=timeout)

        root_sel = None
        for candidate in ("main", "body"):
            try:
                if self.page.locator(candidate).count() > 0:
                    root_sel = candidate
                    break
            except Exception:
                continue

        about["page_root_selector"] = root_sel
        about["page_visible_text"] = None
        about["page_visible_lines"] = []

        if root_sel:
            try:
                page_data = self.page.locator(root_sel).first.evaluate(VISIBLE_TEXT_JS)
                about["page_visible_text"] = page_data.get("visible_text")
                about["page_visible_lines"] = page_data.get("visible_lines", [])
                about["page_links"] = page_data.get("links", [])
                about["page_headings"] = page_data.get("headings", [])
                about["page_images"] = page_data.get("images", [])
                self.logger.info(
                    f"Page-level capture from <{root_sel}>: "
                    f"{len(about['page_visible_lines'])} visible line(s), "
                    f"{page_data.get('char_count')} chars."
                )
            except Exception as e:
                self.logger.warning(f"Page-level text capture failed: {e}")

        # If the detail cards weren't found at all, the page text is still a
        # complete record of what was on screen -- so this is a partial
        # result, not a failure.
        if about.get("section_count", 0) == 0 and about.get("page_visible_text"):
            self.logger.info(
                "No structured detail cards found, but page-level visible text was captured."
            )
        return about

    def collect_company(self, entry: str, pause: float = 1.5, on_details=None):
        """
        Full per-company flow, URL-first: navigate to the About page,
        capture everything visible, hand the result to `on_details` (so it
        reaches disk), then move straight on to the next company.
        """
        self.logger.info(f"=== {entry!r} ===")

        if not self.open_company_about(entry, pause=pause):
            self.logger.warning(f"Could not open an About page for {entry!r}; skipping.")
            return None

        details = self.extract_generic_details()
        details["company_input"] = entry
        details["company_name_searched"] = entry
        details["about"] = self.extract_about_page()

        if on_details is not None:
            try:
                on_details(details)
            except Exception as e:
                self.logger.warning(f"on_details callback failed: {e}")

        return details

    def search_company(self, company_name: str, pause: float = 1.5, on_details=None):
        """
        `on_details`, if given, is called with the details dict the moment
        extraction finishes and BEFORE the long inspection pause -- that is
        what lets process_companies() flush results.json to disk while the
        script is still parked on the About page.

        Types `company_name` into the top-nav search box, submits the
        search, switches to the 'Companies' results tab, clicks the first
        company result, waits for that company page to load, and extracts
        generic details from it.

        Returns a details dict (from extract_generic_details) on success,
        or None if any step along the way failed.
        """
        self.logger.info(f"Searching for company: {company_name!r}")

        search_box, used_sel = self._find_first(SEARCH_INPUT_SELECTORS)
        if not search_box:
            self.logger.warning("Could not find the search box.")
            return None

        try:
            search_box.click()
            search_box.fill("")
            search_box.type(company_name, delay=30)
            search_box.press("Enter")
        except Exception as e:
            self.logger.warning(f"Failed to type/submit search for {company_name!r}: {e}")
            return None

        time.sleep(pause)

        if not self._click_exact_text("Companies", timeout=6000):
            self.logger.warning(f"Could not find/click the 'Companies' tab for {company_name!r}.")
            return None

        time.sleep(pause)

        try:
            self.page.wait_for_selector(SEARCH_RESULT_ITEM_SELECTOR, timeout=6000)
        except Exception:
            self.logger.warning(f"No result items appeared for {company_name!r}.")
            return None

        item = self.page.query_selector(SEARCH_RESULT_ITEM_SELECTOR)
        if not item:
            self.logger.warning(f"No result items appeared for {company_name!r}.")
            return None

        link = item.query_selector(COMPANY_LINK_WITHIN_ITEM_SELECTOR)
        if not link:
            self.logger.warning(f"First result for {company_name!r} had no company link.")
            return None

        try:
            link.click()
        except Exception as e:
            self.logger.warning(f"Failed to click first result for {company_name!r}: {e}")
            return None

        # Confirm we actually landed on a /company/ page before extracting.
        # LinkedIn is an Ember single-page app, so link.click() typically
        # triggers a client-side route swap rather than a full navigation --
        # wait_for_load_state("domcontentloaded") can resolve immediately
        # without the URL or content ever having changed, which is why
        # extraction was silently running against the old search-results
        # page. wait_for_url() correctly detects same-document (SPA) URL
        # changes as well as full navigations.
        try:
            self.page.wait_for_url(lambda url: "/company/" in url, timeout=10000)
        except Exception:
            self.logger.warning(
                f"Didn't detect navigation to a company page for {company_name!r}; "
                f"still at {self.page.url}. Extracting from current page anyway."
            )

        # Give the company page's content a moment to actually render
        # (the URL can update slightly before the DOM does).
        try:
            self.page.wait_for_selector("h1", timeout=8000)
        except Exception:
            pass  # extract_generic_details() is defensive on its own either way
        time.sleep(pause)

        self.logger.info(f"Opened first company result for {company_name!r} ({self.page.url}).")

        if self._click_about_tab():
            self.logger.info(f"Clicked the 'About' tab for {company_name!r} ({self.page.url}).")
        else:
            self.logger.warning(f"Could not find/click the 'About' tab for {company_name!r}.")

        # IMPORTANT ORDERING: extraction and the on-disk write now happen
        # BEFORE the long pause. Previously the 9999999s sleep sat between
        # the About click and extract_generic_details(), so the script never
        # reached the extraction step and results.json was never written --
        # you'd have had to Ctrl+C (which kills the run) to get anything out.
        details = self.extract_generic_details()
        details["company_name_searched"] = company_name
        details["about"] = self.extract_about_sections()

        if on_details is not None:
            try:
                on_details(details)
            except Exception as e:
                self.logger.warning(f"on_details callback failed: {e}")

        # (The long inspection pause that used to sit here has been removed;
        # this legacy search-based path now returns as soon as it has the
        # details, same as collect_company().)

        return details

    def save_results(self, results, output_path):
        """
        Writes `results` to `output_path` as JSON, overwriting whatever was
        there before. Called after every company (not just at the end) so
        results.json always reflects everything captured so far, even if
        the script is interrupted or crashes partway through.
        """
        try:
            Path(output_path).write_text(json.dumps(results, indent=2), encoding="utf-8")
        except Exception as e:
            self.logger.warning(f"Failed to write results to {output_path}: {e}")

    def process_companies(self, companies, pause: float = 1.5, output_path: str = None):
        """Runs search_company() for each name in `companies`, in order,
        and collects the extracted details. Returns a list of dicts, one
        per company that was successfully processed.

        If `output_path` is given, the results collected so far are saved
        (overwriting the file) after every single company, not just once
        at the end -- so you can open the file mid-run, and nothing is
        lost if the script is interrupted or crashes partway through.
        """
        results = []

        def _flush(details):
            """Called from inside search_company() right after extraction and
            before its inspection pause, so the file on disk is current even
            though the script is still sitting on the About page."""
            results.append(details)
            if output_path:
                self.save_results(results, output_path)
                self.logger.info(
                    f"Saved {len(results)} result(s) to {Path(output_path).resolve()} "
                    f"(written before the inspection pause)."
                )

        for name in companies:
            details = self.collect_company(name, pause=pause, on_details=_flush)
            if details:
                pass  # already appended + saved by _flush
            else:
                self.logger.warning(f"No details captured for {name!r}.")

            # No need to bounce off the feed between companies any more --
            # the next iteration navigates to an absolute URL regardless of
            # where the browser currently is.
            time.sleep(pause)
        return results


def load_companies_file(path: str):
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def main():
    parser = argparse.ArgumentParser(
        description="Log into LinkedIn manually."
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Not recommended -- you need a visible window to log in by hand.",
    )
    parser.add_argument(
        "--channel", default=None,
        help="Optional browser channel, e.g. 'chrome' to launch your installed Chrome instead of bundled Chromium.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG-level detail on the console.")
    parser.add_argument(
        "companies_file",
        help="Path to a text file with one company name per line.",
    )
    parser.add_argument(
        "--pause-seconds", type=int, default=0,
        help=(
            "How long to hold the browser open after all companies are done, "
            "for manual inspection (default: 0 = exit immediately)."
        ),
    )
    parser.add_argument(
        "--output", default="results.json",
        help="Path to write the extracted company details as JSON (default: results.json).",
    )
    parser.add_argument(
        "--session-file", default=DEFAULT_SESSION_FILE,
        help="Path to the saved LinkedIn session file (default: session.json).",
    )
    args = parser.parse_args()

    companies = load_companies_file(args.companies_file)

    agent = BrowserAgent(headless=args.headless, channel=args.channel, verbose=args.verbose)
    try:
        agent.start()
        agent.login_linkedin(session_file=getattr(args, 'session_file', DEFAULT_SESSION_FILE))
        results = agent.process_companies(companies, output_path=args.output)

        out_path = Path(args.output)
        agent.logger.info(f"Done. {len(results)} result(s) saved to {out_path.resolve()}")

        agent.pause_for_inspection(seconds=args.pause_seconds)
    except KeyboardInterrupt:
        agent.logger.info(
            "Interrupted by user -- results collected so far are already saved to "
            f"{Path(args.output).resolve()}."
        )
    finally:
        agent.close()


if __name__ == "__main__":
    main()