import json, os
def check(label, path, checks=None):
    if not os.path.isfile(path):
        print(f"{label}: MISSING FILE")
        return
    if checks is None:
        print(f"{label}: HAS FILE ({os.path.getsize(path)} bytes)")
        return
    with open(path) as f:
        d = json.load(f)
    results = []
    for subkey in checks:
        val = d
        for k in subkey.split("."):
            if isinstance(val, dict):
                val = val.get(k)
            elif isinstance(val, list):
                val = len(val)
            else:
                val = str(val) if val else "N/A"
        results.append(f"{subkey}={val}")
    print(f"{label}: {