"""Present sample products for human approval.

[HIP #7] Runs the scraper on up to 5 sample products from input_urls.json,
presents extracted data, and interrupts for user approval.  On rejection [G1]
loops back to ``product_analyzer`` for re-analysis (max 2 full cycles).
If input_urls.json does not exist, shows fields from product_analysis.json
(the initial product URL analysis).
"""

import json
import logging
import os
import subprocess

from langgraph.types import Command, interrupt

from ..decisions import DECISION_APPROVE, _parse_decision, build_decisions
from ..state import ScrapeState

logger = logging.getLogger(__name__)

MAX_REANALYZE_CYCLES = 2
FIELD_CONFIRMATION_SAMPLE_COUNT = 5

BROWSER_METHODS = {
    "undetected_chromedriver",
    "seleniumbase_uc",
    "playwright",
    "undetected_chromedriver_scraper",
    "stealth_browser",
    "uc_chrome",
}


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _needs_browser_queue(scraper_path: str, scraping_method: str) -> bool:
    if scraping_method in BROWSER_METHODS:
        return True
    try:
        with open(scraper_path, "r", encoding="utf-8") as fh:
            head = fh.read(2000).lower()
        for indicator in ("seleniumbase", "undetected", "uc_open", "uc_mode", "chrome_driver", "selenium"):
            if indicator in head:
                return True
    except Exception:
        pass
    return False


def _build_field_summary_from_analysis(slug: str, root: str) -> str:
    """Build a field summary from product_analysis.json when input_urls.json is missing."""
    analysis_path = os.path.join(root, "workspace", slug, "product_analysis.json")
    if not os.path.isfile(analysis_path):
        return "No product analysis or input URLs available."

    try:
        with open(analysis_path, "r", encoding="utf-8") as fh:
            analysis = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return f"Product analysis file could not be read: {exc}"

    fields = analysis.get("fields", {})
    if not fields:
        return "Product analysis has no field mappings."

    lines = [f"Fields mapped from initial product page ({analysis.get('analyzed_products', '?')} products):"]
    for field_name, field_info in fields.items():
        if isinstance(field_info, dict):
            method = field_info.get("method", "?")
            selector = field_info.get("selector", "")
            examples = field_info.get("examples", [])
            tested = field_info.get("tested", False)
            status = "tested" if tested else "untested"
            line = f"  - {field_name}: [{method}] {selector} ({status})"
            if examples:
                line += f" e.g. {examples[0]}"
            lines.append(line)
        else:
            lines.append(f"  - {field_name}: {field_info}")

    return "\n".join(lines)


def _find_latest_output(slug: str, root: str) -> str | None:
    workspace_dir = os.path.join(root, "workspace", slug)
    if not os.path.isdir(workspace_dir):
        return None
    candidates = sorted(
        [
            os.path.join(workspace_dir, f)
            for f in os.listdir(workspace_dir)
            if f.startswith("output_") and f.endswith(".json")
        ]
    )
    return candidates[-1] if candidates else None


def _format_output_products(output_path: str) -> str:
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return f"(output file could not be read: {exc})"

    products = data.get("products", [])
    if not products:
        return "(scraper produced 0 products)"

    meta = data.get("metadata", {})
    lines = [
        f"Products extracted: {len(products)}",
        f"Duration: {meta.get('scraping_duration_seconds', '?')}s",
        f"Failed: {meta.get('failed_products', '?')}",
        "",
    ]

    for i, p in enumerate(products[:5], 1):
        lines.append(f"--- Product {i} ---")
        for field in ["title", "price", "original_price", "availability", "currency", "url", "brand", "sku"]:
            val = p.get(field, "")
            if val:
                val_str = str(val)[:120]
                lines.append(f"  {field}: {val_str}")
        lines.append("")

    return "\n".join(lines)[:4000]


def field_confirmation(state: ScrapeState) -> Command:
    slug = state["site_slug"]
    root = _get_project_root()
    scraper_path = os.path.join(root, "workspace", slug, "scraper_draft.py")
    input_path = os.path.join(root, "workspace", slug, "input_urls.json")

    if state.get("sample_only", False):
        job_id = state.get("job_id", 0)
        logger.info("field_confirmation: skipping (sample_only mode), going to execution")
        try:
            from django.utils.timezone import now as dj_now
            from scraper.models import Step

            Step.objects.filter(job_id=job_id, phase="field_confirmation").update(
                status=Step.STATUS_DONE, completed_at=dj_now()
            )
            Step.objects.filter(job_id=job_id, phase="execution").update(
                status=Step.STATUS_DONE, completed_at=dj_now()
            )
        except Exception:
            pass
        return Command(goto="run_execution")

    if not os.path.isfile(scraper_path):
        logger.error("field_confirmation: scraper not found at %s", scraper_path)
        return Command(
            update={"error_message": "scraper_draft.py not found"},
            goto="cleanup",
        )

    sample_text = ""

    if os.path.isfile(input_path):
        cmd_args = ["--input", input_path, "--sample", str(FIELD_CONFIRMATION_SAMPLE_COUNT)]

        scraping_method = state.get("scraping_method", "")
        if _needs_browser_queue(scraper_path, scraping_method):
            output = _run_sample_via_queue(scraper_path, cmd_args)
        else:
            output = _run_sample_in_process(scraper_path, cmd_args, root)

        sample_text = output[:3000] if output.strip() else ""

        if not sample_text:
            output_file = _find_latest_output(slug, root)
            if output_file:
                logger.info("field_confirmation: reading output file %s", output_file)
                sample_text = _format_output_products(output_file)
    else:
        logger.info("field_confirmation: input_urls.json not found, running scraper with initial product URL")
        product_url = state.get("product_url", "")
        if product_url:
            cmd_args = ["--urls", product_url]
            scraping_method = state.get("scraping_method", "")
            if _needs_browser_queue(scraper_path, scraping_method):
                output = _run_sample_via_queue(scraper_path, cmd_args)
            else:
                output = _run_sample_in_process(scraper_path, cmd_args, root)
            sample_text = output[:3000] if output.strip() else ""

            if not sample_text:
                output_file = _find_latest_output(slug, root)
                if output_file:
                    logger.info("field_confirmation: reading output file %s", output_file)
                    sample_text = _format_output_products(output_file)

    if not sample_text:
        report = state.get("test_report")
        if report:
            assessment = report.get("overall_assessment", "UNKNOWN")
            issues = report.get("issues", [])
            issue_lines = []
            for issue in issues[:5]:
                severity = issue.get("severity", "?")
                desc = issue.get("description", issue.get("field", "?"))
                issue_lines.append(f"  - [{severity.upper()}] {desc}")
            issue_text = "\n".join(issue_lines) if issue_lines else "  (no issues listed)"
            sample_text = (
                f"Test assessment: {assessment}\n"
                f"Issues found:\n{issue_text}\n\n"
                f"Full test report at: workspace/{slug}/test_report.json"
            )
        else:
            sample_text = _build_field_summary_from_analysis(slug, root)

    _persist_field_confirmation_sample(state.get("job_id", 0), sample_text)

    human_response = interrupt(
        {
            "reason": "field_confirmation",
            "message": (
                "Review the sample extraction below. Approve to proceed "
                "with the full scrape, or reject to re-analyze."
            ),
            "sample_output": sample_text,
            "decisions": build_decisions(
                approve_label="Approve",
                reject_label="Reject",
                reject_with_feedback=True,
            ),
        }
    )

    decision = _parse_decision(human_response)
    feedback = decision.get("feedback", "")

    _persist_field_confirmation_decision(state.get("job_id", 0), decision, feedback)

    if decision.get("decision") == DECISION_APPROVE:
        logger.info("field_confirmation: user approved samples")
        return Command(goto="pre_execution_approval")

    reanalyze = state.get("reanalyze_count", 0) + 1
    logger.info(
        "field_confirmation: user rejected, reanalyze cycle %d/%d (feedback: %s)",
        reanalyze,
        MAX_REANALYZE_CYCLES,
        feedback[:200] if feedback else "(none)",
    )

    if reanalyze < MAX_REANALYZE_CYCLES:
        return Command(
            update={
                "reanalyze_count": reanalyze,
                "human_feedback": feedback,
                "skip_site_analysis": True,
                "skip_product_analysis": False,
                "skip_code_generation": True,
            },
            goto="product_analyzer",
        )

    return Command(
        update={
            "reanalyze_count": reanalyze,
            "human_feedback": feedback,
            "interrupt_reason": "reanalyze_exhausted",
            "interrupt_message": (
                f"Product re-analysis has been attempted {reanalyze} times (max {MAX_REANALYZE_CYCLES}) "
                f"and the results were still rejected. Continue anyway to proceed with the "
                f"current scraper, or Abort to stop."
            ),
            "interrupt_options": ["Continue anyway", "Abort"],
            "interrupt_decisions": build_decisions(
                approve_label="Continue anyway",
                reject_label="Abort",
                reject_with_feedback=False,
            ),
        },
        goto="human_approval",
    )


def _run_sample_in_process(scraper_path: str, args: list[str], cwd: str) -> str:
    cmd = ["python3", scraper_path] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=cwd,
        )
        return result.stdout[:5000]
    except subprocess.TimeoutExpired:
        return "[timeout] scraper timed out on sample run"
    except Exception as exc:
        return f"[error] {exc}"


def _run_sample_via_queue(scraper_path: str, args: list[str]) -> str:
    try:
        import httpx
        import json
        from django.conf import settings

        browser_service_url = getattr(settings, "BROWSER_SERVICE_URL", "http://browser-service:8001")
        resp = httpx.post(
            f"{browser_service_url}/scrape",
            json={
                "scraper_path": scraper_path,
                "args": args,
                "timeout": 300,
            },
            timeout=310,
        )
        data = resp.json()
        return data.get("stdout", "")[:5000]
    except Exception as exc:
        return f"[queue error] {exc}"


def _persist_field_confirmation_sample(job_id: int, sample_text: str) -> None:
    if not job_id:
        return
    try:
        from scraper.models import SessionLog

        seq = SessionLog.objects.filter(job_id=job_id).count()
        SessionLog.objects.create(
            job_id=job_id,
            role=SessionLog.ROLE_SYSTEM,
            agent="field_confirmation",
            content=f"Sample output presented for review:\n\n{sample_text[:4000]}",
            seq=seq,
        )
    except Exception as exc:
        logger.warning("Failed to persist field_confirmation sample for job %s: %s", job_id, exc)


def _persist_field_confirmation_decision(job_id: int, decision: dict, feedback: str) -> None:
    if not job_id:
        return
    try:
        from scraper.models import SessionLog

        choice = decision.get("decision", "unknown")
        lines = [f"User decision: {choice}"]
        if feedback:
            lines.append(f"Feedback: {feedback[:500]}")
        seq = SessionLog.objects.filter(job_id=job_id).count()
        SessionLog.objects.create(
            job_id=job_id,
            role=SessionLog.ROLE_USER,
            agent="field_confirmation",
            content="\n".join(lines),
            seq=seq,
        )
    except Exception as exc:
        logger.warning("Failed to persist field_confirmation decision for job %s: %s", job_id, exc)
