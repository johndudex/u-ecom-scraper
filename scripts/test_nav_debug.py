import os, sys, json, logging, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, "/app/webapp")
os.chdir("/app")
django.setup()
logging.disable(logging.CRITICAL)

import agents.nodes.navigate_explore as mod
orig = mod._extract_product_links_bs
def debug(soup, base):
    r = orig(soup, base)
    print(f"EXTRACT_RESULT: {len(r)} links", flush=True)
    for p in r[:5]:
        print(f"  -> {p.get('href','')[:80]}", flush=True)
    return r
mod._extract_product_links_bs = debug

from agents.nodes.navigate_explore import navigate_explore

slug = "calvinklein-co-uk-test6"
import shutil
ws = f"workspace/{slug}"
if os.path.isdir(ws): shutil.rmtree(ws)
os.makedirs(ws)
shutil.copy2(f"workspace/calvinklein-co-uk/site_analysis.json", f"{ws}/site_analysis.json")

state = {"job_id": 0, "url": "https://www.calvinklein.co.uk", "site_slug": slug, "search_criteria": "watches", "input_mode": "navigation", "sample_url": "", "product_url": ""}
print("START", flush=True)
result = navigate_explore(state)
print(f"END: {len(result)} keys", flush=True)
