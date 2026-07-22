"""SCRATCH: verify the _capture_api_from_session RANKING picks aya's real jobs API
over the taxonomy false positive, before burning another pipeline rerun.
"""
import sys

sys.path.insert(0, "/app/experimental/nav_traversal")

from traversal import scan_bundles_for_api, verify_api, _httpx_fetch  # noqa: E402

GOAL = "https://www.ayahealthcare.com/healthcare-jobs/type/travel/"
QUERY = "travel nursing"
# Simulate the on-load taxonomy XHRs the network log would contain for aya.
NETWORK_CANDS = [
    "https://www.ayahealthcare.com/wp-json/aya/v1/joblookups",
    "https://www.ayahealthcare.com/wp-json/aya/v1/professions-with-types",
]

candidates = []
seen = set()


def consider(api):
    if not api:
        return
    base = (api.get("url") or "").split("?")[0]
    if base and base not in seen:
        seen.add(base)
        candidates.append(api)


# 1. network-log candidates
for c in NETWORK_CANDS:
    consider(verify_api(c, _httpx_fetch, QUERY))

# 2. bundle-scan candidates
page = _httpx_fetch(GOAL)
if page.get("ok"):
    for c in scan_bundles_for_api(page.get("text", ""), GOAL, _httpx_fetch):
        consider(verify_api(c, _httpx_fetch, QUERY))


def score(api):
    has_count = 1 if api.get("count") is not None else 0
    n_keys = len(api.get("sample_keys") or [])
    return (has_count, n_keys)


print(f"{len(candidates)} verified candidate(s):")
for a in sorted(candidates, key=score, reverse=True):
    print(f"  score={score(a)} count={a.get('count')} keys={len(a.get('sample_keys') or [])} {a.get('url','')[:70]}")

best = max(candidates, key=score) if candidates else None
print("\n=== PICKED ===")
print(best.get("url") if best else "NONE")
print("CORRECT" if best and "api.ayahealthcare.com" in (best.get("url") or "") else "WRONG")
