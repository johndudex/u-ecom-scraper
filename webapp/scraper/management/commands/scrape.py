"""Create and enqueue a scrape job.

Usage:
    python manage.py scrape https://www.calvinklein.co.uk --mode navigation --search "mens jeans"
    python manage.py scrape https://www.nike.com/product/abc --mode url_list
    python manage.py scrape https://www.shop.com --mode navigation --search "shoes" --product-url "https://www.shop.com/shoes"
"""

from django.core.management.base import BaseCommand

from scraper.models import ScrapeJob
from scraper.tasks import run_scrape_task


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
        job = ScrapeJob.objects.create(
            url=url,
            product_url=options["product_url"],
            currency=options["currency"],
            input_mode=options["mode"],
            search_criteria=options["search_criteria"],
            full_extraction=options["full_extraction"],
            auto_queued=options["auto_queue"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Created job #{job.id} ({job.input_mode}, '{job.search_criteria}')"
            )
        )

        if not options["dry_run"]:
            run_scrape_task.delay(job.id)
            self.stdout.write(self.style.SUCCESS(f"Enqueued task for job #{job.id}"))
