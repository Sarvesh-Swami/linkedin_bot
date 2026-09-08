"""
browser_agent.py
=================

A starter browser-automation agent.

WHAT THIS VERSION DOES
-----------------------
- Launches a real browser (Chromium, via Playwright).
- Executes a FIXED, sequential list of steps you provide (a "task list"):
  goto a URL, click something, type into a search box, press Enter,
  open a new tab, switch tabs, close a tab, scroll, wait, extract text,
  and take a screenshot.
- After every step, it takes a screenshot and (optionally) sends that
  screenshot to a locally running open-source vision model (via Ollama,
  e.g. "llava" or "llama3.2-vision") to get a plain-English description
  of what's on screen. That description is saved along with the rest of
  the step's log, so you get a "details" record of the whole run.
- Saves everything (step log + AI descriptions + extracted text) to a
  JSON file, plus a screenshot per step, in an output folder.

WHAT THIS VERSION DOES NOT DO YET
----------------------------------
It does not let the AI model *decide* what to click next on its own -
you tell it the steps. That's the natural next iteration: instead of
"click selector X", a step would say "click whatever looks like the
search icon", and the vision model would figure out where that is on
the screenshot. The code below is structured so that upgrade is a
small, contained change (see `analyze_screenshot_with_ai` and the
`ai_click` stub near the bottom).

INSTALL
-------
    pip install playwright requests
    playwright install chromium

    # Optional, only needed if you want AI screenshot descriptions:
    # 1. Install Ollama: https://ollama.com
    # 2. Pull a vision model, e.g.:
    #      ollama pull llava
    # 3. Make sure `ollama serve` is running (it usually runs automatically).

RUN
---
    python browser_agent.py --tasks example_task.json --output ./run_output

    # Without AI descriptions (faster, no Ollama needed):
    python browser_agent.py --tasks example_task.json --output ./run_output --no-ai

TASK FILE FORMAT
----------------
A JSON file containing a list of steps. See example_task.json for a
full working example. Supported actions:

    {"action": "goto",       "url": "https://example.com"}
    {"action": "wait",       "seconds": 2}
    {"action": "click",      "selector": "#some-button"}
    {"action": "type",       "selector": "input[name='q']", "text": "hello", "clear": true}
    {"action": "press",      "key": "Enter"}
    {"action": "search",     "selector": "input[name='q']", "query": "laptops"}
    {"action": "scroll",     "pixels": 800}
    {"action": "new_tab",    "url": "https://example.com/other"}
    {"action": "switch_tab", "index": 0}
    {"action": "close_tab",  "index": 1}          # "index" optional -> closes current tab
    {"action": "extract",    "selector": ".title", "save_as": "titles"}
    {"action": "screenshot", "name": "before_search"}
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    import requests
except ImportError:
    requests = None

try:
    from dotenv import load_dotenv
    load_dotenv()  # reads a .env file in the current directory, if present
except ImportError:
    pass  # dotenv is optional; you can also set env vars another way


# --------------------------------------------------------------------------
# Optional: open-source vision model integration (via Ollama, running locally)
# --------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llava"  # any local vision model you've pulled, e.g. "llama3.2-vision"


def analyze_screenshot_with_ai(image_path: str, question: str = None) -> str:
    """
    Send a screenshot to a locally running open-source vision model (via
    Ollama) and get back a text description. Returns "" on any failure so
    that a missing/unavailable model never crashes the run.

    This is the hook you'll extend later to let the AI choose actions
    (e.g. return element coordinates instead of a description).
    """
    if requests is None:
        return ""

    question = question or (
        "Describe what is visible on this webpage screenshot in 1-3 "
        "sentences: what page/section it looks like, and any key content, "
        "buttons, or forms you can see."
    )

    try:
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": question,
                "images": [image_b64],
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except Exception as e:
        return f"[AI description unavailable: {e}]"


# --------------------------------------------------------------------------
# The agent itself
# --------------------------------------------------------------------------

class BrowserAgent:
    def __init__(self, output_dir: str, headless: bool = False, use_ai: bool = True):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.shots_dir = self.output_dir / "screenshots"
        self.shots_dir.mkdir(exist_ok=True)

        self.headless = headless
        self.use_ai = use_ai

        self.log = []          # list of per-step records
        self.extracted = {}    # data saved via "extract" steps
        self.tabs = []         # open pages/tabs
        self.active_tab = 0

        self._playwright = None
        self.browser = None
        self.context = None

    # -- lifecycle -----------------------------------------------------

    def start(self):
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context()
        page = self.context.new_page()
        self.tabs = [page]
        self.active_tab = 0

    def close(self):
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()

    @property
    def page(self):
        return self.tabs[self.active_tab]

    # -- core actions ----------------------------------------------------

    def goto(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded")

    def wait(self, seconds: float):
        time.sleep(seconds)

    def click(self, selector: str, timeout: int = 10000):
        self.page.click(selector, timeout=timeout)

    def type_text(self, selector: str, text: str, clear: bool = True):
        if clear:
            self.page.fill(selector, "")
        self.page.type(selector, text, delay=30)

    def press_key(self, key: str):
        self.page.keyboard.press(key)

    def search(self, selector: str, query: str):
        """Fill a search box and press Enter."""
        self.page.fill(selector, query)
        self.page.press(selector, "Enter")

    def scroll(self, pixels: int = 600):
        self.page.mouse.wheel(0, pixels)

    def new_tab(self, url: str = None):
        page = self.context.new_page()
        if url:
            page.goto(url, wait_until="domcontentloaded")
        self.tabs.append(page)
        self.active_tab = len(self.tabs) - 1

    def switch_tab(self, index: int):
        if 0 <= index < len(self.tabs):
            self.active_tab = index
        else:
            raise IndexError(f"No tab at index {index} (have {len(self.tabs)} tabs)")

    def close_tab(self, index: int = None):
        idx = self.active_tab if index is None else index
        if not (0 <= idx < len(self.tabs)):
            raise IndexError(f"No tab at index {idx} (have {len(self.tabs)} tabs)")
        self.tabs[idx].close()
        del self.tabs[idx]
        if not self.tabs:
            # keep at least one tab open
            self.tabs.append(self.context.new_page())
        self.active_tab = min(self.active_tab, len(self.tabs) - 1)

    def extract(self, selector: str, save_as: str = None):
        elements = self.page.query_selector_all(selector)
        texts = [el.inner_text().strip() for el in elements]
        if save_as:
            self.extracted[save_as] = texts
        return texts

    def screenshot(self, name: str = None) -> str:
        name = name or f"step_{len(self.log)}"
        path = self.shots_dir / f"{name}.png"
        self.page.screenshot(path=str(path))
        return str(path)

    # -- LinkedIn-specific helpers -----------------------------------------

    def login_linkedin(self):
        """
        Logs into LinkedIn using LINKEDIN_EMAIL / LINKEDIN_PASSWORD from
        environment variables (loaded from a .env file). Credentials are
        never written into the task list or the log file.
        """
        email = os.environ.get("LINKEDIN_EMAIL")
        password = os.environ.get("LINKEDIN_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "LINKEDIN_EMAIL / LINKEDIN_PASSWORD not found in environment. "
                "Check that your .env file exists and python-dotenv is installed."
            )

        self.page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        self.page.fill("#username", email)
        self.page.fill("#password", password)
        self.page.click("button[type='submit']")
        # give LinkedIn a moment to redirect to the feed (or to a
        # checkpoint/2FA page, which pause_for_user can handle below)
        self.page.wait_for_load_state("domcontentloaded")

    def search_linkedin_jobs(self, query: str):
        """
        Uses LinkedIn's own jobs search box to search for a keyword.
        Assumes you're already on/near https://www.linkedin.com/jobs/.
        """
        search_selector = "input[aria-label='Search by title, skill, or company']"
        self.page.wait_for_selector(search_selector, timeout=15000)
        self.page.fill(search_selector, query)
        self.page.press(search_selector, "Enter")

    def pause_for_user(self, message: str = "Press Enter in this terminal to continue..."):
        """
        Blocks the script and waits for you to press Enter in the terminal.
        Use this if LinkedIn shows a security checkpoint, CAPTCHA, or 2FA
        prompt -- solve it yourself in the visible browser window, then
        come back to the terminal and press Enter to resume the script.
        This script does not attempt to solve or bypass any of those itself.
        """
        input(f"\n[PAUSED] {message}\n")

    # -- experimental: AI-guided click (next iteration starting point) ----

    def ai_click(self, instruction: str):
        """
        STUB for the next version of this agent: take a screenshot, ask the
        vision model to locate an element matching `instruction` (e.g.
        "the search icon in the top right"), and click it. Not implemented
        yet -- requires a model/prompt that returns pixel coordinates, and
        most local vision models need extra prompting or a grounding model
        (e.g. a dedicated UI-grounding model) to do this reliably.
        """
        raise NotImplementedError(
            "ai_click is a placeholder for the next iteration, where the AI "
            "picks the target element instead of you specifying a selector."
        )

    # -- task runner -------------------------------------------------------

    ACTIONS = {
        "goto": lambda self, s: self.goto(s["url"]),
        "wait": lambda self, s: self.wait(s.get("seconds", 1)),
        "click": lambda self, s: self.click(s["selector"], s.get("timeout", 10000)),
        "type": lambda self, s: self.type_text(s["selector"], s["text"], s.get("clear", True)),
        "press": lambda self, s: self.press_key(s["key"]),
        "search": lambda self, s: self.search(s["selector"], s["query"]),
        "scroll": lambda self, s: self.scroll(s.get("pixels", 600)),
        "new_tab": lambda self, s: self.new_tab(s.get("url")),
        "switch_tab": lambda self, s: self.switch_tab(s["index"]),
        "close_tab": lambda self, s: self.close_tab(s.get("index")),
        "extract": lambda self, s: self.extract(s["selector"], s.get("save_as")),
        "screenshot": lambda self, s: self.screenshot(s.get("name")),
        "login_linkedin": lambda self, s: self.login_linkedin(),
        "search_linkedin_jobs": lambda self, s: self.search_linkedin_jobs(s["query"]),
        "pause_for_user": lambda self, s: self.pause_for_user(s.get("message", "Press Enter in this terminal to continue...")),
    }

    def run_task_list(self, steps: list):
        for i, step in enumerate(steps):
            action = step.get("action")
            record = {"step": i, "action": action, "params": step, "status": "ok"}

            try:
                fn = self.ACTIONS.get(action)
                if fn is None:
                    raise ValueError(f"Unknown action: {action}")
                result = fn(self, step)
                if action == "extract":
                    record["extracted"] = result
            except Exception as e:
                record["status"] = "error"
                record["error"] = str(e)

            # always capture a screenshot + optional AI description after
            # each step, even on failure, so you can see what happened
            try:
                shot_path = self.screenshot(f"step_{i}_{action}")
                record["screenshot"] = shot_path
                if self.use_ai:
                    record["ai_description"] = analyze_screenshot_with_ai(shot_path)
            except Exception as e:
                record["screenshot_error"] = str(e)

            self.log.append(record)
            print(f"[{i}] {action} -> {record['status']}")

        self.save_results()

    def save_results(self):
        out = {
            "log": self.log,
            "extracted": self.extracted,
        }
        results_path = self.output_dir / "results.json"
        with open(results_path, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved results to {results_path}")


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run a fixed sequence of browser steps.")
    parser.add_argument("--tasks", required=True, help="Path to a JSON task-list file.")
    parser.add_argument("--output", default="./run_output", help="Output directory for logs/screenshots.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (no visible window).")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI screenshot descriptions (no Ollama needed).")
    args = parser.parse_args()

    with open(args.tasks) as f:
        steps = json.load(f)

    agent = BrowserAgent(output_dir=args.output, headless=args.headless, use_ai=not args.no_ai)
    agent.start()
    try:
        agent.run_task_list(steps)
    finally:
        agent.close()


if __name__ == "__main__":
    main()