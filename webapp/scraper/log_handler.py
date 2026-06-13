import json
import logging
import threading

_logger = logging.getLogger(__name__)

_thread_local = threading.local()


class _JobLogFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        job_id = getattr(_thread_local, "job_id", None)
        if job_id is None:
            return False
        record._syslog_job_id = job_id
        return True


class RedisLogHandler(logging.Handler):

    _filter_instance = _JobLogFilter()

    ALLOWED_LOGGERS = frozenset({
        "agents",
        "agents.graph",
        "agents.nodes",
        "agents.tools",
        "agents.subagents",
        "scraper",
        "scraper.tasks",
        "scraper.services",
    })

    def __init__(self) -> None:
        super().__init__()
        self.setLevel(logging.INFO)
        self.addFilter(self._filter_instance)

    def emit(self, record: logging.LogRecord) -> bool:
        name_parts = record.name.split(".")
        base = name_parts[0] if name_parts else ""
        if base not in self.ALLOWED_LOGGERS and record.name not in self.ALLOWED_LOGGERS:
            return

        try:
            from .services import _get_redis

            r = _get_redis()
            job_id = getattr(record, "_syslog_job_id", 0)
            if not job_id:
                return

            payload = {
                "type": "syslog",
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
                "created": record.created,
            }
            r.publish(f"job:{job_id}:syslog", json.dumps(payload, default=str))
        except Exception:
            pass

    def format(self, record: logging.LogRecord) -> str:
        return self.formatter.format(record) if self.formatter else record.getMessage()

    @staticmethod
    def set_job_id(job_id: int) -> None:
        _thread_local.job_id = job_id

    @staticmethod
    def clear_job_id() -> None:
        _thread_local.job_id = None
