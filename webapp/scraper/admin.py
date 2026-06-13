from django.contrib import admin
from django.utils import timezone

from .models import Approval, ScrapeJob, Site, Step, ToolCallLog


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "slug",
        "url",
        "status",
        "platform",
        "has_scraper",
        "product_count",
        "last_scraped_at",
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

    def approve_selected(self, request, queryset):
        for approval in queryset:
            approval.status = Approval.STATUS_APPROVED
            approval.resolved_at = timezone.now()

            interrupt_data = approval.response_data or {}
            options = interrupt_data.get("options", [])
            if len(options) == 1:
                human_response = {"choice": options[0]}
            else:
                human_response = {"choice": "Approve"}

            approval.human_response = human_response.get("choice", "Approve")
            approval.save(update_fields=["status", "resolved_at", "human_response"])
            try:
                from .tasks import resume_scrape_task
                resume_scrape_task.delay(approval.job.id, human_response)
            except Exception:
                pass
        self.message_user(request, f"Approved {queryset.count()} approval(s).")

    approve_selected.short_description = "Approve selected"

    def reject_selected(self, request, queryset):
        for approval in queryset:
            approval.status = Approval.STATUS_REJECTED
            approval.resolved_at = timezone.now()

            interrupt_data = approval.response_data or {}
            options = interrupt_data.get("options", [])
            cancel_label = "Cancel" if "Cancel" in options else "Abort"
            human_response = {"choice": cancel_label}

            approval.human_response = cancel_label
            approval.save(update_fields=["status", "resolved_at", "human_response"])
        self.message_user(request, f"Rejected {queryset.count()} approval(s).")

    reject_selected.short_description = "Reject selected"


@admin.register(ToolCallLog)
class ToolCallLogAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "agent", "tool_name", "call_seq", "created_at")
    list_filter = ("agent",)
    raw_id_fields = ("job",)
    search_fields = ("tool_name", "args_summary")
