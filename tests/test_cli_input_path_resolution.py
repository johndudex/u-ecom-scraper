"""[job-60 zquiet] Relative ``--input`` seed paths must resolve against the
scraper's own directory, not the runner's CWD.

The failure: the tester's documented Phase-2 invocation
(``--sample --input input_urls.json``) ran the draft with CWD=/app while the
draft lived in ``workspace/zquiet-com/``. Drafts that open the raw
``args.input`` value crash with FileNotFoundError (exit 1) even though their
own ``INPUT_FILE`` constant (SCRIPT_DIR-based) works — job 60's cascade
burned all retries on exactly this and finalized FAILED with discovery at
13/13 PASS every cycle.

The five templates that accept ``--input`` now rewrite it against
``SCRIPT_DIR`` right after ``parse_args()``. ``ssr_div_list_scraper.py``
already resolved at its consumption site (different idiom, same guarantee)
and is pinned separately. Static contract per the template-test convention
(templates are not importable — see test_discovery_ladder).
"""
from __future__ import annotations

import ntpath
import os
import posixpath

TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)

# Templates with an --input flag that rewrite it against SCRIPT_DIR after
# parse_args(). navigation_scraper / http_navigation_scraper have no --input.
RESOLVING_TEMPLATES = [
    "api_scraper.py",
    "playwright_scraper.py",
    "shopify_scraper.py",
    "requests_scraper.py",
    "undetected_chromedriver_scraper.py",
]

RESOLUTION = "args.input = os.path.join(SCRIPT_DIR, args.input)"


def _src(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name)) as fh:
        return fh.read()


class TestInputPathResolution:
    def test_every_input_template_resolves_against_script_dir(self):
        for name in RESOLVING_TEMPLATES:
            src = _src(name)
            assert '"--input"' in src, f"{name}: no --input flag?"
            assert RESOLUTION in src, (
                f"{name}: --input value is opened raw — a relative seed path "
                "crashes when the runner's CWD differs from the scraper dir "
                "(job-60 zquiet FileNotFoundError class)"
            )

    def test_resolution_happens_after_parse_args(self):
        """The rewrite must run before the branch that consumes args.input."""
        for name in RESOLVING_TEMPLATES:
            src = _src(name)
            parse_at = src.find("args = parser.parse_args()")
            resolve_at = src.find(RESOLUTION)
            assert parse_at != -1 and resolve_at != -1, name
            assert resolve_at > parse_at, f"{name}: resolution before parse_args"

    def test_resolution_is_guarded(self):
        """Only rewrite when the flag was actually passed (default None)."""
        for name in RESOLVING_TEMPLATES:
            src = _src(name)
            resolve_at = src.find(RESOLUTION)
            guard = src.rfind("if args.input", 0, resolve_at)
            assert guard != -1, name
            between = src[guard:resolve_at]
            assert between.count("\n") < 12, (
                f"{name}: guard too far from the resolution line"
            )

    def test_ssr_div_list_resolves_at_consumption_site(self):
        """ssr_div_list never rewrote args.input — it joins at the open() site
        against the file's own dir. Same guarantee, different idiom: pin it so
        a refactor back to raw args.input can't slip through."""
        src = _src("ssr_div_list_scraper.py")
        assert '"--input"' in src
        assert "os.path.join(os.path.dirname(os.path.abspath(__file__)), args.input)" in src

    def test_absolute_values_still_work(self):
        """os.path.join semantics: joining an absolute path returns the
        absolute path — this is why the unconditional join is safe."""
        for j in (posixpath.join, ntpath.join):
            assert j("/scraper/dir", "/abs/seed.json") == "/abs/seed.json"
            assert j("/scraper/dir", "input_urls.json") != "input_urls.json"


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
