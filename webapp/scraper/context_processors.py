import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)


def _safe_count(qs):
    try:
        return qs.count()
    except Exception as e:
        logger.debug(f"dashboard_stats count failed: {e}")
        return 0


def dashboard_stats(request):
    """Inject dashboard stats and lists into every admin/template context.

    Cheap queries (counts) and small lists (last 8 jobs, last 8 sites).
    Wrapped in try/except so admin never breaks if the DB is down.
    """
    if not request.path.startswith("/admin") and not request.path.startswith("/"):
        return {}

    ctx = {
        "dash_total_sites": 0,
        "dash_total_jobs": 0,
        "dash_active_jobs": 0,
        "dash_pending_approvals": 0,
        "dash_completed_today": 0,
        "dash_failed_today": 0,
        "dash_jobs_today": 0,
        "dash_recent_jobs": [],
        "dash_recent_sites": [],
    }

    try:
        from .models import Approval, ScrapeJob, Site

        ctx["dash_total_sites"] = _safe_count(Site.objects.all())
        ctx["dash_total_jobs"] = _safe_count(ScrapeJob.objects.all())
        ctx["dash_active_jobs"] = _safe_count(
            ScrapeJob.objects.filter(
                status__in=[
                    ScrapeJob.STATUS_RUNNING,
                    ScrapeJob.STATUS_PENDING,
                    ScrapeJob.STATUS_WAITING_APPROVAL,
                ]
            )
        )
        ctx["dash_pending_approvals"] = _safe_count(
            Approval.objects.filter(status=Approval.STATUS_PENDING)
        )

        now = timezone.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ctx["dash_jobs_today"] = _safe_count(
            ScrapeJob.objects.filter(created_at__gte=start_of_day)
        )
        ctx["dash_completed_today"] = _safe_count(
            ScrapeJob.objects.filter(
                status=ScrapeJob.STATUS_COMPLETED, completed_at__gte=start_of_day
            )
        )
        ctx["dash_failed_today"] = _safe_count(
            ScrapeJob.objects.filter(
                status=ScrapeJob.STATUS_FAILED, completed_at__gte=start_of_day
            )
        )

        ctx["dash_recent_jobs"] = list(
            ScrapeJob.objects.all().order_by("-created_at")[:8].values(
                "id",
                "url",
                "status",
                "site_name",
                "page_type",
                "created_at",
                "auto_queued",
            )
        )
        ctx["dash_recent_sites"] = list(
            Site.objects.all().order_by("-created_at")[:8].values(
                "id", "name", "slug", "url", "status", "site_type", "platform"
            )
        )
    except Exception as e:
        logger.debug(f"dashboard_stats query failed: {e}")

    return ctx
