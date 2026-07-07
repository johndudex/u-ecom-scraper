"""Pytest fixtures for the scraper app.

Ensures a superuser exists so the DebugAutoLoginMiddleware can auto-login during
tests — views are ``@login_required`` and tests use an unauthenticated
``self.client``. With ``DEBUG_AUTO_LOGIN=True`` (test_settings) + a superuser,
the middleware authenticates each request, so tests get 200 instead of 302.

Function-scoped + autouse so the superuser is created within each Django
TestCase's transaction (session-scoped fixtures aren't reliably visible to
TestCase due to per-test transaction rollback).
"""

import pytest


@pytest.fixture(autouse=True)
def _ensure_test_superuser(django_db_blocker):
    from django.contrib.auth import get_user_model

    with django_db_blocker.unblock():
        User = get_user_model()
        if not User.objects.filter(is_superuser=True).exists():
            User.objects.create_superuser(
                username="testadmin", password="testpass", email=""
            )
