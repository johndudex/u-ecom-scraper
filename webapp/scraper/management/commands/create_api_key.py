"""Create a partner service account + API key (deploy runbook step 5).

Prints the RAW key exactly once — it is never recoverable. --rotate
revokes the partner's existing key and issues a fresh one.
"""
from __future__ import annotations

import secrets

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User

from scraper.models import ApiKey


class Command(BaseCommand):
    help = "Create (or rotate) a partner API key + its service-account user."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="service-account username")
        parser.add_argument("--rotate", action="store_true", help="revoke existing key, issue new")
        parser.add_argument("--label", default="", help="display label")

    def handle(self, *args, **options):
        username = options["user"]
        user = User.objects.filter(username=username).first()
        if user is None:
            user = User.objects.create_user(username, password=secrets.token_urlsafe(32))
        if user.is_superuser:
            raise CommandError("refusing: superuser accounts may not hold API keys")
        existing = ApiKey.objects.filter(user=user, revoked_at__isnull=True).first()
        if existing and not options["rotate"]:
            raise CommandError(
                f"{username} already has an active key ({existing.prefix}…); "
                "pass --rotate to replace it"
            )
        if existing:
            # OneToOne(user): only ONE ApiKey row may exist — the revoked
            # row's hash is dead anyway, so rotate = replace the row.
            existing.delete()
            self.stdout.write(f"revoked + replaced key {existing.prefix}…")
        raw = "pk_" + secrets.token_urlsafe(32)
        key = ApiKey.objects.create(
            user=user, prefix=raw[:8], key_hash=ApiKey.hash_key(raw),
            label=options["label"] or username,
        )
        self.stdout.write(self.style.SUCCESS(f"API key created for {username}"))
        self.stdout.write(f"RAW KEY (shown once): {raw}")
        self.stdout.write(f"prefix: {key.prefix}  id: {key.pk}")
