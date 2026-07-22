#!/usr/bin/env python3
"""Standalone runner for the navigation-traversal prototype.

Usage (inside a container that can reach the site + the LLM):
    python experimental/nav_traversal/run_traversal.py URL CONTENT_TYPE QUERY
    python experimental/nav_traversal/run_traversal.py https://www.ayahealthcare.com/ job_posting nursing
    python experimental/nav_traversal/run_traversal.py https://www.locumtenens.com/ job_posting physician

Prints the discovered traversal path, the chosen mechanism, filters/pagination,
and a PROOF-OF-EXTRACTION (actually fires the found API / form POST and counts
real jobs). No existing agents are touched.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# make repo root importable for `agents.nodes.url_judge` (default judge)
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_WEBAPP = os.path.join(_REPO, "webapp")
for _p in (_WEBAPP, _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The default judge reuses agents.nodes.url_judge -> agents.llm (the LLM), which
# reads Django settings. Configure Django so the real judge works live. (This
# only CONFIGURES Django; it does not modify any agent.)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
try:
    import django
    django.setup()
except Exception as _e:  # pragma: no cover
    print(f"[warn] django.setup() failed ({_e}); the default LLM judge will be unavailable.")

from bs4 import BeautifulSoup  # noqa: E402

from experimental.nav_traversal.traversal import (  # noqa: E402
    _default_fetch,
    browser_traverse,
    count_job_signals,
    extract_links,
)


def _print_result(res) -> None:
    print("\n" + "=" * 72)
    print(f"REACHED GOAL: {res.reached}   mechanism: {res.mechanism}")
    print(f"path ({len(res.path)} hops):")
    for i, u in enumerate(res.path):
        print(f"  {i}. {u}")
    print(f"goal_url: {res.goal_url}")
    print(f"notes: {res.notes}")
    print(f"signals: job_links={res.signals.get('job_links')}, "
          f"results_items={res.signals.get('results_items')}, "
          f"api={'yes' if res.api else 'no'}")
    if res.api:
        print(f"  api_url: {res.api.get('api_url')}")
        print(f"  api_count_reported: {res.api.get('count')}  "
              f"sample_keys: {res.api.get('sample_keys')}")
    print(f"visited {len(res.visited)} page(s); pruned {len(res.pruned)} candidate(s).")
    if res.pruned:
        print("  pruned (LLM judged off-goal):")
        for p in res.pruned[:8]:
            print(f"    - {p[:110]}")


def _read_filters_pagination(html: str) -> dict:
    """Light pass for <select> filters + pagination on the goal page."""
    soup = BeautifulSoup(html, "html.parser")
    filters = []
    for sel in soup.find_all("select"):
        name = (sel.get("name") or sel.get("id") or "").strip()
        n_opts = len([o for o in sel.find_all("option") if (o.get("value") or "").strip()])
        if name and n_opts > 1:
            filters.append({"name": name, "options": n_opts})
    # pagination: ?page=/&pgNum=/&start= params in links, or a[rel=next]
    pag = {"next_rel": bool(soup.select_one('a[rel="next"]'))}
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&](pgNum|page|p|start|offset)=(\d+)", a["href"], re.I)
        if m:
            pag["param"] = m.group(1)
            break
    return {"filters": filters, "pagination": pag}


def proof_of_extraction(res, query: str) -> None:
    print("\n" + "-" * 72)
    print("PROOF OF EXTRACTION (firing the discovered path)")
    if not res.reached:
        print("  goal not reached — nothing to prove.")
        return
    if res.mechanism == "api" and res.api:
        base = res.api["api_url"]
        params = {"limit": 500, "offset": 0, **{k: v for k, v in res.api.get("sample_params", {}).items()}}
        r = _default_fetch(base, method="GET", params=params)
        if r.get("ok"):
            import json as _json
            try:
                data = _json.loads(r["text"])
                # find the items list + count
                items = None
                total = None
                stack = [data]
                while stack:
                    v = stack.pop()
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        if items is None or len(v) > len(items or []):
                            items = v
                    elif isinstance(v, dict):
                        for k, val in v.items():
                            if k.lower() in ("count", "total", "totalcount"):
                                try:
                                    total = int(val)
                                except Exception:
                                    pass
                            stack.append(val)
                print(f"  API {base}?{params}")
                print(f"  -> items on page 1: {len(items or [])}; reported total: {total}")
                if items:
                    j = items[0]
                    show = {k: j.get(k) for k in list(j)[:8]}
                    print(f"  sample item: {show}")
            except Exception as e:
                print(f"  API response not JSON: {e}; body[:200]={r['text'][:200]}")
        else:
            print(f"  API GET failed: status={r.get('status')}")
    elif res.goal_method.upper() == "POST" and res.goal_request_url:
        # replay the form POST -> results page
        r = _default_fetch(res.goal_request_url, method="POST", data=res.goal_data)
        print(f"  POST {res.goal_request_url}  data={res.goal_data}")
        print(f"  -> status={r.get('status')}  final_url={r.get('final_url')}")
        html = r.get("text") or ""
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select('[class*="job-results"]') or soup.select('[class*="job-result"]')
        # total text like "1 - 25 of 3790"
        total = None
        m = re.search(r"(\d+)\s*[-–]\s*\d+\s+of\s+([\d,]+)", html)
        if m:
            total = m.group(2)
        print(f"  -> result cards on page 1: {len(items)}; total reported: {total}")
        if items:
            print(f"  sample card text: {(items[0].get_text(' ') or '').strip()[:160]}")
    else:
        print(f"  mechanism={res.mechanism}; goal_method={res.goal_method} — "
              "fetch the goal page and count listing items:")
        r = _default_fetch(res.goal_url, method="GET")
        from experimental.nav_traversal.traversal import count_job_signals
        s = count_job_signals(r.get("text") or "")
        print(f"  -> job_links={s['job_links']} results_items={s['results_items']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("content_type", nargs="?", default="job_posting")
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--no-judge", action="store_true",
                    help="skip the LLM judge (keep all candidates) — for offline sanity")
    args = ap.parse_args()

    judge = None
    if args.no_judge:
        judge = lambda c, ct, q, s: {"ranking": [{"url": x["href"], "verdict": "correct"} for x in c]}

    res = browser_traverse(args.url, args.content_type, args.query)
    _print_result(res)

    # filters + pagination off the goal page
    if res.goal_url:
        r = _default_fetch(res.goal_url, method="GET")
        fp = _read_filters_pagination(r.get("text") or "")
        print(f"\nfilters on goal page: {fp['filters']}")
        print(f"pagination: {fp['pagination']}")

    proof_of_extraction(res, args.query)

    # Network-log check (only on the listing we reached): did the data come from an API?
    if res.reached and res.goal_url:
        print("\n" + "-" * 72)
        print("NETWORK LOG CHECK (does the data come from a backend API?)")
        from experimental.nav_traversal.traversal import (
            api_from_network, capture_network_resources,
        )
        resources = capture_network_resources(res.goal_url)
        apis = api_from_network(resources)
        if apis:
            print(f"  YES — the page fetched data from {len(apis)} API-looking endpoint(s):")
            for u in apis[:6]:
                print(f"    - {u[:140]}")
        else:
            print(f"  NO backend API detected — the data is in the page HTML itself "
                  f"(checked {len(resources)} network requests).")

    print("=" * 72)
    return 0 if res.reached else 1


if __name__ == "__main__":
    raise SystemExit(main())
