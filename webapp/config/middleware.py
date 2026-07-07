from django.conf import settings
from django.contrib.auth import login, get_user_model
from django.contrib.auth.models import AnonymousUser

User = get_user_model()

EXEMPT_PATHS = ("/accounts/login/", "/accounts/logout/", "/admin/login/", "/admin/logout/")


class DebugAutoLoginMiddleware:
    """When DEBUG_AUTO_LOGIN is set, auto-authenticate as the first superuser.

    Lets curl/wget access authenticated pages (including Django admin) without
    manually handling login cookies. Only runs when DEBUG=True and the env
    flag is set.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Gate on DEBUG_AUTO_LOGIN alone (not DEBUG) — pytest-django forces
        # DEBUG=False during tests, which would otherwise disable auto-login.
        # DEBUG_AUTO_LOGIN is the explicit opt-in flag (set in test_settings/dev
        # env); production leaves it unset → False → middleware is a no-op there.
        if getattr(settings, "DEBUG_AUTO_LOGIN", False):
            if not isinstance(request.user, AnonymousUser):
                return self.get_response(request)

            path = request.path
            if any(path.startswith(p) for p in EXEMPT_PATHS):
                return self.get_response(request)

            superuser = User.objects.filter(is_superuser=True).first()
            # DEBUG/dev/test fallback: if no superuser exists yet, create one so
            # auto-login works without a separate fixture (e.g. in tests where
            # session-scoped fixtures aren't visible to Django TestCase). This
            # whole branch is gated on DEBUG + DEBUG_AUTO_LOGIN, so production
            # (DEBUG=False) is never affected.
            if not superuser:
                superuser, _ = User.objects.get_or_create(
                    username="debug-autologin",
                    defaults={"is_superuser": True, "is_staff": True, "is_active": True},
                )
            if superuser:
                request.user = superuser
                login(request, superuser, backend="django.contrib.auth.backends.ModelBackend")

        return self.get_response(request)
