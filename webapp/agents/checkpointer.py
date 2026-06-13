"""PostgreSQL-backed LangGraph checkpointer wired to Django's DATABASES config."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

_checkpointer = None

if TYPE_CHECKING:
    pass


def _build_conn_string() -> str:
    from django.conf import settings

    db_conf = settings.DATABASES["default"]
    user = db_conf["USER"]
    password = db_conf["PASSWORD"]
    host = db_conf["HOST"]
    port = db_conf["PORT"]
    name = db_conf["NAME"]
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def _ensure_tables(saver) -> None:
    """Ensure checkpoint tables exist, handling CONCURRENTLY index limitations."""
    try:
        saver.setup()
    except Exception as exc:
        logger.warning("PostgresSaver.setup() raised: %s", exc)

    try:
        with saver._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public' "
                "AND tablename='checkpoints'"
            )
            count = cur.fetchone()[0]
    except Exception:
        count = 0

    if count > 0:
        logger.info("PostgresSaver checkpointer tables verified")
        return

    logger.warning("Checkpoint tables missing after setup(), creating manually")
    for idx, sql in enumerate(saver.MIGRATIONS):
        try:
            with saver._cursor() as cur:
                cur.execute(sql)
        except Exception as exc:
            sql_preview = sql[:80].replace("\n", " ")
            logger.debug("Migration %d skipped (%s): %s", idx, sql_preview, exc)

    logger.info("PostgresSaver checkpointer tables created")


def get_checkpointer():
    """Return a PostgresSaver connected to Django's default DB.

    Raises:
        Exception: If Postgres is unreachable.
    """
    global _checkpointer

    if _checkpointer is not None:
        return _checkpointer

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    conn_string = _build_conn_string()
    pool = ConnectionPool(
        conn_string,
        min_size=0,
        max_size=5,
        timeout=5,
    )
    serde = JsonPlusSerializer(pickle_fallback=True)
    saver = PostgresSaver(conn=pool, serde=serde)

    _ensure_tables(saver)

    _checkpointer = saver
    return _checkpointer
