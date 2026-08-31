"""[job-71 popsockets] Draft output filenames must be unique per process.

The failure: ``code_tester`` launched the draft's two phases as parallel tool
calls 1 ms apart — ``--sample --input input_urls.json`` (Phase 2) and
``--listing-url ... --fresh-discovery --discover-only --limit 50`` (Phase 1).
Both subprocesses computed the same module-level
``output_<second-resolution-timestamp>.json``; the Phase-2 run wrote
``Total: 1, Failed: 0`` and the ``--discover-only`` run — which by template
design writes an empty ``products`` array — overwrote it 0.5 s later. Every
rescue gate in ``route_after_testing`` scans ``output_*.json`` on disk, found
only the empty file, and the job finalized ``testing cascade exhausted
without a passing run`` — despite stdout proving the scraper worked.

Static contract per the template-test convention (templates are not
importable — see test_discovery_ladder / test_cli_input_path_resolution).
"""
from __future__ import annotations

import os
import re

TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)

# Templates that compute OUTPUT_FILE at module level (TIMESTAMP constant).
MODULE_LEVEL = [
    "api_scraper.py",
    "playwright_scraper.py",
    "requests_scraper.py",
    "shopify_scraper.py",
    "undetected_chromedriver_scraper.py",
]

# Templates that compute the output filename at the write site (a local
# ``timestamp`` / inline strftime inside main()).
WRITE_TIME = {
    "http_navigation_scraper.py",
    "navigation_scraper.py",
    "ssr_div_list_scraper.py",
}

ALL = MODULE_LEVEL + sorted(WRITE_TIME)


def _src(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name)) as fh:
        return fh.read()


class TestModuleLevelTemplates:
    def test_timestamp_has_subsecond_resolution(self):
        for name in MODULE_LEVEL:
            src = _src(name)
            assert re.search(r'TIMESTAMP\s*=.*strftime\([^)]*%f', src), (
                f"{name}: TIMESTAMP uses second resolution — two drafts "
                "launched in the same second share one output file and the "
                "later write silently erases the earlier result (job-71)"
            )

    def test_output_file_embeds_pid(self):
        for name in MODULE_LEVEL:
            src = _src(name)
            assert re.search(r'output_\{TIMESTAMP\}_\{os\.getpid\(\)\}', src), (
                f"{name}: OUTPUT_FILE lacks a pid component — the only "
                "cross-process uniqueness guarantee (job-71 collision)"
            )


class TestWriteTimeTemplates:
    def test_output_filename_embeds_pid(self):
        for name in sorted(WRITE_TIME):
            src = _src(name)
            assert re.search(r'output_\{[^}]*\}_\{os\.getpid\(\)\}\.json', src), (
                f"{name}: output filename lacks a pid component (job-71 "
                "collision class — concurrent runs of one draft clobber "
                "each other's results)"
            )

    def test_every_template_writes_exactly_one_output_path_pattern(self):
        """Guard against a template gaining a second, still-second-resolution
        write site while the pinned one is updated."""
        for name in ALL:
            src = _src(name)
            assert len(re.findall(r'output_\{TIMESTAMP\}\.json|output_\{timestamp\}\.json', src)) == 0, (
                f"{name}: a second-resolution output filename is still present"
            )
