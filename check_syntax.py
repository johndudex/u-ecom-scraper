#!/usr/bin/env python3
"""Quick syntax check for scraper_draft.py"""
import py_compile
import sys

try:
    py_compile.compile("workspace/calvinklein-co-uk/scraper_draft.py", doraise=True)
    print("✅ Syntax OK")
except py_compile.PyCompileError as e:
    print(f"❌ Syntax error: {e}")
    sys.exit(1)
