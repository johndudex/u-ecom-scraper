from __future__ import annotations

import logging

from celery import Celery
from celery.signals import worker_ready

logger = logging.getLogger(__name__)

app = Celery("scraper")

app.config_from_object("django.conf.settings", namespace="CELERY")
app.autodiscover_tasks()
# Events dispatcher lives outside tasks.py (scraper/events/) — autodiscover
# only finds <app>.tasks. Explicit import so every worker (scrape + events
# queue) registers deliver_callback/dispatch_pending_callbacks.
app.conf.imports = ("scraper.events.dispatcher", "scraper.events.reconciler")


@worker_ready.connect
def _seed_skills_store(**_kwargs):
    """Idempotently seed the File Master's skills/ namespace from the image.

    worker_ready (NOT AppConfig.ready): beat/flower lack FILE_MASTER_URL —
    an app-level hook would crash their boots; pytest also runs it. The
    seed never clobbers learned content (version-stamped; see
    src/skills_store.seed_from_image). ~15 HEADs + few PUTs once per boot.
    """
    import os

    if not os.environ.get("FILE_MASTER_URL"):
        logger.info("skills seed: FILE_MASTER_URL unset — skipping (beat/flower/test ctx)")
        return
    try:
        from src.skills_store import seed_from_image

        stats = seed_from_image(git_sha=os.environ.get("RAILWAY_GIT_COMMIT_SHA", ""))
        logger.info(
            "skills seed ready: %d seeded, %d refreshed, %d kept-learned",
            len(stats["seeded"]), len(stats["refreshed"]), len(stats["kept_learned"]),
        )
    except Exception as exc:  # never block worker boot on the seed
        logger.warning("skills seed failed (worker continues): %s", exc)


@worker_ready.connect
def _log_async_phase_allowlist(**_kwargs):
    """[wave-15 PR-2b] Log the effective async invoke allowlist at boot.

    Deliberately does NOT import agents.graph (pulls LangGraph + every node
    module into beat/flower boot just for one env read) — the resolution
    logic is 5 lines and mirrored from graph._async_phase_allowlist.
    """
    import os

    from django.conf import settings

    allowlist = sorted(
        p.strip()
        for p in (os.environ.get("AGENT_ASYNC_PHASES") or "").split(",")
        if p.strip()
    )
    override = bool(getattr(settings, "LLM_ASYNC_EXECUTION", False))
    logger.info(
        "async invoke: phases=%s all_phases_override=%s (empty allowlist = "
        "every phase runs sync; canary = AGENT_ASYNC_PHASES=code_writer)",
        allowlist or "none",
        override,
    )


@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
