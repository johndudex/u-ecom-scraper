"""Run a single LangGraph node against an existing workspace — debug harness.

Reuses prior artifacts (site/nav/product/scraper analysis, test report) so a prompt
change can be validated on ONE node in seconds, not a 30–60 min full pipeline run.

Usage:
    # Re-run code_writer using existing analysis artifacts (patches ON, as in prod)
    python manage.py run_node calvinklein-co-uk code_writer

    # Same, but with post-generation patches DISABLED — shows the raw LLM output,
    # so you can prove a source-level fix makes a patch redundant before deleting it
    python manage.py run_node calvinklein-co-uk code_writer --no-patches

    # Pin a specific job (for identity / search_criteria / content type)
    python manage.py run_node calvinklein-co-uk product_analyzer --job-id 285

    # Override the probe method for navigation_explore (A/B cloak vs uc_chrome)
    python manage.py run_node calvinklein-co-uk navigation_explore --probe-method cloak_none

Nodes: site_analyzer, navigation_explore, navigation_synthesize, nav_skill_review,
product_analyzer, scraper_analyzer, code_writer, code_tester, cleanup, skill_learner.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

NODES = [
    "site_analyzer",
    "navigation_explore",
    "navigation_agent",
    "navigation_synthesize",
    "nav_skill_review",
    "product_analyzer",
    "scraper_analyzer",
    "code_writer",
    "code_tester",
    "cleanup",
    "skill_learner",
    "dagster_converter",
]

# Artifacts copied from scrapers/<slug>/analysis/ into workspace/<slug>/ so the
# file-scoped agent tools can read prior-phase output. (workspace is normally
# transient; analysis/ is the persistent post-cleanup store.)
ARTIFACT_FILES = [
    "site_analysis.json",
    "navigation_findings.json",
    "navigation_analysis.json",
    "product_analysis.json",
    "scraper_analysis.json",
    "test_report.json",
]


class Command(BaseCommand):
    help = "Run a single graph node against an existing workspace (debug harness)."

    def add_arguments(self, parser):
        parser.add_argument("site_slug", type=str)
        parser.add_argument("node", type=str, choices=sorted(NODES))
        parser.add_argument("--job-id", type=int, default=None)
        parser.add_argument(
            "--no-patches",
            action="store_true",
            help="Disable post-generation patches + strategy overrides (raw LLM output).",
        )
        parser.add_argument(
            "--probe-method",
            type=str,
            default=None,
            help="Override probe method for navigation_explore (e.g. cloak_none).",
        )
        parser.add_argument(
            "--fresh",
            action="store_true",
            help="Don't copy persisted artifacts into workspace (run from a clean slate).",
        )

    def handle(self, *args, **opts):
        from webapp.agents import graph
        from scraper.models import ScrapeJob, Site, ProbeCache
        from src.content_types import get_content_type

        slug = opts["site_slug"]
        node = opts["node"]
        root = getattr(settings, "PROJECT_ROOT", None) or os.environ.get("PROJECT_ROOT", "/app")
        workspace = Path(root) / "workspace" / slug
        analysis_dir = Path(root) / "scrapers" / slug / "analysis"
        workspace.mkdir(parents=True, exist_ok=True)

        # 1. Copy persisted analysis artifacts into the (transient) workspace so
        #    file-scoped agent tools can read them.
        if not opts["fresh"] and analysis_dir.is_dir():
            for jf in analysis_dir.glob("*.json"):
                if jf.name in ARTIFACT_FILES:
                    shutil.copy2(jf, workspace / jf.name)

        def load_artifact(name: str):
            p = workspace / name
            try:
                return json.loads(p.read_text()) if p.is_file() else None
            except Exception as exc:
                self.stderr.write(f"  warn: could not parse {name}: {exc}")
                return None

        before = {f.name: f.stat().st_mtime for f in workspace.glob("*.json")}

        # 2. Resolve identity from DB.
        site = Site.objects.filter(slug=slug).first()
        job = None
        if opts["job_id"]:
            job = ScrapeJob.objects.filter(id=opts["job_id"]).first()
        if not job:
            job = (
                ScrapeJob.objects.filter(url__icontains=slug.replace("-", "."))
                .order_by("-id")
                .first()
            )
        if not job and site:
            job = (
                ScrapeJob.objects.filter(url__icontains=urlparse(site.url).hostname or "")
                .order_by("-id")
                .first()
            )

        url = (site.url if site else None) or (job.url if job else "") or ""
        host = (urlparse(url).hostname or "").lower()

        # Load site_analysis early — it carries the reliable anti_bot signal
        # (ProbeCache.needs_akamai_bypass is often False even for protected sites
        # because the probe wasn't "blocked" — it rendered via uc_chrome).
        site_analysis = load_artifact("site_analysis.json")

        # 3. probe_result from ProbeCache (DB-only — not on disk) + site_analysis.
        #    set_tool_context derives anti_bot from this, so downstream guards fire
        #    correctly. Anti_bot is taken from site_analysis.anti_bot.detected OR
        #    a uc_chrome/cloak method (the real signal), not just the ProbeCache flag.
        probe_result = None
        sa = site_analysis or {}
        sa_anti = (sa.get("anti_bot") or {}) if isinstance(sa, dict) else {}
        sa_conn = (sa.get("connectivity") or {}) if isinstance(sa, dict) else {}
        sa_method = (sa_conn.get("method_that_worked") if isinstance(sa_conn, dict) else "") or ""
        anti_bot_detected = (
            (isinstance(sa_anti, dict) and bool(sa_anti.get("detected")))
            or str(sa_method).startswith(("uc_chrome", "cloak"))
        )
        if host:
            pc = (
                ProbeCache.objects.filter(domain__icontains=host)
                .order_by("-last_used_at")
                .first()
            )
            if pc:
                method = opts["probe_method"] or pc.method
                anti_bot_detected = anti_bot_detected or bool(
                    pc.needs_akamai_bypass or pc.captcha_detected
                ) or str(method).startswith(("uc_chrome", "cloak"))
                probe_result = {
                    "method": method,
                    "anti_bot": {"detected": anti_bot_detected},
                    "connectivity": {"method_that_worked": method},
                }
        elif anti_bot_detected:
            probe_result = {
                "method": sa_method,
                "anti_bot": {"detected": True},
                "connectivity": {"method_that_worked": sa_method},
            }

        page_type = (job.page_type if job else "product") or "product"
        cfg = get_content_type(page_type)
        content_type_config = None
        if cfg:
            content_type_config = {
                "content_type": cfg.name,
                "output_key": cfg.output_key,
                "site_type": cfg.site_type,
                "fields": cfg.output_schema.get("fields", []),
            }

        # 4. Reconstruct ScrapeState.
        state: dict = {
            "site_slug": slug,
            "job_id": (job.id if job else 0),
            "url": url,
            "site_name": (site.name if site else (job.site_name if job else "")) or slug,
            "product_url": (site.sample_url if site else None) or (job.product_url if job else "") or "",
            "sample_url": (site.sample_url if site else None) or (job.product_url if job else "") or "",
            "page_type": page_type,
            "input_mode": (job.input_mode if job else "navigation") or "navigation",
            "search_criteria": (job.search_criteria if job else "") or "",
            "currency": (site.currency if site else None) or (job.currency if job else "") or "",
            "site_type": (site.site_type if site else None)
            or (content_type_config or {}).get("site_type")
            or "shopping",
            "scraping_method": (site.scraping_method if site else None)
            or (job.scraping_method if job else "")
            or ((site_analysis or {}).get("site", {}) or {}).get("scraping_mechanism", "")
            or "",
            "content_type_config": content_type_config or {},
            "probe_result": probe_result,
            "site_analysis": site_analysis,
            "navigation_analysis": load_artifact("navigation_analysis.json"),
            "product_analysis": load_artifact("product_analysis.json"),
            "scraper_analysis": load_artifact("scraper_analysis.json"),
            "test_report": load_artifact("test_report.json"),
            "test_retry_count": 0,
            "remap_count": 0,
            "messages": [],
            "agent_logs": [],
        }
        # navigation_findings isn't a TypedDict field but some nodes read it.
        nf = load_artifact("navigation_findings.json")
        if nf:
            state["navigation_findings"] = nf

        # 5. Patch toggle.
        if opts["no_patches"]:
            graph._PATCHES_ENABLED = False
            self.stdout.write(self.style.WARNING("patches DISABLED (--no-patches)"))
        else:
            graph._PATCHES_ENABLED = True

        # 6. Dispatch to the node's _invoke_* function (same code path as production).
        fn = getattr(graph, f"_invoke_{node}", None)
        if fn is None or not callable(fn):
            raise CommandError(f"No _invoke_{node} in webapp.agents.graph")
        config = {"recursion_limit": graph.AGENT_RECURSION_MAP.get(node, graph.AGENT_RECURSION_LIMIT)}

        self.stdout.write(
            f"→ running _invoke_{node}  (slug={slug}, job={state['job_id']}, "
            f"method={(probe_result or {}).get('method')}, anti_bot={bool(probe_result and probe_result['anti_bot']['detected'])})"
        )
        try:
            result = fn(state, config)
        finally:
            graph._PATCHES_ENABLED = True  # always restore

        # 7. Report what changed.
        self.stdout.write(self.style.SUCCESS(f"\n✓ _invoke_{node} returned."))
        if isinstance(result, dict):
            keys = [k for k in result if k not in ("messages", "agent_logs")]
            if keys:
                self.stdout.write(f"  state keys updated: {keys}")

        self.stdout.write("\n=== workspace file changes ===")
        changed = False
        for jf in sorted(workspace.glob("*.json")):
            m = jf.stat().st_mtime
            if before.get(jf.name) != m:
                changed = True
                self.stdout.write(f"  UPDATED {jf.name}  ({jf.stat().st_size} bytes)")
        if not changed:
            self.stdout.write("  (no workspace JSON files changed)")

        # Node-specific summaries.
        self._summarize(node, workspace)

    # ------------------------------------------------------------------
    def _summarize(self, node: str, workspace: Path) -> None:
        """Print a node-specific digest (strategy, coverage, scraper shape)."""
        draft = workspace / "scraper_draft.py"
        if node == "code_writer" and draft.is_file():
            src = draft.read_text(errors="ignore")
            import re as _re

            sb = "seleniumbase" in src.lower() or "from seleniumbase" in src.lower() or "SB(" in src
            pw = "sync_playwright" in src or "chromium.launch" in src
            nets = src.count('"networkidle"') + src.count("'networkidle'")
            sleeps = sorted(set(int(m) for m in _re.findall(r"time\.sleep\((\d+)\)", src)))
            self.stdout.write("\n=== code_writer output digest ===")
            self.stdout.write(f"  seleniumbase markers: {sb}")
            self.stdout.write(f"  playwright markers:   {pw}")
            self.stdout.write(f"  networkidle count:    {nets}")
            self.stdout.write(f"  sleep() values:       {sleeps}")
            self.stdout.write(f"  size:                 {len(src)} bytes")

        if node == "code_tester":
            tr = workspace / "test_report.json"
            if tr.is_file():
                try:
                    rep = json.loads(tr.read_text())
                    self.stdout.write("\n=== code_tester digest ===")
                    self.stdout.write(f"  overall_status: {rep.get('overall_status')}")
                    self.stdout.write(f"  confidence:     {rep.get('confidence_score')}")
                    remediation = rep.get("remediation") or {}
                    if remediation:
                        self.stdout.write(f"  remediation:    target={remediation.get('target')} fields={remediation.get('fields')}")
                    fields = rep.get("fields") or {}
                    for fname, fdata in fields.items():
                        self.stdout.write(f"  field {fname}: {fdata}")
                except Exception as exc:
                    self.stderr.write(f"  warn: test_report parse: {exc}")
