"""
extract_details.py

Strips a LinkedIn "About" scrape (results.json) down to just the real
company details, discarding:
  - nav/footer boilerplate (Home, My Network, Accessibility, Ad Choices, ...)
  - the language picker list
  - scraper/DOM metadata (selectors, viewport, rect coords, outer_html, aria labels, char_count...)
  - "people also viewed / follow" suggestion blocks

Usage:
    python3 extract_details.py results.json results_clean.json
"""

import json
import re
import sys


def clean_title(title):
    """'(1) AuraLearn AI: About | LinkedIn' -> 'AuraLearn AI'"""
    if not title:
        return None
    t = re.sub(r'^\(\d+\)\s*', '', title)          # drop leading "(1) " notification count
    t = re.sub(r':\s*About\s*\|\s*LinkedIn$', '', t)
    t = re.sub(r'\s*\|\s*LinkedIn$', '', t)
    return t.strip() or None


def extract_followers(text):
    m = re.search(r'([\d,.]+\s?[KMk]?)\s+followers', text or "")
    return m.group(1).strip() if m else None


def extract_employee_range(text):
    m = re.search(r'(\d[\d,]*\+?-?\d*)\s+employees', text or "")
    return m.group(1) if m else None


def first_line(text):
    if not text:
        return None
    line = text.strip().split("\n")[0].strip()
    return line or None


def extract_description(about):
    """Pull the real 'Overview' paragraph(s), ignoring nav/footer text."""
    for sec in (about.get("sections") or []):
        paras = sec.get("paragraphs")
        if paras:
            joined = "\n\n".join(p.strip() for p in paras if p and p.strip())
            if joined:
                return joined
    return None


def clean_company_size(value):
    # fields["Company size"] can be a plain string or [employees, "N associated members ..."]
    if isinstance(value, list):
        return value[0] if value else None
    return value


def clean_specialties(value):
    # "RMI, Home Furnishings Rentals, ..." -> ["RMI", "Home Furnishings Rentals", ...]
    if not value:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or None


def clean_phone(value):
    # raw value repeats itself: "+91 74980 74484 Phone number is +91 74980 74484"
    if not value:
        return None
    m = re.match(r'^(.*?)\s+Phone number is\s+', value)
    return m.group(1).strip() if m else value.strip()


def is_failed_scrape(entry):
    """Detect scrapes that never reached a real company About page
    (e.g. redirected to the logged-in user's own feed/notifications)."""
    about = entry.get("about") or {}
    return not about.get("company_slug") and not about.get("fields")


def extract_record(entry):
    about = entry.get("about") or {}
    fields = about.get("fields") or {}
    header_text = entry.get("visible_text") or ""

    record = {
        "company_name": clean_title(entry.get("title")) or first_line(header_text),
        "searched_as": entry.get("company_input") or entry.get("company_name_searched"),
        "linkedin_url": entry.get("url"),
        "website": fields.get("Website"),
        "industry": fields.get("Industry"),
        "company_size": clean_company_size(fields.get("Company size")),
        "headquarters": fields.get("Headquarters"),
        "founded": fields.get("Founded"),
        "specialties": clean_specialties(fields.get("Specialties")),
        "verified_since": fields.get("Verified page"),
        "phone": clean_phone(fields.get("Phone")),
        "followers": extract_followers(header_text),
        "description": extract_description(about),
    }

    # drop empty/None values so the output only shows details actually present
    return {k: v for k, v in record.items() if v not in (None, "", [], {})}


def main():
    in_path = sys.argv[1] if len(sys.argv) > 1 else "results.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "results_clean.json"
    failed_path = sys.argv[3] if len(sys.argv) > 3 else "results_failed.json"

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        data = [data]

    cleaned, failed = [], []
    for entry in data:
        if is_failed_scrape(entry):
            failed.append({
                "searched_as": entry.get("company_input") or entry.get("company_name_searched"),
                "landed_on_url": entry.get("url"),
                "reason": "no company data found (likely a bad/failed search redirect)",
            })
        else:
            cleaned.append(extract_record(entry))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)

    with open(failed_path, "w", encoding="utf-8") as f:
        json.dump(failed, f, indent=2, ensure_ascii=False)

    print(f"Read {len(data)} record(s) from {in_path}")
    print(f"Wrote {len(cleaned)} cleaned record(s) to {out_path}")
    if failed:
        print(f"Wrote {len(failed)} failed/empty scrape(s) to {failed_path}")


if __name__ == "__main__":
    main()