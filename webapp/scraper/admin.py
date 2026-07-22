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

