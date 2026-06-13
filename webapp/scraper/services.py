"""Service layer for LangGraph graph execution and event streaming.

Public API
----------
* ``LangGraphService.build_graph()``        — compile the scrape StateGraph
* ``LangGraphService.stream_graph()``       — execute with astream_events
* ``LangGraphService.interrupt_to_approval()`` — map interrupt → Approval

Redis pub/sub channel ``job:{job_id}`` is used to push real-time events to
the SSE layer in views.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.graph.state import CompiledStateGraph

from django.conf import settings

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Lazy Redis connection (shared for the process lifetime)
# ═══════════════════════════════════════════════════════════════════════════

_redis_client: Any = None


def _get_redis() -> Any:
    """Return a shared ``redis.Redis`` instance, creating it on first call."""
    global _redis_client
    if _redis_client is None:
        import redis as redis_lib

        _redis_client = redis_lib.from_url(settings.CELERY_BROKER_URL)
    return _redis_client


# ═══════════════════════════════════════════════════════════════════════════
# Interrupt → Approval type mapping [D2]
# ═══════════════════════════════════════════════════════════════════════════

INTERRUPT_TO_APPROVAL_TYPE: dict[str, str] = {
    "re_scrape": "re_scrape",
    "retry_failed": "re_scrape",
    "low_confidence": "confidence",
    "choose_mechanism": "mechanism",
    "low_coverage": "field_coverage",
    "validation_failed": "validation",
    "field_confirmation": "field_confirm",
    "pre_execution": "execution",
    "reanalyze_exhausted": "validation",
    "skill_approval": "skill_update",
}

# ═══════════════════════════════════════════════════════════════════════════
# LangGraphService
# ═══════════════════════════════════════════════════════════════════════════


class _ScrapeCallbackHandler(BaseCallbackHandler):
    """Callback handler that captures LLM/tool/chain events into SessionLog."""

    _NODE_DISPLAY: dict[str, str] = {
        "site_analyzer": "site-analyzer",
        "product_analyzer": "product-analyzer",
        "scraper_analyzer": "scraper-analyzer",
        "code_writer": "code-writer",
        "code_tester": "code-tester",
        "cleanup": "cleanup",
        "skill_learner": "skill-learner",
    }

    def __init__(self, job: Any) -> None:
        self.job = job
        self._pending_tool_name: str | None = None
        self._current_node: str = ""

    def on_llm_end(self, response: Any, *, run_id: str, parent_run_id: str | None = None, **kwargs: Any) -> None:
        content = ""
        if hasattr(response, "generations") and response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    if hasattr(gen, "text"):
                        content += gen.text or ""
        if not content:
            return

        from scraper.models import SessionLog

        agent = self._NODE_DISPLAY.get(self._current_node, "")
        seq = SessionLog.objects.filter(job=self.job).count()
        SessionLog.objects.create(
            job=self.job,
            role=SessionLog.ROLE_ASSISTANT,
            agent=agent,
            content=content[:3000],
            seq=seq,
        )
        LangGraphService._publish_redis(
            self.job.id,
            {
                "type": "log",
                "seq": seq,
                "role": "assistant",
                "agent": agent,
                "content": content[:500],
            },
        )

    def on_tool_start(self, serialized: dict, input_str: Any, *, run_id: str, parent_run_id: str | None = None, tags: list[str] | None = None, metadata: dict | None = None, run_name: str | None = None, **kwargs: Any) -> None:
        name = serialized.get("name", "unknown_tool") if isinstance(serialized, dict) else str(serialized)
        self._pending_tool_name = name

        from scraper.models import SessionLog

        seq = SessionLog.objects.filter(job=self.job).count()
        SessionLog.objects.create(
            job=self.job,
            role=SessionLog.ROLE_TOOL,
            agent=name,
            content=f"[calling {name}]",
            seq=seq,
        )

    def on_tool_end(self, output: Any, *, run_id: str, parent_run_id: str | None = None, **kwargs: Any) -> None:
        tool_name = self._pending_tool_name or "unknown_tool"
        output_str = str(output)
        content = f"[{tool_name}]"
        if len(output_str) < 1500:
            content += f" {output_str[:1500]}"
        else:
            content += f" (output: {len(output_str)} bytes)"

        from scraper.models import SessionLog

        seq = SessionLog.objects.filter(job=self.job).count()
        SessionLog.objects.create(
            job=self.job,
            role=SessionLog.ROLE_TOOL,
            agent=tool_name,
            content=content[:20000],
            seq=seq,
        )
        self._pending_tool_name = None

    def on_chain_start(self, serialized: dict, inputs: dict, *, run_id: str, parent_run_id: str | None = None, tags: list[str] | None = None, metadata: dict | None = None, run_name: str | None = None, **kwargs: Any) -> None:
        node = run_name or serialized.get("name", "") if isinstance(serialized, dict) else ""
        self._current_node = node
        phase = LangGraphService._node_to_phase(node)
        if phase:
            _upsert_step_from_event(self.job, phase, "running")

    def on_chain_end(self, outputs: dict, *, run_id: str, parent_run_id: str | None = None, **kwargs: Any) -> None:
        pass


class LangGraphService:
    """Manage LangGraph graph lifecycle: compile, stream, interrupt, resume.

    All public methods are ``@staticmethod`` so callers don't need an
    instance.  The class acts as a namespace for graph-related operations.
    """

    # ── Graph construction ──────────────────────────────────────────────

    @staticmethod
    def build_graph() -> "CompiledStateGraph":
        """Build and compile the scrape graph with a PostgreSQL checkpointer.

        The checkpointer instance is obtained from ``agents.checkpointer`` and
        cached by ``lru_cache``, so repeated calls are cheap.
        """
        from agents.checkpointer import get_checkpointer
        from agents.graph import build_scrape_graph

        checkpointer = get_checkpointer()
        return build_scrape_graph(checkpointer=checkpointer)

    # ── Config helpers ──────────────────────────────────────────────────

    @staticmethod
    def get_thread_id(job_id: int) -> str:
        """Return the LangGraph thread ID for a given ScrapeJob pk."""
        return f"job-{job_id}"

    @staticmethod
    def get_config(job_id: int) -> dict[str, Any]:
        """Return the LangGraph ``config`` dict for a given ScrapeJob pk."""
        return {
            "configurable": {"thread_id": LangGraphService.get_thread_id(job_id)},
            "recursion_limit": 50,
        }

    # ── Execution ──────────────────────────────────────────────────

    @staticmethod
    def stream_graph(
        graph: CompiledStateGraph,
        initial_state: dict[str, Any],
        config: dict[str, Any],
        job: Any,
    ) -> None:
        """Run the graph synchronously and log events via callback.

        Uses ``graph.invoke()`` with a callback handler that captures LLM
        messages and tool calls into SessionLog rows.  Agent messages are
        also persisted directly by the wrapper nodes in ``graph.py``.
        """
        try:
            handler = _ScrapeCallbackHandler(job)
            graph.invoke(
                initial_state,
                config,
                callbacks=[handler],
            )
        except Exception as exc:
            from langgraph.errors import GraphInterrupt

            if isinstance(exc, GraphInterrupt):
                logger.info("Job %d: GraphInterrupt caught", job.id)
            else:
                logger.error("Job %d: graph execution failed: %s", job.id, exc)
                raise

        LangGraphService._check_and_create_approval(graph, config, job)

    # ── Interrupt handling [D2] ─────────────────────────────────────────

    @staticmethod
    def _check_and_create_approval(
        graph: CompiledStateGraph,
        config: dict[str, Any],
        job: Any,
    ) -> bool:
        """Inspect the checkpoint for interrupt data and create an Approval.

        Returns ``True`` if an interrupt was found, ``False`` otherwise.
        Called after ``astream_events`` completes to detect human-in-the-loop
        pauses.
        """
        from scraper.models import Approval, SessionLog

        try:
            snapshot = graph.get_state(config)
        except Exception as exc:
            logger.warning("Job %d: could not read graph state: %s", job.id, exc)
            return False

        found_interrupt = False

        for task in getattr(snapshot, "tasks", []):
            interrupts = getattr(task, "interrupts", [])
            for interrupt_value in interrupts:
                if not isinstance(interrupt_value, dict):
                    if hasattr(interrupt_value, "value"):
                        interrupt_value = interrupt_value.value
                    else:
                        interrupt_value = {"value": interrupt_value}

                if not isinstance(interrupt_value, dict):
                    interrupt_value = {"value": str(interrupt_value)}

                approval = LangGraphService.interrupt_to_approval(
                    interrupt_value, job
                )
                found_interrupt = True

                # System log so the UI shows what was asked.
                seq = SessionLog.objects.filter(job=job).count()
                question_preview = interrupt_value.get("question", interrupt_value.get("message", ""))
                SessionLog.objects.create(
                    job=job,
                    role=SessionLog.ROLE_SYSTEM,
                    agent="human_approval",
                    content=(
                        f"⏸️ Waiting for approval: {str(question_preview)[:500]}"
                    ),
                    seq=seq,
                )
                LangGraphService._publish_redis(
                    job.id,
                    {
                        "type": "approval",
                        "approval_id": approval.id,
                        "approval_type": approval.approval_type,
                        "question": approval.question,
                        "options": interrupt_value.get("options", []),
                    },
                )

        return found_interrupt

    @staticmethod
    def interrupt_to_approval(interrupt_data: dict[str, Any], job: Any) -> Any:
        """Map a LangGraph ``interrupt()`` payload to a Django ``Approval``.

        The ``interrupt_data`` dict is expected to have at least a ``reason``
        key whose value maps through ``INTERRUPT_TO_APPROVAL_TYPE``.
        """
        from scraper.models import Approval

        reason = interrupt_data.get("reason", "")
        approval_type = INTERRUPT_TO_APPROVAL_TYPE.get(reason, Approval.TYPE_EXECUTION)

        question = str(
            interrupt_data.get("question", interrupt_data.get("message", ""))
        )[:2000]

        approval = Approval.objects.create(
            job=job,
            approval_type=approval_type,
            question=question,
            response_data=interrupt_data,
            status=Approval.STATUS_PENDING,
        )
        logger.info(
            "Job %d: created Approval %d (type=%s, reason=%s)",
            job.id,
            approval.id,
            approval_type,
            reason,
        )
        return approval

    # ── Redis pub/sub for SSE [D4] ──────────────────────────────────────

    @staticmethod
    def _publish_redis(job_id: int, payload: dict[str, Any]) -> None:
        """Publish a JSON payload to ``job:{job_id}`` Redis channel.

        Consumed by the SSE endpoint in views.py (Phase 9) to push real-time
        updates to the browser.
        """
        try:
            r = _get_redis()
            channel = f"job:{job_id}"
            r.publish(channel, json.dumps(payload, default=str))
        except Exception as exc:
            # Redis pub/sub is best-effort — never block the scrape.
            logger.debug("Redis publish failed for job %d: %s", job_id, exc)

    # ── Phase mapping helpers ────────────────────────────────────────────

    NODE_PHASE_MAP: dict[str, str] = {
        "site_analyzer": "site_analysis",
        "product_analyzer": "product_analysis",
        "scraper_analyzer": "scraper_analysis",
        "code_writer": "code_generation",
        "code_tester": "testing",
        "cleanup": "cleanup",
        "skill_learner": "skill_learning",
    }

    @staticmethod
    def _node_to_phase(node_name: str) -> Optional[str]:
        """Map a LangGraph node name to a Step phase string."""
        return LangGraphService.NODE_PHASE_MAP.get(node_name)


# ═══════════════════════════════════════════════════════════════════════════
# Step helper (module-level so both services and tasks can import it)
# ═══════════════════════════════════════════════════════════════════════════


def _upsert_step_from_event(job: Any, phase: str, status: str, notes: str = "") -> None:
    """Create or update a Step row for *job*."""
    from scraper.models import Step

    from django.utils import timezone

    step, _created = Step.objects.get_or_create(
        job=job, phase=phase, defaults={"notes": notes}
    )
    if step.status != status:
        step.status = status
    if status == Step.STATUS_DONE and not step.completed_at:
        step.completed_at = timezone.now()
    elif status == Step.STATUS_RUNNING and not step.started_at:
        step.started_at = timezone.now()
    if notes:
        step.notes = notes
    step.save()
