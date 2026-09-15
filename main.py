#!/usr/bin/env python3
"""
browser_agent.py
=================

Logs into LinkedIn (you complete the login by hand), then navigates
directly to a LinkedIn jobs *search-results* URL (no clicking through
the search box or the Jobs tab -- we just go straight there), and
scrolls through the results extracting each listing's company name,
saving every unique one to a text file.

FLOW
----
1. Go to the LinkedIn login page and PAUSE so you can log in by hand
   (credentials, 2FA, security checkpoints, etc.). The script never
   fills in, stores, or reads any LinkedIn credentials itself.
2. Confirm the session actually reached the feed.
3. Navigate directly to a jobs search-results URL, e.g.:
     https://www.linkedin.com/jobs/search/?keywords=founder%27s%20office&origin=SWITCH_SEARCH_VERTICAL
4. Scroll through the job results (LinkedIn loads more as you scroll),
   extract each listing's company name, and append new/unique ones to
   a text file as they're found.

No task-list JSON, no screenshots, no AI vision model, no clicking
through the search box / Jobs tab -- this goes straight to the results
URL and scrapes the real page elements directly.

A NOTE ON SELECTORS
--------------------
LinkedIn's CSS classes on these cards are auto-generated hashes (e.g.
"_7acc3727") that can change on every deploy, so this script deliberately
avoids matching on them. Instead it anchors on things much more likely
to stay stable:
  - the `componentkey` attribute on the job-card container
    (e.g. "job-card-component-ref-4461860947")
  - and, for title/company/location, simple ORDER within the card
    (the 1st, 2nd, and 3rd <p> elements) rather than their exact class.

If a run logs "No job cards appeared", it saves the current page's HTML
to debug_page.html so you can open it, find the real markup, and send
me the relevant snippet.

INSTALL
-------
    pip install playwright
    playwright install --with-deps chromium

RUN
---
    python3 browser_agent.py
    python3 browser_agent.py --keywords "founder's office"
    python3 browser_agent.py --url "https://www.linkedin.com/jobs/search/?keywords=founder%27s%20office&origin=SWITCH_SEARCH_VERTICAL"
"""

import argparse
import logging
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from linkedin_login import login_or_restore, save_session, load_session, DEFAULT_SESSION_FILE


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
# LinkedIn config
# --------------------------------------------------------------------------

DEFAULT_KEYWORDS = "founder's office"
JOBS_SEARCH_URL_TEMPLATE = "https://www.linkedin.com/jobs/search/?keywords={keywords}&origin=SWITCH_SEARCH_VERTICAL"

# LIST of candidates, tried in order until one matches. The first entry
# is confirmed from real page inspection (classic /jobs/search/ UI, card
# has both class "job-card-container" and a data-job-id attribute holding
# the literal job posting ID -- about as stable an anchor as you get).
# The rest are fallbacks for other UI variants LinkedIn may serve.
JOB_CARD_SELECTORS = [
    "div.job-card-container[data-job-id]",
    "div[componentkey^='job-card-component-ref-']",
    "li.jobs-search-results__list-item",
    "div.job-card-container",
    "div.base-search-card",
    "li.scaffold-layout__list-item",
]

# Specific, known selectors for the company name WITHIN a card -- tried
# first, before falling back to "2nd <p> in the card" (see
# _extract_job_info). ".artdeco-entity-lockup__subtitle" is confirmed
# from real page inspection to hold the company name on the classic UI.
# Add new ones here if a run's debug_page.html shows something different.
COMPANY_NAME_SELECTORS_WITHIN_CARD = [
    ".artdeco-entity-lockup__subtitle",
    ".job-card-container__primary-description",
    ".base-search-card__subtitle",
    "h4.base-search-card__subtitle",
]

# Pagination "Next" button, at the bottom of the results list.
NEXT_PAGE_BUTTON_SELECTORS = [
    "button[aria-label='Next']",
    "button.artdeco-pagination__button--next",
]


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

    def scroll(self, pixels: int = 1400):
        self.page.mouse.wheel(0, pixels)

    # -- login -----------------------------------------------------------

    def login_linkedin(self, session_file: str = DEFAULT_SESSION_FILE):
        """
        Auto-login using credentials from .env, or restore a previously
        saved session.  Only pauses if LinkedIn shows a 2FA / CAPTCHA
        checkpoint that needs manual intervention.
        """
        login_or_restore(self.page, logger=self.logger, session_file=session_file)

    # -- go straight to the jobs search results ---------------------------

    def open_jobs_search(self, keywords: str = DEFAULT_KEYWORDS, url: str = None):
        """
        Navigates directly to a LinkedIn jobs search-results URL -- no
        clicking through the search box or the Jobs tab. If `url` is
        given, it's used as-is; otherwise one is built from `keywords`
        using JOBS_SEARCH_URL_TEMPLATE.
        """
        target = url or JOBS_SEARCH_URL_TEMPLATE.format(keywords=quote(keywords))
        self.logger.info(f"Navigating to jobs search: {target}")
        self.page.goto(target, wait_until="domcontentloaded")
        self.logger.info(f"Now at {self.page.url}")

    # -- element-lookup helpers that try multiple fallback selectors -------

    def _find_first(self, selectors, root=None):
        """Return (element, selector) for the first selector (from a list)
        that currently matches something -- no waiting, just an instant
        check. `root` can be a page or an element handle (to search within
        a single job card, for example). Returns (None, None) if nothing
        matches right now."""
        scope = root or self.page
        for sel in selectors:
            try:
                el = scope.query_selector(sel)
                if el:
                    return el, sel
            except Exception:
                continue
        return None, None

    def _find_all_first(self, selectors, root=None):
        """Like _find_first, but returns ALL elements for the first
        selector that matches anything, plus that selector."""
        scope = root or self.page
        for sel in selectors:
            try:
                els = scope.query_selector_all(sel)
                if els:
                    return els, sel
            except Exception:
                continue
        return [], None

    def _wait_for_first(self, selectors, timeout_each: int = 4000):
        """Try waiting for each selector (in order) to become visible,
        giving each one up to `timeout_each` ms. Returns (element,
        selector) for whichever one shows up first, or (None, None) if
        none of them ever appear."""
        for sel in selectors:
            try:
                el = self.page.wait_for_selector(sel, timeout=timeout_each, state="visible")
                if el:
                    return el, sel
            except Exception:
                continue
        return None, None

    def _click_exact_text(self, text: str, timeout: int = 4000) -> bool:
        """Click the first visible element whose whole (trimmed) text
        content is exactly `text`. Returns True on success, False if no
        such element ever became visible."""
        try:
            locator = self.page.get_by_text(text, exact=True).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.click()
            return True
        except Exception:
            return False

    @staticmethod
    def _dedupe_repeated(text: str) -> str:
        """LinkedIn sometimes renders the same label twice inside one
        element (a visible copy plus an accessibility-only duplicate);
        collapse an exact A+A repeat down to a single A."""
        t = text.strip()
        n = len(t)
        if n > 0 and n % 2 == 0:
            half = n // 2
            if t[:half] == t[half:]:
                return t[:half].strip()
        return t

    # -- job-results scraping (scroll + extract) ----------------------------

    def _extract_job_info(self, card):
        """
        Return (title, company, location) for one job card. Tries known,
        specific company-name selectors first (COMPANY_NAME_SELECTORS_WITHIN_CARD).
        If none of those match -- e.g. this is the newer markup where class
        names are random hashes that change between deploys -- falls back
        to structural order: the first three <p> elements in a card are,
        in order, the job title, the company name, and the location.
        """
        el, used_sel = self._find_first(COMPANY_NAME_SELECTORS_WITHIN_CARD, root=card)
        if el:
            company = self._dedupe_repeated(" ".join(el.inner_text().split()))
            return None, company, None

        paragraphs = card.query_selector_all("p")
        texts = []
        for p in paragraphs[:3]:
            raw = " ".join(p.inner_text().split())
            texts.append(self._dedupe_repeated(raw))
        while len(texts) < 3:
            texts.append("")
        title, company, location = texts[0], texts[1], texts[2]
        return title, company, location

    def _current_first_card_id(self):
        """Return an identifier for whatever job card is currently first
        on the page (its data-job-id, or componentkey as a fallback), or
        None if no card is present. Used to detect whether clicking 'Next'
        actually moved us to a different page of results."""
        cards, _ = self._find_all_first(JOB_CARD_SELECTORS)
        if not cards:
            return None
        try:
            return cards[0].get_attribute("data-job-id") or cards[0].get_attribute("componentkey")
        except Exception:
            return None

    def _go_to_next_jobs_page(self) -> bool:
        """
        Click the pagination 'Next' button. Returns True if a click went
        through, False if there's no next page (button not found, or
        disabled).
        """
        btn, used_sel = self._find_first(NEXT_PAGE_BUTTON_SELECTORS)
        if btn:
            try:
                if btn.get_attribute("disabled") is not None or btn.get_attribute("aria-disabled") == "true":
                    return False
            except Exception:
                pass
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
                time.sleep(1.5)
                self.logger.debug(f"Clicked 'Next' via selector: {used_sel!r}")
                return True
            except Exception as e:
                self.logger.debug(f"Failed to click 'Next' button ({used_sel!r}): {e}")
                return False

        # Fall back to matching on the button's visible text.
        if self._click_exact_text("Next", timeout=3000):
            time.sleep(1.5)
            self.logger.debug("Clicked 'Next' via exact-text match.")
            return True

        return False

    def _scrape_current_page(self, output_path: Path, seen_companies: set, max_rounds: int = 40, pause: float = 1.2):
        """
        Scrolls the CURRENT page of job results to the bottom (LinkedIn
        loads more listings lazily as you scroll), extracts the company
        name from every newly-loaded card, and appends new/unique company
        names to `output_path` as they're found. Stops once the number of
        loaded cards stops growing for two scrolls in a row, or after
        `max_rounds` scrolls -- whichever comes first.
        """
        processed = 0

        self.logger.debug("Waiting for job cards to appear...")
        _, found_sel = self._wait_for_first(JOB_CARD_SELECTORS, timeout_each=8000)
        if not found_sel:
            debug_path = Path("debug_page.html")
            try:
                debug_path.write_text(self.page.content(), encoding="utf-8")
                debug_note = f"Saved the current page HTML to {debug_path.resolve()} for inspection."
            except Exception as e:
                debug_note = f"Also couldn't save debug HTML: {e}"
            self.logger.warning(
                f"No job cards appeared within the timeout at {self.page.url} -- "
                f"LinkedIn's markup may have changed. {debug_note} Continuing anyway."
            )

        last_count = -1
        stable_rounds = 0

        for round_num in range(1, max_rounds + 1):
            cards, _ = self._find_all_first(JOB_CARD_SELECTORS)
            count = len(cards)

            new_cards = cards[processed:count]
            new_companies = []
            for card in new_cards:
                _title, company, _location = self._extract_job_info(card)
                if company and company not in seen_companies:
                    seen_companies.add(company)
                    new_companies.append(company)

            if new_companies:
                with open(output_path, "a", encoding="utf-8") as f:
                    for c in new_companies:
                        f.write(c + "\n")

            if new_cards:
                processed = count
                self.logger.info(
                    f"Round {round_num}: {count} card(s) loaded so far, "
                    f"{len(new_companies)} new compan(ies) saved (total unique: {len(seen_companies)})"
                )

            if count == last_count:
                stable_rounds += 1
                if stable_rounds >= 2:
                    self.logger.info(f"Job list stable at {count} card(s) -- reached the end of this page's results.")
                    break
            else:
                stable_rounds = 0
            last_count = count

            self.scroll(1400)
            time.sleep(pause)
        else:
            self.logger.info(f"Hit max_rounds={max_rounds} while scrolling this page -- stopping.")

    def scrape_job_companies(
        self,
        output_file: str = "companies.txt",
        max_rounds: int = 40,
        max_pages: int = None,
        pause: float = 1.2,
    ):
        """
        Assumes you're on a jobs search-results page. For each page: scrolls
        to the bottom to load every listing, extracts every company name,
        and appends new/unique ones to `output_file` -- then clicks 'Next'
        and repeats on the next page. Keeps going until there's no 'Next'
        button (or it's disabled), the page stops actually changing (a
        safety net in case 'Next' is present but non-functional), or
        `max_pages` is reached (pass None for no limit).
        """
        output_path = Path(output_file)
        output_path.write_text("", encoding="utf-8")  # start this run's file fresh
        seen_companies = set()
        prev_first_id = None
        page_num = 1

        while True:
            self.logger.info(f"--- Scraping job results, page {page_num} ---")
            self._scrape_current_page(output_path, seen_companies, max_rounds=max_rounds, pause=pause)

            current_first_id = self._current_first_card_id()
            if page_num > 1 and current_first_id is not None and current_first_id == prev_first_id:
                self.logger.info(
                    "This page's results look identical to the previous page -- "
                    "stopping here to avoid looping."
                )
                break
            prev_first_id = current_first_id

            if max_pages and page_num >= max_pages:
                self.logger.info(f"Reached max_pages={max_pages} -- stopping.")
                break

            if not self._go_to_next_jobs_page():
                self.logger.info("No 'Next' button found (or it's disabled) -- reached the last page.")
                break

            page_num += 1

        self.logger.info(
            f"Done. Saved {len(seen_companies)} unique compan(ies) across {page_num} page(s) "
            f"to {output_path.resolve()}"
        )
        return sorted(seen_companies)


def main():
    parser = argparse.ArgumentParser(
        description="Log into LinkedIn manually, go straight to a jobs search URL, and scrape company names to a text file."
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
        "--output-file", default="companies.txt",
        help="Where to save the extracted company names (default: companies.txt in the current directory).",
    )
    parser.add_argument(
        "--max-scroll-rounds", type=int, default=40,
        help="Safety cap on how many times to scroll while loading job results, per page (default: 40).",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Safety cap on how many pages of results to walk through via 'Next' (default: no limit -- goes until the last page).",
    )
    parser.add_argument(
        "--keywords", default=DEFAULT_KEYWORDS,
        help=f"Keywords to search jobs for (default: {DEFAULT_KEYWORDS!r}). Ignored if --url is given.",
    )
    parser.add_argument(
        "--url", default=None,
        help="Full jobs search-results URL to use as-is, overriding --keywords.",
    )
    parser.add_argument(
        "--session-file", default=DEFAULT_SESSION_FILE,
        help="Path to the saved LinkedIn session file (default: session.json).",
    )
    args = parser.parse_args()

    session_file = getattr(args, 'session_file', DEFAULT_SESSION_FILE)

    agent = BrowserAgent(headless=args.headless, channel=args.channel, verbose=args.verbose)
    try:
        agent.start()
        agent.login_linkedin(session_file=session_file)
        agent.open_jobs_search(keywords=args.keywords, url=args.url)
        agent.scrape_job_companies(
            output_file=args.output_file,
            max_rounds=args.max_scroll_rounds,
            max_pages=args.max_pages,
        )
    except KeyboardInterrupt:
        agent.logger.info("Interrupted by user.")
    finally:
        agent.close()


if __name__ == "__main__":
    main()