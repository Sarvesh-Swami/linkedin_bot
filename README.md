# LinkedIn Bot

A local Playwright-based pipeline that logs into LinkedIn, scrapes companies hiring for a given job search, pulls each company's About-page details and People-page profile URLs, and (optionally) finds SMTP-verified corporate emails for those people. A Flask dashboard orchestrates and streams the whole pipeline; every step also runs standalone from the CLI.

## Pipeline

```
main.py                       companies.txt
  (job search -> companies)        |
                                    v
extract_company_details.py    results.json
  (About-page scrape)              |
                                    v
json_parser.py                clean_data.json   (+ results_failed.json)
  (strip nav/footer noise,         |
   keep real company fields)       v
people_profile_scraper.py     people.json
  (People-page profile URLs)       |
                                    v
Email_finder.py                email_results.json
  (SMTP-verified emails,
   run separately from the
   web dashboard -- see below)
```

| Script | Input | Output | Needs LinkedIn login |
|---|---|---|---|
| [main.py](main.py) | job keywords / search URL | `companies.txt` | yes |
| [extract_company_details.py](extract_company_details.py) | `companies.txt` | `results.json` | yes |
| [json_parser.py](json_parser.py) | `results.json` | `clean_data.json`, `results_failed.json` | no |
| [people_profile_scraper.py](people_profile_scraper.py) | `clean_data.json` | `people.json` | yes |
| [Email_finder.py](Email_finder.py) | `people.json` + `clean_data.json` | `email_results.json` | no (SMTP only) |

`pipeline.py` runs the first four steps in order; the email finder is a separate, opt-in last step (it needs outbound SMTP, not a browser session) — see [email_finder_readme.md](email_finder_readme.md) for full details.

## Requirements

- Python 3.8+
- Google Chrome / Chromium (installed by Playwright below)
- A LinkedIn account (used for the automated login — see **Responsible use**)
- For the email finder: outbound **port 25** access (most home/office networks and cloud sandboxes block this — see [email_finder_readme.md](email_finder_readme.md))

## Setup

```bash
pip install -r requirements.txt
pip install dnspython --break-system-packages   # only needed for Email_finder.py

playwright install --with-deps chromium
```

Create a `.env` file in the project root with your LinkedIn credentials:

```
LINKEDIN_EMAIL=you@example.com
LINKEDIN_PASSWORD=your-password
```

`.env` and `session.json` (the saved browser session/cookies) are both listed in `.gitignore` — never commit either.

> **Note:** this repo's git history already has a `session.json` committed (live LinkedIn cookies). Treat that session as compromised — log out of it / rotate, and avoid re-committing a fresh one.

## Usage

### Option A — Web dashboard

```bash
python pipeline.py
# open http://localhost:5050
```

Configure job keywords (or a full jobs-search URL) and start the run. Each step streams its logs live; when a step pauses for manual LinkedIn login/2FA, use the dashboard's input box to send keystrokes back to that step.

### Option B — Run steps individually from the CLI

```bash
# 1. Scrape company names from a jobs search
python main.py --keywords "founder's office"
# or: python main.py --url "https://www.linkedin.com/jobs/search/?keywords=..."

# 2. Scrape each company's About page
python extract_company_details.py companies.txt --output results.json

# 3. Clean the raw scrape into structured records
python json_parser.py results.json clean_data.json results_failed.json

# 4. Scrape profile URLs from each company's People page
python people_profile_scraper.py clean_data.json --output-file people.json

# 5. (optional) Find SMTP-verified emails for those profiles
python Email_finder.py people.json clean_data.json -o email_results.json
```

Every script also supports `--headless`, `--channel` (e.g. `chrome` to use your installed browser instead of bundled Chromium), `--verbose`, and `--session-file`. Run any script with `--help` for the full flag list.

The first script in a fresh run pauses for you to complete LinkedIn login by hand (credentials from `.env` are auto-filled; you only need to handle 2FA/CAPTCHA checkpoints). The resulting session is saved to `session.json` so later steps and later runs can skip login until it expires.

## Output files

| File | Produced by | Contents |
|---|---|---|
| `companies.txt` | `main.py` | unique company names found in job search results |
| `results.json` | `extract_company_details.py` | raw scraped About-page data per company |
| `clean_data.json` | `json_parser.py` | cleaned company records (name, website, industry, size, HQ, description, ...) |
| `results_failed.json` | `json_parser.py` | companies whose scrape didn't land on a real About page |
| `people.json` | `people_profile_scraper.py` | `{company_name, profiles: [linkedin.com/in/... urls]}` per company |
| `email_results.json` | `Email_finder.py` | companies -> employees -> SMTP-verified (or guessed) email |
| `debug_page.html` | any scraper, on selector-miss | last page HTML, for diagnosing a LinkedIn markup change |
| `run_output/` | a prior manual run | saved snapshot of `companies.txt` / `results.json` / `run.log` |

## Project structure

```
main.py                       job search -> companies.txt
extract_company_details.py    companies.txt -> results.json
json_parser.py                results.json -> clean_data.json
people_profile_scraper.py     clean_data.json -> people.json
Email_finder.py               people.json + clean_data.json -> email_results.json
linkedin_login.py             shared login/session helper used by every scraper
pipeline.py                   Flask app that orchestrates + streams the above via SSE
templates/dashboard.html      pipeline dashboard UI
static/style.css              dashboard styling
```

## Responsible use

- Automated login and scraping is against LinkedIn's User Agreement; expect occasional security checkpoints and use your own judgment/risk tolerance.
- `Email_finder.py` SMTP-probes real mail servers (`RCPT TO`, nothing is sent) — keep `--delay` reasonable and only probe domains you have a legitimate reason to contact. See [email_finder_readme.md](email_finder_readme.md) for the full rationale.
- Never commit `.env` or `session.json` — both hold live credentials/session cookies.
