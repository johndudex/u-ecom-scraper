from django.conf import settings
from django.db import models
from pathlib import Path
import re
from urllib.parse import urlparse


def _site_type_choices():
    try:
        from src.content_types import SITE_TYPE_CHOICES as choices

        return choices
    except ImportError:
        return [
            ("shopping", "Shopping"),
            ("articles", "Articles"),
            ("jobs", "Jobs"),
            ("forum", "Forum"),
            ("general", "General"),
        ]


def _input_mode_choices():
    try:
        from src.content_types import INPUT_MODE_CHOICES as choices

        return choices
    except ImportError:
        return [
            ("url_list", "URL List"),
            ("list_page", "List Page"),
            ("navigation", "Navigation"),
            ("search_term", "Search Term"),
        ]


def _normalize_url(url: str) -> str:
    if not url:
        return url
    p = urlparse(url)
    clean_path = re.sub(r"/{2,}", "/", p.path)
    return p._replace(path=clean_path).geturl()


def _sync_input_urls_file(instance):
    urls = instance.input_urls or []
    if not urls or not instance.slug:
        return
    try:
        from django.conf import settings

        import json

        import logging

        file_path = (
            Path(settings.PROJECT_ROOT) / "scrapers" / instance.slug / "input_urls.json"
        )
        # Shrinkage guard: never overwrite the production file with FEWER urls
        # than it already has. A job can briefly hold a subset (e.g. a 1-URL
        # sample) on the Site row; syncing that would silently truncate the
        # user's full list (the wildsecrets 50→1 / dollartree 5→1 / vistastaff
        # 5→1 truncation). Skip + log instead.
        if file_path.exists():
            try:
                with open(file_path, encoding="utf-8") as _f:
                    _existing = json.load(_f).get("urls") or []
                if len(_existing) > len(urls):
                    logging.getLogger("scraper.models").warning(
                        "input_urls sync skipped for %s: file has %d urls, new list "
                        "has %d (refusing to shrink the production file)",
                        instance.slug, len(_existing), len(urls),
                    )
                    return
            except Exception:
                pass
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


class ContentType(models.Model):
    value = models.CharField(max_length=50, unique=True)
    label = models.CharField(max_length=100)
    group = models.CharField(max_length=50)
    enabled = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        app_label = 'scraper'
        ordering = ["sort_order", "group", "value"]
        verbose_name = "Content Type"
        verbose_name_plural = "Content Types"

    def __str__(self):
        return f"{self.group} > {self.label}"


class ScrapeJob(models.Model):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_WAITING_APPROVAL = "waiting_approval"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CAPTCHA_BLOCKED = "captcha_blocked"
    STATUS_AKAMAI_BLOCKED = "akamai_blocked"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_WAITING_APPROVAL, "Waiting for Approval"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_CAPTCHA_BLOCKED, "Captcha Blocked"),
        (STATUS_AKAMAI_BLOCKED, "Akamai Blocked"),
    ]

    url = models.URLField()
    product_url = models.URLField(max_length=1000, blank=True, default="")
    currency = models.CharField(max_length=10, blank=True, default="")
    page_type = models.CharField(max_length=30, default="product")
    input_mode = models.CharField(
        max_length=15,
        choices=_input_mode_choices(),
        default="url_list",
    )
    search_criteria = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    graph_thread_id = models.CharField(max_length=100, blank=True, default="")
    celery_task_id = models.CharField(max_length=100, blank=True, default="")

    site_name = models.CharField(max_length=200, blank=True, default="")
    platform = models.CharField(max_length=100, blank=True, default="")
    scraping_method = models.CharField(max_length=100, blank=True, default="")
    product_count = models.IntegerField(default=0)
    output_file = models.CharField(max_length=500, blank=True, default="")
    # Per-job scraper/dagster artifact paths (attributed — each job remembers its
    # own generated files, independent of the shared scrapers/{slug}/scraper.py
    # which later jobs can overwrite). [per-job-attribution]
    scraper_file = models.CharField(max_length=500, blank=True, default="")
    dagster_file = models.CharField(max_length=500, blank=True, default="")
    site_folder = models.CharField(max_length=500, blank=True, default="")
    full_extraction = models.BooleanField(default=False)
    auto_queued = models.BooleanField(default=False)
    # Jobs created from the intake UI skip ALL human-approval gates (they run
    # unattended); homepage jobs keep the approval stage. [intake-ui]
    skip_approvals = models.BooleanField(default=False)
    # User-facing display name (rename) + library bookmark. Title falls back to
    # "Job #<id>" in the UI when empty. [intake-revision]
    title = models.CharField(max_length=200, blank=True, default="")
    is_saved = models.BooleanField(default=False)
    # Per-user ownership (nullable for system/auto-queued jobs).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="scrape_jobs",
    )

    # Intake UI (templates/scraper-intake.html) — user-facing knobs surfaced to
    # product_analyzer / code_writer as advisory hints. All default empty so
    # legacy jobs and the home view are unaffected. [intake-ui]
    target_fields = models.JSONField(default=list, blank=True)
    scope = models.CharField(max_length=20, blank=True, default="")
    scope_value = models.CharField(max_length=200, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    # search_term jobs: the search results page URL the user entered (distinct
    # from `search_criteria`, which holds the keywords). [intake-ui]
    search_url = models.URLField(max_length=1000, blank=True, default="")

    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.url} ({self.status})"

    @property
    def duration_seconds(self):
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return 0

    @property
    def page_type_display(self) -> str:
        labels = {
            "product": "Product",
            "product_list": "Product List",
            "product_navigation": "Product Navigation",
            "article": "Article",
            "article_list": "Article List",
            "article_navigation": "Article Navigation",
            "job_posting": "Job Posting",
            "job_navigation": "Job Navigation",
            "forum_thread": "Forum Thread",
            "serp": "SERP",
            "page_content": "Page Content",
        }
        return labels.get(self.page_type, self.page_type)


class Step(models.Model):
    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_DONE, "Done"),
        (STATUS_FAILED, "Failed"),
    ]

    PHASE_CHOICES = [
        ("accessibility_check", "Accessibility Check"),
        ("site_analysis", "Site Analysis"),
        ("browser_traverse", "Browser Navigation"),
        ("navigation_skill_review", "Navigation Skill Review"),
        ("navigation_analysis", "Navigation Analysis"),
        ("content_analysis", "Content Analysis"),
        ("product_analysis", "Product Analysis"),
        ("scraper_analysis", "Scraper Analysis"),
        ("code_generation", "Code Generation"),
        ("code_review", "Code Review"),
        ("testing", "Testing"),
        ("field_confirmation", "Field Confirmation"),
        ("execution", "Execution"),
        ("cleanup", "Cleanup"),
        ("skill_learning", "Skill Learning"),
        ("dagster_converter", "Dagster Conversion"),
        ("store_job_listings", "Store Listings"),
    ]

    job = models.ForeignKey(ScrapeJob, related_name="steps", on_delete=models.CASCADE)
    phase = models.CharField(max_length=50, choices=PHASE_CHOICES)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    notes = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.job.id}/{self.phase} ({self.status})"


class Approval(models.Model):
    TYPE_RESCRAPE = "re_scrape"
    TYPE_CONFIDENCE = "confidence"
    TYPE_MECHANISM = "mechanism"
    TYPE_FIELD_COVERAGE = "field_coverage"
    TYPE_VALIDATION = "validation"
    TYPE_FIELD_CONFIRM = "field_confirm"
    TYPE_EXECUTION = "execution"
    TYPE_SKILL_UPDATE = "skill_update"

    TYPE_CHOICES = [
        (TYPE_RESCRAPE, "Re-scrape Confirmation"),
        (TYPE_CONFIDENCE, "Low Confidence Warning"),
        (TYPE_MECHANISM, "Scraping Mechanism Choice"),
        (TYPE_FIELD_COVERAGE, "Low Field Coverage"),
        (TYPE_VALIDATION, "Validation Retry/Fail"),
        (TYPE_FIELD_CONFIRM, "Field Confirmation"),
        (TYPE_EXECUTION, "Execution Approval"),
        (TYPE_SKILL_UPDATE, "Skill File Update"),
    ]

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_SUPERSEDED = "superseded"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_SUPERSEDED, "Superseded (job ended)"),
    ]

    job = models.ForeignKey(
        ScrapeJob, related_name="approvals", on_delete=models.CASCADE
    )
    approval_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    question = models.TextField(blank=True, default="")
    response_data = models.JSONField(null=True, blank=True, default=dict)
    # The LangGraph interrupt_id this approval corresponds to. Needed to
    # resume ONLY this specific interrupt (not stale ones from earlier nodes
    # that accumulate in the checkpoint). Set when _check_and_create_approval
    # creates the approval from the snapshot.
    interrupt_id = models.CharField(max_length=200, blank=True, default="", db_index=True)
    # P0-10: stuck-approved watchdog fields. resume_value stores the exact
    # decision dict passed to resume_scrape_task (so the watchdog can replay
    # it if the resume failed to consume the interrupt). resume_attempts
    # counts watchdog re-dispatches (capped at STUCK_APPROVED_MAX_RETRIES).
    resume_value = models.JSONField(null=True, blank=True, default=dict)
    resume_attempts = models.IntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    interrupt_value = models.JSONField(null=True, blank=True, default=dict)
    human_response = models.CharField(max_length=200, blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.status}] {self.get_approval_type_display()} (Job {self.job_id})"


class SessionLog(models.Model):
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_SYSTEM = "system"
    ROLE_TOOL = "tool"

    ROLE_CHOICES = [
        (ROLE_USER, "User"),
        (ROLE_ASSISTANT, "Assistant"),
        (ROLE_SYSTEM, "System"),
        (ROLE_TOOL, "Tool"),
    ]

    job = models.ForeignKey(
        ScrapeJob, related_name="session_logs", on_delete=models.CASCADE
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_ASSISTANT)
    agent = models.CharField(max_length=100, blank=True, default="")
    content = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    seq = models.IntegerField(default=0)

    class Meta:
        ordering = ["seq"]
        indexes = [
            models.Index(fields=["job", "seq"]),
        ]

    def __str__(self):
        preview = self.content[:60] if self.content else "(empty)"
        return f"[{self.role}] Job {self.job_id}: {preview}"


class ToolCallLog(models.Model):
    job = models.ForeignKey(
        ScrapeJob, related_name="tool_call_logs", on_delete=models.CASCADE
    )
    agent = models.CharField(max_length=100)
    tool_name = models.CharField(max_length=200)
    tool_call_id = models.CharField(max_length=200, blank=True, default="")
    call_seq = models.IntegerField(default=0)
    args_summary = models.TextField(blank=True, default="")
    result_summary = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["call_seq"]
        indexes = [
            models.Index(fields=["job", "call_seq"]),
            models.Index(fields=["job", "agent"]),
        ]

    def __str__(self):
        return f"[{self.agent}] #{self.call_seq} {self.tool_name}"


class Site(models.Model):
    url = models.URLField(unique=True)
    name = models.CharField(max_length=200, blank=True, default="")
    slug = models.CharField(max_length=200, blank=True, default="")
    sample_url = models.URLField(max_length=1000, blank=True, default="")
    input_urls = models.JSONField(default=list, blank=True)
    currency = models.CharField(max_length=10, blank=True, default="")

    site_type = models.CharField(
        max_length=20,
        choices=_site_type_choices(),
        default="shopping",
    )
    output_schema = models.JSONField(default=dict, blank=True)

    platform = models.CharField(max_length=100, blank=True, default="")
    scraping_method = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=20, default="new")
    product_count = models.IntegerField(default=0)
    fields_extracted = models.JSONField(default=list, blank=True)
    has_scraper = models.BooleanField(default=False)
    default_scraper_path = models.CharField(max_length=500, blank=True, default="")
    last_scraped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Site"

    def __str__(self):
        return f"{self.slug or self.url} ({self.status})"

    def save(self, **kwargs):
        self.url = _normalize_url(self.url)
        self.sample_url = _normalize_url(self.sample_url)
        if self.input_urls:
            self.input_urls = [_normalize_url(u) for u in self.input_urls]
        result = super().save(**kwargs)
        _sync_input_urls_file(self)
        return result


class ProbeCache(models.Model):
    domain = models.CharField(max_length=253, unique=True, db_index=True)
    method = models.CharField(max_length=50)
    needs_akamai_bypass = models.BooleanField(default=False)
    captcha_detected = models.BooleanField(default=False)
    cached_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-cached_at"]
        verbose_name = "Probe Cache"
        verbose_name_plural = "Probe Cache"

    def __str__(self):
        return f"{self.domain} ({self.method})"

    @property
    def is_expired(self):
        from django.utils import timezone
        from datetime import timedelta

        return timezone.now() > self.cached_at + timedelta(hours=4)


class AgentPlayground(models.Model):
    """Tracks individual agent runs from the Agent Playground UI.

    Allows testing agents in isolation without creating a ScrapeJob or
    running the full graph workflow.
    """

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    # Available agents for testing (matches AGENT_TOOL_MAP keys)
    AGENT_CHOICES = [
        ("site_analyzer", "Site Analyzer"),
        ("browser_traverse", "Browser Navigation"),
        ("nav_skill_review", "Navigation Skill Review"),
        ("product_analyzer", "Product Analyzer"),
        ("scraper_analyzer", "Scraper Analyzer"),
        ("code_writer", "Code Writer"),
        ("code_tester", "Code Tester"),
        ("cleanup", "Cleanup"),
    ]

    agent_name = models.CharField(max_length=50, choices=AGENT_CHOICES)
    prompt = models.TextField(help_text="Custom prompt for the agent")
    url = models.CharField(
        max_length=500, blank=True, default="", help_text="Target URL (optional)"
    )
    search_criteria = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Search criteria for navigation agents (optional)"
    )
    site_slug = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Site slug for workspace scoping",
    )
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    output_summary = models.TextField(blank=True, default="")
    output_artifacts = models.JSONField(
        default=list, blank=True, help_text="Files written by the agent"
    )
    tool_call_count = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Agent Playground Run"
        verbose_name_plural = "Agent Playground"

    def __str__(self):
        return f"#{self.id} {self.agent_name} ({self.status})"


# ─────────────────────────────────────────────────────────────────────────────
# Close pending approvals when a job reaches a terminal state.
#
# A job that ends (completed/failed/cancelled/blocked) can no longer act on its
# open approvals — leaving them "pending" pollutes the approval queue with ghost
# entries.  This generic post_save hook closes them so the queue only shows
# approvals that are genuinely actionable (i.e. on waiting_approval jobs).
# Idempotent: only ever touches status=pending rows.  [goal: human-interaction]
# ─────────────────────────────────────────────────────────────────────────────
from django.db.models.signals import post_save
from django.dispatch import receiver

_TERMINAL_JOB_STATUSES = frozenset({
    ScrapeJob.STATUS_COMPLETED,
    ScrapeJob.STATUS_FAILED,
    ScrapeJob.STATUS_CANCELLED,
    ScrapeJob.STATUS_CAPTCHA_BLOCKED,
    ScrapeJob.STATUS_AKAMAI_BLOCKED,
})


@receiver(post_save, sender=ScrapeJob)
def _close_open_approvals_on_terminal_job(sender, instance: "ScrapeJob", **kwargs):
    """Supersede any still-pending approvals the moment a job ends."""
    if instance.status not in _TERMINAL_JOB_STATUSES:
        return
    open_approvals = instance.approvals.filter(status=Approval.STATUS_PENDING)
    if not open_approvals.exists():
        return
    from django.utils import timezone
    import logging as _logging

    count = open_approvals.count()
    open_approvals.update(
        status=Approval.STATUS_SUPERSEDED,
        resolved_at=timezone.now(),
    )
    _logging.getLogger("scraper.models").info(
        "Job %s: superseded %d open approval(s) (job ended: %s)",
        instance.id, count, instance.status,
    )


class JobListing(models.Model):
    """A single scraped job listing, stored for the jobs dashboard.

    Populated by the `store_job_listings` graph node (post-completion, non-breaking)
    from the output JSON of job_navigation / job_posting scrapes. Enables the
    dashboard to show "all jobs posted in the last N days" across all sites without
    re-reading JSON files.
    """

    # Identity + source
    scrape_job = models.ForeignKey(
        ScrapeJob, on_delete=models.CASCADE, related_name="listings", null=True, blank=True
    )
    site = models.ForeignKey(
        Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="listings"
    )
    site_name = models.CharField(max_length=200, blank=True, default="")
    site_slug = models.CharField(max_length=200, blank=True, default="")
    url = models.URLField(max_length=1000, blank=True, default="")
    job_source_id = models.CharField(max_length=200, blank=True, default="")

    # Core fields (from content_types JOB_FIELDS)
    title = models.CharField(max_length=500, blank=True, default="")
    company = models.CharField(max_length=300, blank=True, default="")
    location = models.CharField(max_length=300, blank=True, default="")
    description = models.TextField(blank=True, default="")
    salary = models.CharField(max_length=300, blank=True, default="")
    job_type = models.CharField(max_length=100, blank=True, default="")
    employment_type = models.CharField(max_length=100, blank=True, default="")

    # Date filtering (the key field for the dashboard)
    posted_date = models.DateField(null=True, blank=True, db_index=True)
    date_posted_reliable = models.BooleanField(default=True)
    valid_through = models.DateField(null=True, blank=True)

    # Extra fields (flexible — store any site-specific fields not covered above)
    extra_data = models.JSONField(default=dict, blank=True)

    # Metadata — scraped_at is auto_now_add (fires on INSERT only), so it
    # IS first_seen_at (the first scrape run that discovered this job). It
    # survives update_or_create (auto_now_add is INSERT-only). The system's
    # authoritative freshness signal when posted_date is unreliable.
    scraped_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scraped_at", "-posted_date"]
        indexes = [
            models.Index(fields=["posted_date"]),
            models.Index(fields=["company"]),
            models.Index(fields=["location"]),
            models.Index(fields=["site_slug"]),
        ]

    def __str__(self):
        return f"{self.title} @ {self.company} ({self.posted_date})"

