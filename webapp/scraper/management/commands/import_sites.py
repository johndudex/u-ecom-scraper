import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from scraper.models import Site


class Command(BaseCommand):
    help = "Import sites from a JSON fixture file, skipping duplicates by URL"

    def add_arguments(self, parser):
        parser.add_argument("fixture_file", type=str, help="Path to the fixture JSON file")

    def handle(self, *args, **options):
        fixture_path = options["fixture_file"]

        try:
            with open(fixture_path) as f:
                fixtures = json.load(f)
        except FileNotFoundError:
            self.stderr.write(self.style.ERROR(f"File not found: {fixture_path}"))
            return

        created = 0
        skipped = 0

        with transaction.atomic():
            for item in fixtures:
                fields = item["fields"]
                url = fields["url"]
                if Site.objects.filter(url=url).exists():
                    skipped += 1
                    continue
                Site.objects.create(**fields)
                created += 1

        self.stdout.write(
            self.style.SUCCESS(f"Created: {created}, Skipped: {skipped}")
        )
