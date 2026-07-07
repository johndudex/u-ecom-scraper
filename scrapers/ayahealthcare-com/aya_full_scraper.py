#!/usr/bin/env python3
"""Aya Healthcare full job extraction via the backend Job/search API.
Discovered by navigate_explore (api.ayahealthcare.com/AyaHealthCareWeb/Job/search).
Paginates offset (500/page) to the full count (~25,811). Pure HTTP — no browser."""
import json, time, os
from datetime import datetime, timezone
import httpx

URL = "https://api.ayahealthcare.com/AyaHealthCareWeb/Job/search"
LOOKUPS = "https://www.ayahealthcare.com/wp-json/aya/v1/joblookups"
H = {"Accept": "application/json", "Referer": "https://www.ayahealthcare.com/", "Origin": "https://www.ayahealthcare.com", "User-Agent": "Mozilla/5.0"}
PAGE = 500
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# stateCode is a numeric ID in the API — map it to abbreviation/name via the
# joblookups taxonomy (fetched once at startup).
_STATES = {}
try:
    _lk = httpx.get(LOOKUPS, headers=H, timeout=30).json()
    _STATES = {s["id"]: s for s in _lk.get("states", [])}
except Exception:
    pass

def fetch_page(offset):
    r = httpx.get(URL, params={"limit": PAGE, "offset": offset}, headers=H, timeout=60)
    r.raise_for_status()
    return r.json()

def map_job(i):
    city = i.get("city") or ""
    code = i.get("stateCode")
    st_abbr = st_name = ""
    if isinstance(code, int) and code in _STATES:
        st_abbr = _STATES[code].get("abbreviation", "")
        st_name = _STATES[code].get("name", "")
    loc = f"{city}, {st_abbr}".strip(", ") if (city or st_abbr) else ""
    pay = None
    if i.get("weeklyPayLow") or i.get("weeklyPayHigh"):
        pay = f"${i.get('weeklyPayLow')}-{i.get('weeklyPayHigh')}/wk"
    elif i.get("regularPayLow") or i.get("regularPayHigh"):
        pay = f"${i.get('regularPayLow')}-{i.get('regularPayHigh')}/yr"
    return {
        "job_id": i.get("jobID"),
        "title": i.get("expertiseText") or i.get("professionText") or "Healthcare Job",
        "profession": i.get("professionText"),
        "specialty": i.get("expertiseText"),
        "company": i.get("facilityName") or "Aya Healthcare",
        "location": loc, "city": city, "state": st_abbr, "state_name": st_name,
        "employment_type": i.get("employmentTypeText"),
        "shift": i.get("shiftText"), "hours": i.get("hours"),
        "pay": pay,
        "weekly_pay_low": i.get("weeklyPayLow"), "weekly_pay_high": i.get("weeklyPayHigh"),
        "annual_pay_low": i.get("regularPayLow"), "annual_pay_high": i.get("regularPayHigh"),
        "start_date": i.get("startDate"), "posted_date": i.get("enteredTime"),
        "positions": i.get("positions"),
        "url": f"https://www.ayahealthcare.com/jobs/{i.get('jobID')}",
    }

def main():
    all_items, offset, total, t0 = [], 0, None, time.time()
    while True:
        d = fetch_page(offset)
        items = d.get("items", [])
        if total is None:
            total = d.get("count"); print(f"API reports total count: {total}")
        all_items.extend(items)
        print(f"offset={offset:>6} got={len(items):>3} cum={len(all_items):>6}/{total}")
        if len(items) < PAGE or (total and len(all_items) >= total): break
        offset += PAGE
    jobs = [map_job(i) for i in all_items]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    out = os.path.join(SCRIPT_DIR, f"output_{ts}.json")
    payload = {"site": {"name": "Aya Healthcare", "url": "https://www.ayahealthcare.com", "scraping_method": "backend_api"}, "jobs": jobs, "metadata": {"total_reported": total, "extracted": len(jobs), "duration_seconds": round(time.time()-t0, 1), "scraped_at": datetime.now(timezone.utc).isoformat()}}
    with open(out, "w") as f: json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\nWROTE {out}: {len(jobs)} jobs (API reported {total}) in {round(time.time()-t0,1)}s")

if __name__ == "__main__":
    main()
