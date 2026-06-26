import json
import os

path = "/app/workspace/round4/product_analyzer/product_analysis.json"
text = open(path).read()

fixed = []
i = 0
in_string = False
while i < len(text):
    if text[i] == '"' and (i == 0 or text[i-1] != '\\'):
        in_string = not in_string
    if text[i] == '\\' and in_string and i + 1 < len(text):
        nc = text[i+1]
        if nc not in ('"', '\\', '/', 'b', 'f', 'n', 'r', 't', 'u'):
            fixed.append('\\\\')
            i += 2
            continue
    fixed.append(text[i])
    i += 1

result = ''.join(fixed)
open(path, 'w').write(result)

d = json.load(open(path))
fields = d.get('fields', {})
has_exp = sum(1 for v in fields.values() if isinstance(v, dict) and 'expectations' in v)
print(f"Fixed! Fields: {len(fields)}, with expectations: {has_exp}")
for k in fields:
    print(f"  {k}: {'OK' if 'expectations' in fields[k] else 'MISSING'}")
