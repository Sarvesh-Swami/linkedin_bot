# email_finder.py

Finds and **SMTP-verifies** corporate emails for LinkedIn profiles grouped by
company. **Only verified emails are saved** — no guesses are ever written to the
output.

## How it works (per person)

1. Parse first / middle / last name from the LinkedIn URL slug
   (`aniil-kumar-sharma-820996126` -> first `aniil`, middle `kumar`,
   last `sharma`; trailing random IDs and title tokens like `adv`/`dr`
   are stripped).
2. Work out every **mail-capable domain** for the company: the website
   domain plus common TLD swaps (`.com`, `.in`, `.co`, `.io`, `.net`,
   `.org`, `.co.in`, `.org.in`), keeping only the ones that actually have
   an MX record. (DNS only — works even where SMTP is blocked.)
3. Build every **name-pattern x domain** combination, most-likely first
   (`first.last`, `firstlast`, `flast`, `firstl`, `f.last`, `first`,
   `last`, `last.first`, middle-initial variants, etc.).
4. SMTP-probe each combination in order (RCPT TO — no mail is actually
   sent). **Stop at the first address the mail server confirms**, save it,
   and move to the next person.
5. If none of the combinations verify, save nothing for that person and
   move on. Catch-all domains (which accept everything) are detected and
   skipped so no unverifiable guess is ever saved.

## Requirements

- Python 3.8+
- `pip install dnspython --break-system-packages`
- **Outbound port 25 must be open.** This is the hard requirement. The
  script verifies every address over SMTP, so if your network blocks
  port 25 it cannot confirm anything and will (correctly) save nothing.
  It runs a preflight check at startup and aborts immediately with a
  clear message if port 25 is unreachable, instead of grinding through
  guaranteed timeouts.

  Home wifi, many office networks, and most cloud sandboxes block port 25
  by default. Run from a VPS/cloud box where you've had port 25 unblocked,
  or replace the SMTP step with a paid verification API (Hunter,
  ZeroBounce, NeverBounce, etc.).

## Usage

```bash
pip install dnspython --break-system-packages

python email_finder.py people.json clean_data.json -o results.json

# See MX lookups / catch-all checks in the console too:
python email_finder.py people.json clean_data.json -o results.json -v

# Be gentler on mail servers (avoid spam-probe flagging):
python email_finder.py people.json clean_data.json -o results.json --delay 3

# Force it to try SMTP even if the preflight says port 25 is blocked:
python email_finder.py people.json clean_data.json -o results.json --skip-preflight
```

## Output shape

Only verified people appear under `employees`:

```json
[
  {
    "company_name": "Punekar Group",
    "website_domain": "punekargroup.com",
    "mail_domains": ["punekargroup.com"],
    "domain_status": "ok",
    "domain_note": "using website domain (has a valid MX record)",
    "employees": [
      {
        "profile_url": "https://www.linkedin.com/in/rohan-punekar/",
        "name_guess": "Rohan Punekar",
        "name_confidence": "high",
        "email": "rohan.punekar@punekargroup.com",
        "combinations_tried": 3
      }
    ]
  }
]
```

A company with zero verified people will have an empty `employees` list.

## Reading the logs

Every attempt is logged (and always written in full to `email_finder.log`):

```
COMPANY: Punekar Group
  mail domain(s): punekargroup.com  (using website domain (has a valid MX record))
[1/4] https://www.linkedin.com/in/rohan-punekar/
  16 name-pattern(s) x 1 domain(s) = 16 total combination(s) to try
  [1/16] trying rohan.punekar@punekargroup.com ...
  [1/16] rohan.punekar@punekargroup.com -> FAILED (REJECTED (550 ...)) -- next
  [2/16] trying rohanpunekar@punekargroup.com ...
  [2/16] rohanpunekar@punekargroup.com -> PASSED (ACCEPTED (250 OK))
  -> VERIFIED email: rohanpunekar@punekargroup.com
```

If every line reads `UNKNOWN (... port 25 blocked/filtered ...)`, that's the
network block, not the logic — see Requirements above.

## Responsible use

Rapid RCPT-TO probing can look like spam reconnaissance and get your sending
IP blocklisted. Keep `--delay` reasonable, and only probe domains you have a
legitimate reason to contac

# email_finder.py

Matches LinkedIn profiles to guessed & (optionally) SMTP-verified corporate emails.

## What it does

1. Reads your **profiles JSON** (`company_name` + `profiles: [urls]`).
2. Reads your **company-info JSON** (`company_name` + `website`) and pulls each
   company's domain from the `website` field.
3. For every profile URL, guesses the person's first/last name from the
   LinkedIn slug (e.g. `abhishek-rai-3b6973209` → Abhishek Rai — the trailing
   id and title tokens like "adv"/"dr" are stripped automatically).
4. Generates a ranked list of standard email patterns for that name + domain
   (`first.last@`, `flast@`, `first@`, etc.).
5. (Optional) Probes the domain's real mail server over SMTP (`RCPT TO`,
   no email actually sent) to find which candidate is a real mailbox, and
   detects "catch-all" domains that accept anything.
6. Writes one combined JSON file with every company → domain → employees →
   guessed/verified email.

## Usage

```bash
pip install dnspython --break-system-packages

# Full run with SMTP verification (needs outbound port 25 access):
python3 email_finder.py profiles.json company_info.json -o results.json

# Skip SMTP verification entirely and just take the best-guess pattern
# (use this if your network blocks port 25, e.g. most home ISPs/cloud boxes):
python3 email_finder.py profiles.json company_info.json -o results.json --no-verify

# Slow down SMTP probing (be polite to mail servers, avoid spam-filter flags):
python3 email_finder.py profiles.json company_info.json -o results.json --delay 3
```

## Output shape

```json
[
  {
    "company_name": "Punekar Group",
    "domain": "punekargroup.com",
    "domain_status": "ok",
    "employees": [
      {
        "profile_url": "https://www.linkedin.com/in/rohan-punekar/",
        "name_guess": "Rohan Punekar",
        "name_confidence": "high",
        "email": "rohan.punekar@punekargroup.com",
        "verification_status": "valid",
        "candidates_tried": ["rohan.punekar@punekargroup.com", "..."]
      }
    ]
  }
]
```

`verification_status` values:

- `valid` — the mail server explicitly accepted this address.
- `catch_all_domain` — the domain accepts *any* address, so we can't
  confirm which candidate is real; `email` is our best guess only.
- `not_found` — every candidate was explicitly rejected or unconfirmable.
- `no_mx` — the domain has no mail server we could find.
- `no_domain` — we had no domain for this company at all.
- `not_verified` — you ran with `--no-verify`.

`name_confidence` is `"low"` when the LinkedIn slug had no hyphens
(e.g. `victorwanja`, `sampla`) — there's no reliable way to split those
into first/last name, so only a single "whole-slug@domain" candidate is
tried.

## Real-world caveats (please read)

- **Port 25 is often blocked.** Most home ISPs, corporate networks, and
  cloud/dev sandboxes block outbound port 25 to fight spam. If it's
  blocked, every SMTP probe times out and comes back "unknown" — this is
  expected, not a bug. Run the verification step from a host that has
  real SMTP egress (a VPS, your own mail server, etc.), or use
  `--no-verify` and manually verify the shortlist another way.
- **Greylisting / rate limiting**: some servers deliberately soft-fail
  the first attempt from an unknown sender. A single "unknown" result
  isn't proof an address is invalid.
- **Catch-all domains** will make every candidate look "accepted" — the
  script detects this per-domain and reports it honestly instead of
  claiming false confidence.
- **Be a good citizen.** Rapid-fire RCPT TO probing against one server
  can get flagged as spam recon and get your sending IP blocklisted.
  Use `--delay` generously if you're checking many people at the same
  company.
- **Name guessing is heuristic**, not certain — always spot-check
  `name_confidence: "low"` entries by hand.
