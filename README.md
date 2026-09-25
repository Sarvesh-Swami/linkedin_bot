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

Copy the template and fill in **your own** LinkedIn account:

```bash
cp .env.example .env            # Windows PowerShell: copy .env.example .env
```

```
LINKEDIN_EMAIL=you@example.com
LINKEDIN_PASSWORD=your-password
LINKEDIN_ACCOUNT=your-profile-handle     # optional, see next section
```

`.env`, `session.json` (the saved browser session/cookies) and every scraped output file are listed in `.gitignore` — never commit them. Leaving the credentials blank is fine too: the bot then simply pauses and lets you sign in by hand.

## Sessions & accounts (one login per device)

`session.json` holds live LinkedIn cookies for whichever account signed in. It is **per device**, gitignored, and never shared:

- **First run (fresh clone / new machine):** there is no session file, so the login step signs you in yourself — credentials from your `.env` are auto-filled, and you only have to handle 2FA/CAPTCHA. On success the session is written to `session.json` and reused by every later run on that device, so you stay logged in until the session expires.
- **Every run after that:** the file is reused *only if it provably belongs to you*. Each saved session carries a `bot_meta` block recording the `LINKEDIN_EMAIL` and machine name that saved it, and the profile handle it is signed in as. A session file copied from someone else's machine/account (or one committed to a public repo, which has no `bot_meta` at all) is detected, deleted, and its cookies cleared — you get a fresh login instead of silently ending up in that person's account.
- Set `LINKEDIN_ACCOUNT` (your profile handle, or your full `linkedin.com/in/...` URL) to also assert *which* account a restored session must be; a mismatch triggers a fresh login.
- Deleting `session.json` at any time forces the next run to log in again. `--session-file <path>` on any script points it somewhere else entirely.
- Escape hatch: `LINKEDIN_ALLOW_FOREIGN_SESSION=1` in the environment skips the ownership check (only for deliberately moving *your own* session between *your own* devices).

> **If you ever had a `session.json` committed to a repo:** treat it as compromised — the cookies in it belong to whoever ran the bot. It is gitignored now, so remove it from the working tree and, if it is in history, rotate that account's password and purge it (`git rm --cached session.json`, then `git filter-repo`/BFG if needed).

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

The first script in a fresh run logs you into LinkedIn (credentials from `.env` are auto-filled; you only need to handle 2FA/CAPTCHA checkpoints, and if `.env` is missing entirely it pauses for a fully manual login). The resulting session is saved to `session.json` so later steps and later runs on this device can skip login until it expires — see **Sessions & accounts** above for how that file is kept per-device and never reused across accounts.

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
| `session.json` | `linkedin_login.py` on first login | **gitignored, per-device** LinkedIn cookies + a `bot_meta` ownership block, reused by later runs on this device |

## Project structure

```
main.py                       job search -> companies.txt
extract_company_details.py    companies.txt -> results.json
json_parser.py                results.json -> clean_data.json
people_profile_scraper.py     clean_data.json -> people.json
Email_finder.py               people.json + clean_data.json -> email_results.json
linkedin_login.py             shared login/session helper used by every scraper
                              (auto-login, session save/restore, per-device
                               session ownership checks)
pipeline.py                   Flask app that orchestrates + streams the above via SSE
templates/dashboard.html      pipeline dashboard UI
static/style.css              dashboard styling
.env.example                  template for your own .env (copy it, then edit)
.gitignore                    keeps .env, session.json and run output out of git
session.json                  created on first login -- gitignored, per device
```

## Responsible use

- Automated login and scraping is against LinkedIn's User Agreement; expect occasional security checkpoints and use your own judgment/risk tolerance.
- `Email_finder.py` SMTP-probes real mail servers (`RCPT TO`, nothing is sent) — keep `--delay` reasonable and only probe domains you have a legitimate reason to contact. See [email_finder_readme.md](email_finder_readme.md) for the full rationale.
- Never commit `.env` or `session.json` — both hold live credentials/session cookies.
