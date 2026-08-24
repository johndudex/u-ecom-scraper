"""create_api_key management command (deploy runbook step 5).

Locks:
- creates user + key together, prints the RAW key exactly once
- idempotent-ish: --rotate revokes the old key and issues a new one
- never a superuser (the code-level mandate)
"""
from __future__ import annotations

import os
import sys
from io import StringIO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.core.management import call_command  # noqa: E402

from scraper import models  # noqa: E402


@pytest.mark.django_db
def test_creates_user_and_key():
    out = StringIO()
    call_command("create_api_key", "--user", "acme-partner", stdout=out)
    printed = out.getvalue()
    key = models.ApiKey.objects.select_related("user").get(user__username="acme-partner")
    assert "pk_" in printed  # raw key printed once
    assert key.user.is_superuser is False
    assert key.user.is_active
    # the printed raw key authenticates
    raw = [l for l in printed.splitlines() if "pk_" in l][0].strip().split()[-1]
    assert models.ApiKey.hash_key(raw) == key.key_hash


@pytest.mark.django_db
def test_rotate_revokes_old():
    out = StringIO()
    call_command("create_api_key", "--user", "acme2", stdout=out)
    first = models.ApiKey.objects.get(user__username="acme2")
    out2 = StringIO()
    call_command("create_api_key", "--user", "acme2", "--rotate", stdout=out2)
    # OneToOne(user): rotation REPLACES the row (revoked hash is dead)
    assert not models.ApiKey.objects.filter(pk=first.pk).exists()
    assert models.ApiKey.objects.filter(user__username="acme2").count() == 1


@pytest.mark.django_db
def test_rejects_existing_without_rotate():
    from django.core.management.base import CommandError

    call_command("create_api_key", "--user", "acme3", stdout=StringIO())
    with pytest.raises(CommandError):
        call_command("create_api_key", "--user", "acme3", stdout=StringIO())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
