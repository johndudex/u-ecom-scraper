"""[wave-40 T11] Process-local registry of live graph invocations, by site slug.

Why: prod 765 (nike.in) and 807 (papier) lost workspaces mid-run —
``_finalize_job`` rmtree'd ``workspace/{slug}/`` while an abandoned
daemon-thread agent (a wall-clock abandon keeps walking) or a same-slug
sibling job still needed it. The guard that sat at that rmtree was
URL-scoped, slug-blind AND self-excluding: it excluded the job's own row,
which also excluded the job's own zombie.

Deliberately STDLIB-ONLY at import time: graph nodes and celery tasks both
import this module (and in-container ``agents.*`` and ``webapp.agents.*`` are
distinct module objects — every consumer must use the absolute ``agents``
identity). The DB half of ``delete_blocked_reason`` imports django lazily, at
call time.

Semantics:
- ``register(slug, job_id)`` — TASK START, both ``run_scrape_task`` and the
  resume path, so every in-flight generation is counted (including
  soft-limit deaths raised in the task thread). Returns a generation token
  that is ALSO stored in a ContextVar, so the finalize-time disposal can
  clear THIS generation's own entry without touching any other entry that
  shares the ``(slug, job_id)``.
- ``unregister(slug, job_id, token)`` — the task's outermost finally, AFTER
  finalize; discards exactly that token.
- ``register_abandoned(slug, job_id)`` — the agent-abandonment sites in
  ``graph._invoke_agent_with_timeout``. An abandoned daemon walk keeps
  running after its task's token is unregistered, so NOTHING discards these
  entries: they block until process restart / the ``_trash`` retention sweep.
- ``alive_for_slug(slug)`` — any entry at all. It must NOT special-case job
  ids: the 765 zombie shares the resumed job's id, and an entry under the
  same ``(slug, job_id)`` from an abandoned generation still counts.
- ``delete_blocked_reason(slug, exclude_job_id)`` — the shared guard:
  ``""`` = may delete, else the reason. ``exclude_job_id`` only ever
  whitelists the CALLER'S OWN current generation (token-matched) — never an
  abandoned entry, and never another job's.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger(__name__)

#: System namespace under ``workspace/`` — never a site slug, never auto-managed.
TRASH_DIRNAME = "_trash"

_lock = threading.Lock()
# slug -> {(job_id, token), ...}: live task generations.
_live: dict[str, set[tuple[int, str]]] = {}
# slug -> {job_id, ...}: abandoned walks. Nothing ever removes from here.
_abandoned: dict[str, set[int]] = {}

# The calling task's own generation token, set by ``register`` at task start
# and read by the finalize-time disposal (same thread ⇒ same context).
_current_token: contextvars.ContextVar[str] = contextvars.ContextVar(
    "wave40-generation-token", default=""
)


def register(slug: str, job_id: int) -> str:
    """Count this task generation as live for *slug*; return its token."""
    if not slug:
        return ""
    token = uuid.uuid4().hex
    with _lock:
        _live.setdefault(slug, set()).add((int(job_id), token))
    _current_token.set(token)
    return token


def unregister(slug: str, job_id: int, token: str) -> None:
    """Discard exactly *token* (a stale/unknown token is a no-op)."""
    if not slug or not token:
        return
    with _lock:
        entries = _live.get(slug)
        if entries is not None:
            entries.discard((int(job_id), token))
            if not entries:
                _live.pop(slug, None)


def register_abandoned(slug: str, job_id: int) -> None:
    """Record a walk that outlived its wall clock and can never be cleared."""
    if not slug:
        return
    with _lock:
        _abandoned.setdefault(slug, set()).add(int(job_id))
    logger.warning(
        "invocation_registry: ABANDONED walk registered for '%s' (job %s) — "
        "its workspace stays guarded until restart/purge", slug, job_id,
    )


def clear_own(slug: str, job_id: int, token: str) -> None:
    """Read-and-clear of THIS generation's own entry (finalize is post-graph).

    Token-scoped on purpose: an abandoned generation registered under the
    same ``(slug, job_id)`` survives, because its token is a different one.
    """
    unregister(slug, job_id, token)


def current_token() -> str:
    """This context's generation token ('' outside a registered task)."""
    return _current_token.get()


def alive_for_slug(slug: str) -> bool:
    """Any entry for *slug* — live or abandoned, any job id."""
    with _lock:
        return bool(_live.get(slug)) or bool(_abandoned.get(slug))


def delete_blocked_reason(slug: str, exclude_job_id: int | None = None) -> str:
    """``""`` = the caller may delete ``workspace/{slug}/``; else the reason."""
    if not slug or slug.startswith("_"):
        return f"'{slug or ''}' is system namespace — never auto-managed"
    own_reason = _own_registry_reason(slug, exclude_job_id)
    if own_reason:
        return own_reason
    return _db_sibling_reason(slug, exclude_job_id)


def _own_registry_reason(slug: str, exclude_job_id: int | None) -> str:
    with _lock:
        abandoned = _abandoned.get(slug)
        if abandoned:
            return (
                f"an abandoned walk is still registered for '{slug}' "
                f"(job {sorted(abandoned)}) — it outlives every token"
            )
        own = (
            (int(exclude_job_id), _current_token.get())
            if exclude_job_id is not None else None
        )
        for job_id, _token in sorted(_live.get(slug, ())):
            if own is not None and (job_id, _token) == own:
                continue  # the caller's own generation is asking
            return f"live invocation registered for '{slug}' (job {job_id})"
    return ""


def _db_sibling_reason(slug: str, exclude_job_id: int | None) -> str:
    """The slug-scoped DB sibling check the old URL filter should have been.

    Failure here FAILS OPEN: the in-process liveness leg is the load-bearing
    one, and the wipe sites must stay usable where no DB is configured.
    """
    try:
        from scraper.models import ScrapeJob
        from scraper.tasks import _generate_slug
    except Exception as exc:
        logger.warning("invocation_registry: sibling check unavailable (%s)", exc)
        return ""
    live = (
        ScrapeJob.STATUS_RUNNING,
        ScrapeJob.STATUS_PENDING,
        ScrapeJob.STATUS_WAITING_APPROVAL,
    )
    try:
        rows = ScrapeJob.objects.filter(status__in=live).values_list(
            "id", "status", "url"
        )
        for job_id, status, url in rows:
            if exclude_job_id is not None and job_id == exclude_job_id:
                continue
            if _generate_slug(url or "") == slug:
                return f"job {job_id} is still {status} for slug '{slug}'"
    except Exception as exc:
        logger.warning("invocation_registry: sibling check failed (%s)", exc)
        return ""
    return ""


# ─── the tombstone (same-filesystem rename into workspace/_trash/) ──────────


def trash_name(slug: str, job_id: int | None = None) -> str:
    """``{slug}-{job_id}-{ns-since-epoch}`` — the stamp is the retention key."""
    return f"{slug or 'unknown'}-{int(job_id or 0)}-{time.time_ns()}"


def parse_trash_name(name: str) -> tuple[str, int, float] | None:
    """Inverse of :func:`trash_name` → ``(slug, job_id, epoch_seconds)``,
    or ``None`` for a name this module did not write."""
    parts = (name or "").rsplit("-", 2)
    if len(parts) != 3:
        return None
    slug, job_id, stamp = parts
    if not (job_id.isdigit() and stamp.isdigit()):
        return None
    return slug, int(job_id), int(stamp) / 1e9


def tombstone_into_trash(path: str | os.PathLike, slug: str,
                         job_id: int | None = None) -> str:
    """Rename *path* to ``<parent>/_trash/{name}`` — preserve, never destroy.

    Same-filesystem by construction (both sides under the same parent), so
    the rename cannot cross devices. Returns the destination path, ``""``
    when there was nothing to move or the rename failed.
    """
    src = os.fspath(path)
    if not os.path.isdir(src):
        return ""
    try:
        parent = os.path.dirname(src.rstrip(os.sep)) or os.curdir
        trash_root = os.path.join(parent, TRASH_DIRNAME)
        os.makedirs(trash_root, exist_ok=True)
        dst = os.path.join(trash_root, trash_name(slug, job_id))
        os.rename(src, dst)
        return dst
    except OSError as exc:
        logger.warning("invocation_registry: tombstone of %s failed: %s", src, exc)
        return ""


def _reset_for_tests() -> None:
    """Test-support: empty the process-local store (nothing else may)."""
    with _lock:
        _live.clear()
        _abandoned.clear()
    _current_token.set("")
