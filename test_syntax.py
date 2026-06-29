import sys
sys.path.insert(0, "/app")
# Quick syntax + import check
try:
    import ast
    with open("workspace/adameve-com/scraper_draft.py") as f:
        source = f.read()
    ast.parse(source)
    print("✅ Syntax check passed")
except SyntaxError as e:
    print(f"❌ Syntax error: {e}")

# Check imports
try:
    import requests
    print("✅ requests available")
except ImportError:
    print("❌ requests missing")

try:
    from bs4 import BeautifulSoup
    print("✅ beautifulsoup4 available")
except ImportError:
    print("❌ beautifulsoup4 missing")

try:
    import lxml
    print("✅ lxml available")
except ImportError:
    print("❌ lxml missing")
