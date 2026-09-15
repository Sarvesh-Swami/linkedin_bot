#!/usr/bin/env python3
"""
email_finder.py
================
Given:
  1. A "profiles" JSON file:
     [
       {"company_name": "AuraLearn AI", "profiles": ["https://www.linkedin.com/in/victorwanja/"]},
       ...
     ]
  2. A "company info" JSON file (used only to look up each company's domain):
     [
       {"company_name": "AuraLearn AI", "website": "https://www.auralearn.co.ke", ...},
       ...
     ]

This script will, for every profile of every company:
  - Guess the person's first/last name from the LinkedIn URL slug.
  - Generate a ranked list of standard corporate-email patterns
    (first.last@domain, flast@domain, etc.).
  - Test each candidate against the domain's real mail server via SMTP
    RCPT-TO probing (no email is actually sent) to see which one is
    accepted as a valid mailbox.
  - Detect "catch-all" domains (servers that accept ANY address) so we
    don't report false positives.

Output: a single JSON file with one object per company:
  {
    "company_name": "...",
    "website_domain": "...",   # domain taken from the company-info file's 'website'
    "domain": "...",           # domain we actually generated/checked emails against
    "domain_status": "ok" | "catch_all" | "no_mx" | "not_found",
    "domain_note": "...",      # explains whether/why the mail domain differs from website_domain
    "employees": [
      {
        "profile_url": "...",
        "name_guess": "First Last",
        "name_confidence": "high" | "low" | "none",
        "email": "first.last@domain.com" | null,
        "verification_status": "valid" | "catch_all_domain" | "unknown" | "not_found" | "no_domain",
        "candidates_tried": ["first.last@domain.com", "flast@domain.com", ...]
      },
      ...
    ]
  }

IMPORTANT CAVEATS (read before trusting the output):
  - SMTP verification requires outbound access on port 25. Many ISPs,
    corporate networks, and cloud sandboxes block this. A preflight check
    runs automatically at startup: if port 25 isn't reachable at all, the
    script logs a clear warning and switches to --no-verify mode itself
    rather than grinding through guaranteed timeouts for hours.
  - Some receiving servers greylist or silently drop unknown senders,
    so "unknown" doesn't always mean "invalid" -- it means "couldn't
    confirm."
  - Name-guessing from a URL slug is inherently lossy. Slugs with no
    hyphens (e.g. "victorwanja", "sampla") can't be reliably split into
    first/last name -- those are flagged with name_confidence="low".
  - The website domain and the real email domain are often NOT the same
    (e.g. site on .in, mail on .com). The script checks the website
    domain's MX record first, and if that domain has no mail server at
    all, automatically tries common TLD swaps (.com, .in, .co, .io, .net,
    .org, .co.in, .org.in) on the same base name and uses whichever one
    actually has a working MX record. This only needs DNS, so it works
    even when SMTP itself is blocked. See `domain_note` in the output for
    what happened for each company.
  - Use responsibly: hammering a mail server with many RCPT TO attempts
    can look like spam probing and get your sending IP blocklisted.
    The script rate-limits itself per domain (see --delay).
"""

import argparse
import json
import logging
import random
import re
import smtplib
import socket
import string
import sys
import threading
import time
from urllib.parse import urlparse

try:
    import dns.resolver
except ImportError:
    print("This script requires dnspython. Install with:\n"
          "    pip install dnspython --break-system-packages\n", file=sys.stderr)
    raise

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logger = logging.getLogger("email_finder")


def setup_logging(log_file: str, verbose: bool):
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        fileh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        fileh.setLevel(logging.DEBUG)  # log file always gets full detail
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
        logger.info(f"Logging full detail to: {log_file}")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

TITLE_TOKENS = {
    "dr", "er", "ca", "cs", "adv", "mr", "ms", "mrs", "prof", "eng", "ir", "cfa", "cpa"
}

# The address we claim to be sending FROM during the SMTP probe.
# Use a real, deliverable domain you control if you actually run this.
PROBE_FROM_ADDRESS = "verify@example.com"

SMTP_TIMEOUT_SECS = 6
DEFAULT_DELAY_SECS = 1.5  # politeness delay between SMTP attempts to the same domain

# Known-good, always-reachable mail servers used only to test whether THIS
# network can complete an outbound port-25 connection at all.
PREFLIGHT_TARGETS = ["aspmx.l.google.com", "gmail-smtp-in.l.google.com"]

# When a domain's own MX lookup fails (or as a secondary check even when it
# doesn't), also try these TLD variants of the same base name. Companies very
# often host their marketing site on one TLD (.in, .co, .io ...) and run
# email on a completely different one (usually .com).
ALT_TLDS_TO_TRY = ["com", "in", "co", "io", "net", "org", "co.in", "org.in"]


# --------------------------------------------------------------------------
# Preflight: is port 25 even usable on this network?
# --------------------------------------------------------------------------

def check_port25_connectivity():
    """Try to open a TCP connection (not a full SMTP session) to a couple of
    always-on public mail servers. Returns True if at least one succeeds
    within a few seconds, False otherwise. This tells us, once, up front,
    whether outbound port 25 is usable at all on this network -- so we don't
    burn hours re-discovering that fact one timed-out candidate at a time."""
    for host in PREFLIGHT_TARGETS:
        try:
            with socket.create_connection((host, 25), timeout=5):
                logger.debug(f"  preflight: connected to {host}:25 successfully")
                return True
        except Exception as e:
            logger.debug(f"  preflight: could not reach {host}:25 ({e})")
    return False


# --------------------------------------------------------------------------
# Step 1: name extraction from a LinkedIn profile URL
# --------------------------------------------------------------------------

def _looks_like_id(part: str) -> bool:
    """Heuristic: LinkedIn appends a random alphanumeric id to slugs when the
    plain name is taken, e.g. 'abhishek-rai-3b6973209'. Treat a hyphen-part
    as a random id (not a name) if it has 3+ digits, or is long and has any digit."""
    digits = sum(c.isdigit() for c in part)
    if digits >= 3:
        return True
    if len(part) >= 8 and digits >= 1:
        return True
    return False


def extract_name_from_url(url: str):
    """Return (first_name, last_name, middle_name, confidence) guessed from a
    LinkedIn profile URL's slug. confidence is 'high', 'low', or 'none'.
    middle_name is '' when there isn't a clear middle token."""
    try:
        path = urlparse(url).path.strip("/")
        slug = path.split("/")[-1] if path else ""
    except Exception:
        slug = ""

    if not slug:
        return "", "", "", "none"

    parts = [p for p in slug.split("-") if p]

    # Strip a trailing random-id segment, but never strip down to nothing.
    while len(parts) > 1 and _looks_like_id(parts[-1]):
        parts.pop()

    # Strip known professional-title tokens (adv, dr, er, ...).
    parts = [p for p in parts if p.lower() not in TITLE_TOKENS]

    if len(parts) >= 3:
        # first ... middle(s) ... last -- keep first, last, and one middle token
        first, middle, last = parts[0], parts[1], parts[-1]
        return first.lower(), last.lower(), middle.lower(), "high"
    elif len(parts) == 2:
        first, last = parts[0], parts[-1]
        return first.lower(), last.lower(), "", "high"
    elif len(parts) == 1:
        # No hyphens in the slug (e.g. "victorwanja", "sampla", "msam2606") --
        # we cannot reliably split this into first/last name.
        return parts[0].lower(), "", "", "low"
    else:
        return "", "", "", "none"


# --------------------------------------------------------------------------
# Step 2: domain lookup from the company-info file
# --------------------------------------------------------------------------

def extract_domain(website_url: str) -> str:
    """Turn 'https://www.auralearn.co.ke/some/path' into 'auralearn.co.ke'."""
    if not website_url:
        return ""
    netloc = urlparse(website_url).netloc or urlparse("//" + website_url).netloc
    netloc = netloc.split(":")[0]  # drop port if present
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.lower()


def build_company_domain_map(company_info_list):
    """company_name -> domain, from the second JSON file's 'website' field."""
    mapping = {}
    for entry in company_info_list:
        name = entry.get("company_name", "")
        domain = extract_domain(entry.get("website", ""))
        if name and domain:
            mapping[name] = domain
    return mapping


def _base_name_and_tld(domain: str):
    """Split 'rangmanchfarms.in' -> ('rangmanchfarms', 'in').
    Handles two-part TLDs like '.co.in' reasonably by treating everything
    after the first dot as the TLD."""
    parts = domain.split(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return domain, ""


def resolve_mail_domains(website_domain: str, mail_info: "DomainMailInfo"):
    """Return an ORDERED LIST of every candidate mail domain worth trying for
    this company, each one confirmed to have a real MX record. Website domain
    and mail domain are often different (site on .in, mail on .com, etc.), so
    we don't stop at the first hit -- we collect ALL variants that have mail
    servers, most-likely first, and the caller will try each in turn.

    Only does DNS lookups (no port 25 needed), so it works even on networks
    where SMTP itself is blocked.

    Returns (list_of_domains, note).
    """
    if not website_domain:
        return [], "no website domain provided"

    domains = []
    base, original_tld = _base_name_and_tld(website_domain)

    # Priority order: the domain we were given first, then the most common
    # email TLDs, then the rest.
    ordered_candidates = [website_domain]
    for tld in ALT_TLDS_TO_TRY:
        cand = f"{base}.{tld}"
        if cand not in ordered_candidates:
            ordered_candidates.append(cand)

    for cand in ordered_candidates:
        if mail_info.get_mx_host(cand):
            domains.append(cand)

    if not domains:
        return [], (f"no MX record found for '{website_domain}' or any TLD variant "
                    f"({', '.join(ordered_candidates[1:])})")

    if domains == [website_domain]:
        note = "using website domain (has a valid MX record)"
    else:
        note = f"mail-capable domain(s) found: {', '.join(domains)}"
    return domains, note


def resolve_mail_domain(website_domain: str, mail_info: "DomainMailInfo"):
    """Backwards-compatible single-domain helper (returns the top pick)."""
    domains, note = resolve_mail_domains(website_domain, mail_info)
    return (domains[0] if domains else None), note


# --------------------------------------------------------------------------
# Step 3: generate candidate emails (local-parts x domains)
# --------------------------------------------------------------------------

def generate_local_parts(first: str, last: str, middle: str = ""):
    """Return an ordered list of just the LOCAL PART (before the @) for every
    standard corporate-email pattern, most-likely first. Domain is added
    separately so we can try each local-part against every candidate domain."""
    first = re.sub(r"[^a-z]", "", (first or "").lower())
    last = re.sub(r"[^a-z]", "", (last or "").lower())
    middle = re.sub(r"[^a-z]", "", (middle or "").lower())

    parts = []

    def add(p):
        if p and p not in parts:
            parts.append(p)

    if first and last:
        f, l = first, last
        fi, li = f[0], l[0]
        mi = middle[0] if middle else ""

        # Ordered roughly by how common each pattern is in practice.
        add(f"{f}.{l}")          # john.doe
        add(f"{f}{l}")           # johndoe
        add(f"{fi}{l}")          # jdoe
        add(f"{f}{li}")          # johnd
        add(f"{fi}.{l}")         # j.doe
        add(f"{f}.{li}")         # john.d
        add(f"{f}_{l}")          # john_doe
        add(f"{f}-{l}")          # john-doe
        add(f"{f}")              # john
        add(f"{l}")              # doe
        add(f"{l}.{f}")          # doe.john
        add(f"{l}{f}")           # doejohn
        add(f"{l}.{fi}")         # doe.j
        add(f"{li}{f}")          # djohn
        add(f"{l}_{f}")          # doe_john
        add(f"{fi}{li}")         # jd
        if mi:
            add(f"{f}.{mi}.{l}")     # john.m.doe
            add(f"{fi}{mi}{l}")      # jmdoe
            add(f"{f}{mi}{l}")       # johnmdoe
    elif first:
        # Single-token slug (e.g. "victorwanja"): can't split reliably, so
        # just try the whole token and a couple of trivial variants.
        add(first)
        # crude vowel-boundary guess is unreliable; skip it and just try token.

    return parts


def generate_email_candidates(first: str, last: str, domain: str, middle: str = ""):
    """Every (local-part @ domain) combination for a SINGLE domain, ordered."""
    if not domain:
        return []
    return [f"{lp}@{domain}" for lp in generate_local_parts(first, last, middle)]


def generate_all_candidates(first: str, last: str, domains, middle: str = ""):
    """Every (local-part @ domain) combination across ALL candidate domains.

    Ordering strategy: try the MOST-LIKELY patterns on the MOST-LIKELY domain
    first. Concretely we go domain-by-domain (the domain list is already in
    priority order), and within each domain we go pattern-by-pattern. This
    means we fully exhaust the primary domain's patterns before spending
    probes on fallback domains, which is usually what you want."""
    all_candidates = []
    for domain in domains:
        for lp in generate_local_parts(first, last, middle):
            addr = f"{lp}@{domain}"
            if addr not in all_candidates:
                all_candidates.append(addr)
    return all_candidates


# --------------------------------------------------------------------------
# Step 4: SMTP-based verification
# --------------------------------------------------------------------------

class DomainMailInfo:
    """Caches MX records + catch-all status per domain so we don't repeat
    the lookup / probing for every employee at the same company."""

    def __init__(self):
        self._mx_cache = {}
        self._catch_all_cache = {}

    def get_mx_host(self, domain: str):
        if domain in self._mx_cache:
            return self._mx_cache[domain]
        host = None
        try:
            resolver = dns.resolver.Resolver()
            resolver.timeout = 4       # seconds per DNS server attempt
            resolver.lifetime = 6      # seconds total, across retries -- hard cap
            answers = resolver.resolve(domain, "MX")
            # lowest preference number = highest priority
            best = min(answers, key=lambda r: r.preference)
            host = str(best.exchange).rstrip(".")
        except Exception as e:
            logger.debug(f"  [{domain}] MX lookup failed: {e}")
            host = None
        self._mx_cache[domain] = host
        return host

    def is_catch_all(self, domain: str, mx_host: str, from_addr: str):
        """Probe a random, almost-certainly-nonexistent mailbox. If the
        server accepts it, the domain is catch-all and individual
        verification results for it can't be trusted."""
        if domain in self._catch_all_cache:
            return self._catch_all_cache[domain]
        random_local = "no-such-user-" + "".join(random.choices(string.ascii_lowercase, k=12))
        fake_addr = f"{random_local}@{domain}"
        logger.debug(f"  [{domain}] catch-all check -- probing random address {fake_addr}")
        result, code, detail = _smtp_probe(mx_host, from_addr, fake_addr)
        is_catch_all = result is True
        if is_catch_all:
            logger.info(f"  [{domain}] catch-all DETECTED (random address was accepted, code {code}) "
                        f"-- individual results for this domain can't be trusted")
        else:
            logger.debug(f"  [{domain}] not catch-all (random address -> {detail})")
        self._catch_all_cache[domain] = is_catch_all
        return is_catch_all


def _smtp_probe_inner(mx_host: str, from_addr: str, to_addr: str):
    """Does the actual SMTP conversation. May block past SMTP_TIMEOUT_SECS
    if the OS/network silently drops packets instead of refusing the
    connection -- that's why this is only ever called through the
    watchdog wrapper _smtp_probe(), never directly."""
    socket.setdefaulttimeout(SMTP_TIMEOUT_SECS)
    with smtplib.SMTP(mx_host, 25, timeout=SMTP_TIMEOUT_SECS) as smtp:
        smtp.ehlo_or_helo_if_needed()
        code, msg = smtp.mail(from_addr)
        if code >= 400:
            return None, code, f"MAIL FROM rejected ({code} {msg})"
        code, msg = smtp.rcpt(to_addr)
        msg_txt = msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg)
        if code in (250, 251):
            return True, code, f"ACCEPTED ({code} {msg_txt})"
        if code in (550, 551, 553, 554):
            return False, code, f"REJECTED ({code} {msg_txt})"
        return None, code, f"AMBIGUOUS response ({code} {msg_txt})"


# We run each SMTP attempt on its own daemon thread and enforce a hard
# wall-clock timeout from the main thread. Daemon=True is essential here:
# if a connection genuinely never resolves (silently dropped packets), the
# thread will sit blocked forever, and daemon threads are the only kind
# Python will let the process exit without waiting for.
HARD_WATCHDOG_SECS = SMTP_TIMEOUT_SECS + 4  # headroom over the inner socket timeout


def _smtp_probe(mx_host: str, from_addr: str, to_addr: str):
    """Return (result, smtp_code, detail_str).
    result: True (accepted / looks valid), False (rejected / invalid),
    or None (couldn't determine -- timeout, blocked, greylisted, etc.).
    Guaranteed to return within HARD_WATCHDOG_SECS no matter what."""
    if not mx_host:
        return None, None, "no MX host"

    box = {}  # shared container the worker thread writes its result into

    def worker():
        try:
            box["result"] = _smtp_probe_inner(mx_host, from_addr, to_addr)
        except socket.timeout:
            box["result"] = (None, None, f"timed out after {SMTP_TIMEOUT_SECS}s (port 25 likely blocked/filtered)")
        except ConnectionRefusedError:
            box["result"] = (None, None, "connection refused by mail server")
        except (OSError, smtplib.SMTPException) as e:
            box["result"] = (None, None, f"error: {e}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=HARD_WATCHDOG_SECS)

    if "result" in box:
        return box["result"]

    # Thread is still stuck (blocked socket that never times out on its own).
    # We abandon it -- it'll die with the process -- and move on.
    return None, None, (f"hard timeout after {HARD_WATCHDOG_SECS}s -- connection never completed "
                         f"(port 25 is almost certainly blocked/filtered on this network)")


def verify_person(first, last, middle, domains, mail_info: DomainMailInfo, delay: float):
    """Exhaust EVERY (name-pattern x domain) combination for ONE person,
    stopping the moment SMTP confirms a valid mailbox. Only a genuinely
    verified address is ever returned.

    Returns (email_or_None, status, tried_addresses) where status is one of:
      'valid'            - SMTP confirmed a real mailbox (email is set)
      'catch_all'        - some domain accepts everything; cannot verify
                           individual addresses, so NOTHING is saved (email None)
      'no_domain'        - no mail-capable domain for this company
      'no_name'          - couldn't parse any name from the URL
      'not_found'        - exhausted all combinations, none confirmed
    """
    if not domains:
        logger.warning("  no mail-capable domain for this company -- skipping")
        return None, "no_domain", []

    local_parts = generate_local_parts(first, last, middle)
    if not local_parts:
        logger.warning("  could not parse a usable name from the URL -- skipping")
        return None, "no_name", []

    # Build the full ordered combination list: primary domain's patterns first,
    # then fall back to the next domain, etc.
    candidates = generate_all_candidates(first, last, domains, middle)
    logger.info(f"  {len(local_parts)} name-pattern(s) x {len(domains)} domain(s) "
                f"= {len(candidates)} total combination(s) to try")

    tried = []
    catch_all_seen = False

    # Group probing by domain so we resolve MX + catch-all once per domain.
    for domain in domains:
        mx_host = mail_info.get_mx_host(domain)
        if not mx_host:
            logger.debug(f"  [{domain}] no MX -- skipping this domain")
            continue
        logger.debug(f"  [{domain}] mail server: {mx_host}")

        # Catch-all check: if the server accepts a random address, we can't
        # trust ANY positive result for this domain, so we skip it entirely
        # rather than save an unverifiable guess.
        if mail_info.is_catch_all(domain, mx_host, PROBE_FROM_ADDRESS):
            logger.warning(f"  [{domain}] is catch-all -- cannot verify individual "
                           f"addresses here; skipping this domain (won't save guesses)")
            catch_all_seen = True
            continue

        domain_candidates = [f"{lp}@{domain}" for lp in local_parts]
        for addr in domain_candidates:
            n = len(tried) + 1
            tried.append(addr)
            logger.info(f"  [{n}/{len(candidates)}] trying {addr} ...")
            result, code, detail = _smtp_probe(mx_host, PROBE_FROM_ADDRESS, addr)
            time.sleep(delay)

            if result is True:
                logger.info(f"  [{n}/{len(candidates)}] {addr} -> PASSED ({detail})")
                logger.info(f"  -> VERIFIED email: {addr}")
                return addr, "valid", tried
            elif result is False:
                logger.info(f"  [{n}/{len(candidates)}] {addr} -> FAILED ({detail}) -- next")
            else:
                logger.info(f"  [{n}/{len(candidates)}] {addr} -> UNKNOWN ({detail}) -- next")

    if catch_all_seen:
        logger.warning("  -> no verifiable email (catch-all domain blocked verification)")
        return None, "catch_all", tried
    logger.warning(f"  -> no email verified out of {len(tried)} combination(s) tried")
    return None, "not_found", tried


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def process(profiles_path, company_info_path, output_path, delay, skip_preflight=False):
    with open(profiles_path, "r", encoding="utf-8") as f:
        profiles_data = json.load(f)
    with open(company_info_path, "r", encoding="utf-8") as f:
        company_info_data = json.load(f)

    domain_map = build_company_domain_map(company_info_data)
    mail_info = DomainMailInfo()

    # Preflight: this whole script depends on SMTP verification. If port 25
    # is blocked we CANNOT verify anything, and since we only ever save
    # verified emails, the run would produce nothing. Fail loudly and stop
    # rather than churn for hours.
    if not skip_preflight:
        logger.info("Preflight: checking whether this network can reach port 25 at all ...")
        if check_port25_connectivity():
            logger.info("Preflight OK -- port 25 is reachable. Proceeding with SMTP verification.")
        else:
            logger.error("=" * 70)
            logger.error("PREFLIGHT FAILED: cannot reach ANY mail server on port 25 from this network.")
            logger.error("This script verifies every email over SMTP and only saves VERIFIED ones, so")
            logger.error("with port 25 blocked it cannot confirm a single address -- the output would be")
            logger.error("empty no matter how many combinations we try. This is a network/ISP/router")
            logger.error("block (very common on home wifi, many offices, and most cloud sandboxes), NOT")
            logger.error("a bug in the combination logic.")
            logger.error("")
            logger.error("To actually find emails, run this from a host with real port-25 egress:")
            logger.error("  - a VPS/cloud box where you've requested port 25 be unblocked, or")
            logger.error("  - swap the SMTP check for a paid verification API (Hunter, ZeroBounce, etc).")
            logger.error("")
            logger.error("Aborting now so you don't wait on guaranteed timeouts. (Use --skip-preflight")
            logger.error("to force it to try anyway.)")
            logger.error("=" * 70)
            # Still write an (empty-of-emails) results file so downstream steps don't crash.
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2, ensure_ascii=False)
            return []

    results = []

    for company in profiles_data:
        company_name = company.get("company_name", "")
        profile_urls = company.get("profiles", [])
        website_domain = domain_map.get(company_name, "")

        mail_domains, domain_note = resolve_mail_domains(website_domain, mail_info)

        company_result = {
            "company_name": company_name,
            "website_domain": website_domain or None,
            "mail_domains": mail_domains,          # every mail-capable domain we'll try
            "domain_status": "ok" if mail_domains else "not_found",
            "domain_note": domain_note,
            "employees": [],                        # only VERIFIED people go in here
        }

        logger.info("=" * 70)
        logger.info(f"COMPANY: {company_name}")
        logger.info(f"  website domain: {website_domain or 'NOT FOUND'}")
        logger.info(f"  mail domain(s): {', '.join(mail_domains) if mail_domains else 'NONE FOUND'}  ({domain_note})")
        logger.info(f"  {len(profile_urls)} profile(s)")

        for idx, url in enumerate(profile_urls, start=1):
            first, last, middle, confidence = extract_name_from_url(url)
            name_guess = " ".join(p.capitalize() for p in (first, middle, last) if p)
            logger.info(f"[{idx}/{len(profile_urls)}] {url}")
            logger.debug(f"  parsed name: first={first!r} middle={middle!r} last={last!r} conf={confidence}")

            email, status, tried = verify_person(first, last, middle, mail_domains, mail_info, delay)

            if status == "valid" and email:
                # ONLY verified emails are saved.
                company_result["employees"].append({
                    "profile_url": url,
                    "name_guess": name_guess,
                    "name_confidence": confidence,
                    "email": email,
                    "combinations_tried": len(tried),
                })
            else:
                # Not saved into employees, but logged so you can see it was
                # attempted and why it didn't yield a verified address.
                logger.info(f"  (not saved: {status})")

        logger.info(f"  => {len(company_result['employees'])}/{len(profile_urls)} verified for {company_name}")
        results.append(company_result)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Find and SMTP-VERIFY corporate emails for LinkedIn profiles grouped by company. "
                    "Only verified emails are saved."
    )
    parser.add_argument("profiles_json", help="Path to the profiles JSON file (company_name + profiles[])")
    parser.add_argument("company_info_json", help="Path to the company-info JSON file (company_name + website)")
    parser.add_argument("-o", "--output", default="email_results.json", help="Output JSON path")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECS,
                         help="Seconds to wait between SMTP attempts (politeness / rate-limit)")
    parser.add_argument("--skip-preflight", action="store_true",
                         help="Skip the initial port-25 connectivity check and attempt SMTP verification "
                              "regardless")
    parser.add_argument("--log-file", default="email_finder.log",
                         help="Path to write full debug-level logs to (set to '' to disable file logging)")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="Also print internal debug detail (MX lookups, catch-all checks) to the "
                              "console (the log file always gets this detail regardless)")
    args = parser.parse_args()

    setup_logging(args.log_file, args.verbose)

    results = process(args.profiles_json, args.company_info_json, args.output, args.delay,
                      args.skip_preflight)

    total_people = sum(len(c.get("employees", [])) for c in results)
    total_profiles = 0
    # Recount profiles from the input for an honest found/total ratio.
    try:
        with open(args.profiles_json, "r", encoding="utf-8") as f:
            total_profiles = sum(len(c.get("profiles", [])) for c in json.load(f))
    except Exception:
        total_profiles = total_people

    logger.info("=" * 70)
    logger.info(f"Processed {len(results)} companies, {total_profiles} profiles.")
    logger.info(f"VERIFIED emails saved: {total_people}/{total_profiles}")
    logger.info(f"Results written to: {args.output}")
    if args.log_file:
        logger.info(f"Full log written to: {args.log_file}")


if __name__ == "__main__":
    main()