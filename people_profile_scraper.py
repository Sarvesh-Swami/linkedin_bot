#!/usr/bin/env python3
"""
browser_agent.py
=================

Logs into LinkedIn (you complete the login by hand), then for every
company in an input JSON file:

  1. Takes that company's `linkedin_url` (whatever page it points to --
     .../about/, .../posts/, or just the bare company URL) and turns it
     into that company's PEOPLE tab, e.g.:
         https://www.linkedin.com/company/auralearnai/about/
         -> https://www.linkedin.com/company/auralearnai/people/
  2. Navigates there and scrolls (LinkedIn lazily loads more people
     cards as you scroll) until the list of loaded cards stops growing.
  3. Extracts every person-card's profile URL directly from the card
     element -- LinkedIn links both the avatar and the name to the same
     `https://www.linkedin.com/in/...` URL, so we just collect every
     such link on the page and dedupe them.
  4. Appends {"company_name": ..., "profiles": [...]} to the output
     list, then moves on to the next company.

The output JSON is (re)written after every company, so nothing is lost
if the script is interrupted partway through a long list.

NOT included yet (next iteration): clicking a "Next page" control on
the People tab itself. For now we only scroll each company's People
page to its natural end.

INSTALL
-------
    pip install playwright
    playwright install --with-deps chromium

RUN
---
    python3 browser_agent.py companies.json
    python3 browser_agent.py companies.json --output-file people.json
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


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


# Every person-card links both its avatar and its name to this same
# prefix -- that's the one thing about these cards that isn't a hashed,
# auto-generated class name, so it's what we anchor on.
PERSON_PROFILE_URL_PREFIX = "https://www.linkedin.com/in/"


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

    def login_linkedin(self):
        """
        Navigates to the LinkedIn login page and then PAUSES so you can log
        in yourself in the visible browser window. Once you resume, checks
        whether you actually reached the feed.
        """
        self.logger.info("Navigating to LinkedIn login page...")
        self.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        self.pause_for_user(
            "Please log in to LinkedIn manually in the browser window "
            "(including any 2FA or security checkpoint), then press Enter "
            "here to continue."
        )

        try:
            self.page.wait_for_url("**/feed/**", timeout=8000)
            self.logger.info(f"Confirmed on the LinkedIn feed ({self.page.url}) -- login successful.")
        except Exception:
            self.logger.warning(
                f"Didn't detect /feed/ within the timeout; currently at "
                f"{self.page.url}. Continuing anyway."
            )

    # -- company People page -----------------------------------------------

    @staticmethod
    def _to_people_url(linkedin_url: str) -> str:
        """
        Turn any LinkedIn company URL into that company's People tab, e.g.:
            https://www.linkedin.com/company/auralearnai/about/
            -> https://www.linkedin.com/company/auralearnai/people/
        Works whether the input ends in /about/, /posts/, /jobs/, or
        nothing at all -- we just keep everything through the company
        slug and replace whatever comes after it.
        """
        trimmed = linkedin_url.strip().rstrip("/")
        parts = trimmed.split("/")
        if "company" in parts:
            company_idx = parts.index("company")
            base = "/".join(parts[: company_idx + 2])  # .../company/<slug>
        else:
            base = trimmed
        return base + "/people/"

    def open_company_people(self, linkedin_url: str) -> str:
        target = self._to_people_url(linkedin_url)
        self.logger.info(f"Navigating to company people page: {target}")
        self.page.goto(target, wait_until="domcontentloaded")
        self.logger.info(f"Now at {self.page.url}")
        return target

    @staticmethod
    def _clean_profile_url(href: str) -> str:
        """Strip query string / fragment so the same profile linked twice
        in one card (avatar + name) collapses to a single URL."""
        parsed = urlparse(href)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _extract_profile_urls_on_page(self):
        """Return the set of unique person-profile URLs anywhere in the
        current page's DOM."""
        anchors = self.page.query_selector_all(f"a[href^='{PERSON_PROFILE_URL_PREFIX}']")
        urls = set()
        for a in anchors:
            try:
                href = a.get_attribute("href")
            except Exception:
                href = None
            if href:
                urls.add(self._clean_profile_url(href))
        return urls

    def scrape_company_people(self, linkedin_url: str, max_rounds: int = 40, pause: float = 1.2):
        """
        Loads the given company's People page, scrolls to load every
        person-card LinkedIn lazily renders, and returns the sorted list
        of unique profile URLs found. Stops once the count of unique
        profiles found stops growing for two scrolls in a row, or after
        `max_rounds` scrolls -- whichever comes first.
        """
        self.open_company_people(linkedin_url)

        seen = set()
        last_count = -1
        stable_rounds = 0

        for round_num in range(1, max_rounds + 1):
            found = self._extract_profile_urls_on_page()
            new = found - seen
            seen |= new

            if new:
                self.logger.info(
                    f"Round {round_num}: {len(new)} new profile(s) found (total unique: {len(seen)})"
                )

            count = len(seen)
            if count == last_count:
                stable_rounds += 1
                if stable_rounds >= 2:
                    self.logger.info(
                        f"Profile list stable at {count} -- reached the end of this page's results."
                    )
                    break
            else:
                stable_rounds = 0
            last_count = count

            self.scroll(1400)
            time.sleep(pause)
        else:
            self.logger.info(f"Hit max_rounds={max_rounds} while scrolling -- stopping.")

        return sorted(seen)


# --------------------------------------------------------------------------
# input / output helpers
# --------------------------------------------------------------------------

def load_companies(input_file: str):
    data = json.loads(Path(input_file).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    return data


def save_results(output_file: str, results: list):
    Path(output_file).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Log into LinkedIn manually, then for each company in the input "
            "JSON, visit its People page and scrape every profile URL."
        )
    )
    parser.add_argument("input_file", help="Path to the JSON file of companies (each needs a linkedin_url).")
    parser.add_argument(
        "--output-file", default="people.json",
        help="Where to save the results (default: people.json in the current directory).",
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
        "--max-scroll-rounds", type=int, default=40,
        help="Safety cap on how many times to scroll each company's People page (default: 40).",
    )
    args = parser.parse_args()

    companies = load_companies(args.input_file)

    agent = BrowserAgent(headless=args.headless, channel=args.channel, verbose=args.verbose)
    results = []
    try:
        agent.start()
        agent.login_linkedin()

        for company in companies:
            name = company.get("company_name", "Unknown")
            linkedin_url = company.get("linkedin_url")
            if not linkedin_url:
                agent.logger.warning(f"Skipping {name!r} -- no linkedin_url in input.")
                continue

            agent.logger.info(f"--- {name} ---")
            profiles = agent.scrape_company_people(linkedin_url, max_rounds=args.max_scroll_rounds)
            results.append({"company_name": name, "profiles": profiles})

            # Save after every company so partial progress isn't lost if
            # the script gets interrupted partway through the list.
            save_results(args.output_file, results)

    except KeyboardInterrupt:
        agent.logger.info("Interrupted by user.")
    finally:
        agent.close()

    agent.logger.info(
        f"Done. Saved {len(results)} compan(ies) to {Path(args.output_file).resolve()}"
    )


if __name__ == "__main__":
    main()