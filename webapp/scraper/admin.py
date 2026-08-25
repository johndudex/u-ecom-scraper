from django.contrib import admin
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

from .models import (
    Approval,
    AgentPlayground,
    ContentType,
    ProbeCache,
    ScrapeJob,
    SessionLog,
    Site,
    Step,
    ToolCallLog,
)


@admin.register(ContentType)
class ContentTypeAdmin(admin.ModelAdmin):
    list_display = ("group", "value", "label", "enabled", "sort_order")
    list_filter = ("group", "enabled")
    list_editable = ("enabled", "sort_order")
    ordering = ("sort_order", "group", "value")
    search_fields = ("value", "label", "group")


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "url",
        "status",
        "platform",
        "has_scraper",
        "product_count",
    )
    list_filter = ("status", "has_scraper", "platform")
    search_fields = ("url", "name", "slug")
    ordering = ("-created_at",)


@admin.register(ScrapeJob)
class ScrapeJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "url",
        "status",
        "site_name",
        "product_count",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("url", "site_name")
    readonly_fields = (
        "created_at",
        "started_at",
        "completed_at",
        "site_name",
        "platform",
        "scraping_method",
        "product_count",
        "output_file",
        "error_message",
        "duration_seconds",
        "graph_thread_id",
        "celery_task_id",
    )
    ordering = ("-created_at",)

    fieldsets = (
        (None, {"fields": ("url", "product_url", "currency")}),
        ("Status", {"fields": ("status",)}),
        (
            "Results",
            {
                "fields": (
                    "site_name",
                    "platform",
                    "scraping_method",
                    "product_count",
                    "output_file",
                    "site_folder",
                )
            },
        ),
        ("Timing", {"fields": ("created_at", "started_at", "completed_at")}),
        ("Error", {"fields": ("error_message",)}),
    )


@admin.register(Step)
class StepAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "phase", "status", "started_at", "completed_at")
    list_filter = ("status", "phase")
    raw_id_fields = ("job",)


@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "job",
        "approval_type",
        "status",
        "created_at",
        "resolved_at",
    )
    list_filter = ("status", "approval_type")
    actions = ["approve_selected", "reject_selected"]
    readonly_fields = ("created_at", "resolved_at")

    fieldsets = (
        (None, {"fields": ("job", "approval_type", "question", "status")}),
        ("Response", {"fields": ("response_data", "interrupt_value", "human_response")}),
        ("Timing", {"fields": ("created_at", "resolved_at")}),
    )

    def _build_decision(self, approval, approve: bool):
        """Build a decision-dict resume value (consistent with views.py and
        _auto_approve_stale_jobs) from the interrupt's decisions/options.
        Reads `decisions` (current payload) falling back to `options` (legacy).
        Returns (human_response_dict, label_for_display).
        """
        interrupt_data = approval.response_data or {}
        decisions = interrupt_data.get("decisions") or interrupt_data.get("options") or []
        # Cancel-ish labels for reject; approve-ish for approve.
        if approve:
            label = decisions[0] if isinstance(decisions, list) and decisions else "Approve"
        else:
            label = "Cancel"
            if isinstance(decisions, list):
                for d in decisions:
                    if isinstance(d, str) and d.lower() in ("cancel", "abort", "no", "reject", "stop"):
                        label = d
                        break
        return ({"decision": "approve" if approve else "reject",
                 "label": label, "feedback": ""}, label)

    def approve_selected(self, request, queryset):
        for approval in queryset:
            human_response, label = self._build_decision(approval, approve=True)
            # STATUS_APPROVED means "answered" (the resume-targeting lookup in
            # tasks.py filters on it); the approve intent lives in the dict.
            approval.status = Approval.STATUS_APPROVED
            approval.resolved_at = timezone.now()
            approval.human_response = label
            approval.save(update_fields=["status", "resolved_at", "human_response"])
            try:
                from .tasks import resume_scrape_task
                resume_scrape_task.delay(approval.job.id, human_response)
            except Exception as exc:
                logger.warning("approve_selected: resume dispatch failed for job %s: %s", approval.job_id, exc)
        self.message_user(request, f"Approved {queryset.count()} approval(s).")

    approve_selected.short_description = "Approve selected"

    def reject_selected(self, request, queryset):
        for approval in queryset:
            human_response, label = self._build_decision(approval, approve=False)
            # STATUS_APPROVED (NOT REJECTED): the targeted-resume lookup in
            # tasks.py filters on STATUS_APPROVED, so the graph must see this
            # resolved approval to route correctly (reject → re-analyze /
            # cleanup / __end__ depending on the gate). The reject intent
            # lives in the decision dict, not the status. Without dispatch
            # the job hangs in WAITING_APPROVAL forever.
            approval.status = Approval.STATUS_APPROVED
            approval.resolved_at = timezone.now()
            approval.human_response = label
            approval.save(update_fields=["status", "resolved_at", "human_response"])
            try:
                from .tasks import resume_scrape_task
                resume_scrape_task.delay(approval.job.id, human_response)
            except Exception as exc:
                logger.warning("reject_selected: resume dispatch failed for job %s: %s", approval.job_id, exc)
        self.message_user(request, f"Rejected {queryset.count()} approval(s).")

    reject_selected.short_description = "Reject selected"


@admin.register(ToolCallLog)
class ToolCallLogAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "agent", "tool_name", "call_seq", "created_at")
    list_filter = ("agent",)
    raw_id_fields = ("job",)
    search_fields = ("tool_name", "args_summary")


@admin.register(SessionLog)
class SessionLogAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "role", "agent", "seq", "created_at")
    list_filter = ("role", "agent")
    raw_id_fields = ("job",)
    search_fields = ("content", "agent")


@admin.register(ProbeCache)
class ProbeCacheAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "domain",
        "method",
        "needs_akamai_bypass",
        "captcha_detected",
        "cached_at",
        "last_used_at",
    )
    list_filter = ("method", "needs_akamai_bypass", "captcha_detected")
    search_fields = ("domain",)


@admin.register(AgentPlayground)
class AgentPlaygroundAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "agent_name",
        "status",
        "tool_call_count",
        "created_at",
        "completed_at",
    )
    list_filter = ("status", "agent_name")
    search_fields = ("agent_name", "url", "prompt")
    readonly_fields = ("created_at", "started_at", "completed_at")



# ── Partner API (docs/specs/*_api.yaml) ─────────────────────────────────────
from .models import ApiKey, EventOutbox, JobCallback  # noqa: E402


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("prefix", "user", "label", "created_at", "last_used_at", "revoked_at", "is_active")
    search_fields = ("prefix", "user__username", "label")
    list_filter = ("revoked_at",)
    readonly_fields = ("key_hash", "created_at", "last_used_at")

    @admin.display(boolean=True, description="Active")
    def is_active(self, obj):
        return obj.revoked_at is None and obj.user.is_active


@admin.register(JobCallback)
class JobCallbackAdmin(admin.ModelAdmin):
    list_display = ("job", "status", "url", "delivered_count", "last_delivered_at", "disabled_reason")
    list_filter = ("status",)
    search_fields = ("url", "job__id")
    raw_id_fields = ("job",)
    # The secret is RAW at rest (HMAC signing requires it) — never render it.
    exclude = ("secret",)


@admin.register(EventOutbox)
class EventOutboxAdmin(admin.ModelAdmin):
    """The partner event timeline — the support surface for 'what did this
    job's partner receive?' (fold m10)."""

    list_display = ("event_id", "job", "event_type", "state", "attempts", "created_at", "next_attempt_at")
    list_filter = ("state", "event_type")
    search_fields = ("event_id", "job__id")
    raw_id_fields = ("job", "user")
    date_hierarchy = "created_at"
    readonly_fields = ("payload",)


# ── Date-reliability recompute (a66e33f data repair) ────────────────────────
from django.contrib import admin as _admin  # noqa: E402
from django.http import HttpResponseRedirect  # noqa: E402
from django.urls import path, reverse as _reverse  # noqa: E402


@_admin.site.admin_view
def joblisting_recompute(request):
    """Phase 11 §5, as a button: preview (no param) or apply (?write=1).

    Replaces the start-command-flip procedure — the stack runs web-UI-only.
    Superuser-only via admin_view + the staff required check.
    """
    from io import StringIO

    from django.contrib import messages
    from django.core.management import call_command

    if not request.user.is_superuser:
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("superuser only")
    write = request.GET.get("write") == "1"
    out = StringIO()
    call_command("recompute_date_reliability", *(["--write"] if write else []), stdout=out)
    messages.success(request, out.getvalue().strip().replace("\n", " | "))
    return HttpResponseRedirect(_reverse("admin:scraper_joblisting_changelist"))


from .models import JobListing  # noqa: E402


@admin.register(JobListing)
class JobListingAdmin(admin.ModelAdmin):
    list_display = ("title", "site_slug", "posted_date", "date_posted_reliable", "scraped_at")
    list_filter = ("date_posted_reliable", "site_slug")
    search_fields = ("title", "url")
    date_hierarchy = "scraped_at"

    def get_urls(self):
        from django.urls import path

        urls = super().get_urls()
        custom = [
            path(
                "recompute-dates/",
                self.admin_site.admin_view(joblisting_recompute),
                name="admin_joblisting_recompute",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["recompute_url"] = "recompute-dates/"
        return super().changelist_view(request, extra_context)
