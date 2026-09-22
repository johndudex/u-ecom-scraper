"""Create and enqueue a scrape job.

Usage:
    python manage.py scrape https://www.calvinklein.co.uk --mode navigation --search "mens jeans"
    python manage.py scrape https://www.nike.com/product/abc --mode url_list
    python manage.py scrape https://www.shop.com --mode navigation --search "shoes" --product-url "https://www.shop.com/shoes"
"""

from django.core.management.base import BaseCommand

from scraper.models import ScrapeJob, Site


def _fm_input_urls_file_exists(slug: str) -> bool | None:
    """True/False for scrapers/{slug}/input_urls.json; None on any error
    (fail-open, wave-40 T5 — a transient FM outage must not lock the CLI out)."""
    try:
        import src.artifacts as artifacts

        return artifacts.exists(f"scrapers/{slug}/input_urls.json")
    except Exception:
        return None


def _write_input_urls(slug: str, urls: list) -> None:
    """Best-effort seed write (mirrors intake_create_job: an FM outage logs
    and never breaks the create)."""
    try:
        import src.artifacts as artifacts

        artifacts.write_json(
            artifacts.scrapers_key(slug, "input_urls.json"), {"urls": urls}
        )
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "scrape command: could not persist seed URL for %s: %s", slug, exc
        )


class Command(BaseCommand):
    help = "Create and enqueue a scrape job"

    def add_arguments(self, parser):
        parser.add_argument("url", type=str)
        parser.add_argument(
            "--mode",
            type=str,
            default="url_list",
            choices=["url_list", "navigation", "list_page", "search_term"],
        )
        parser.add_argument(
            "--page-type",
            type=str,
            default="product",
            dest="page_type",
            choices=[
                "product",
                "product_list",
                "product_navigation",
                "article",
                "article_list",
                "article_navigation",
                "job_posting",
                "job_navigation",
                "forum_thread",
                "serp",
                "page_content",
            ],
            help="Page type (drives content type, site_type, and routing). "
            "Use job_navigation for job portals discovered by browsing.",
        )
        parser.add_argument("--search", type=str, default="", dest="search_criteria")
        parser.add_argument("--product-url", type=str, default="")
        parser.add_argument("--currency", type=str, default="")
        parser.add_argument("--full-extraction", action="store_true")
        parser.add_argument("--auto-queue", action="store_true")
        parser.add_argument(
            "--dry-run", action="store_true", help="Create job but don't enqueue"
        )

    def handle(self, *args, **options):
        url = options["url"]

        # [wave-40 T5] Same repair+gate as intake: url_list with no URL list
        # anywhere dies ~4s into setup_workspace. A PDP-shaped url is seeded
        # (that flow works — prod 758); a CLI cannot 422, so the genuinely
        # empty case hard-exits with the reason BEFORE any row is created.
        from scraper.tasks import _generate_slug
        from src.intake_coerce import coerce_pdp_intake
        from src.intake_url_list import coerced_seed_url, missing_url_list_reason

        slug = _generate_slug(url)
        _site = Site.objects.filter(slug=slug).first()
        _seed = ""
        if coerce_pdp_intake(options["product_url"] or url, "listing")[0] == "pdp":
            _seed = coerced_seed_url(
                options["product_url"] or url, options["mode"], ""
            )
        _reason = missing_url_list_reason(
            options["mode"],
            "",
            bool(_site and _site.input_urls),
            _fm_input_urls_file_exists(slug),
            _seed,
        )
        if _reason:
            raise SystemExit(_reason)

        job = ScrapeJob.objects.create(
            url=url,
            page_type=options["page_type"],
            product_url=options["product_url"],
            currency=options["currency"],
            input_mode=options["mode"],
            search_criteria=options["search_criteria"],
            full_extraction=options["full_extraction"],
            auto_queued=options["auto_queue"],
        )
        if _seed and slug:
            _write_input_urls(slug, [_seed])
        self.stdout.write(
            self.style.SUCCESS(
                f"Created job #{job.id} (page_type={job.page_type}, "
                f"mode={job.input_mode}, '{job.search_criteria}')"
            )
        )

        if not options["dry_run"]:
            from scraper.tasks import dispatch_scrape_job

            # [wave-15 1.0] keystone replaces the bare .delay, which never
            # persisted the task id at all (watchdog liveness + sweep were
            # blind to shell-dispatched jobs).
            dispatch_scrape_job(job.id)
            self.stdout.write(self.style.SUCCESS(f"Enqueued task for job #{job.id}"))
