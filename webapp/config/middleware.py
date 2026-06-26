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
        if getattr(settings, "DEBUG_AUTO_LOGIN", False) and getattr(settings, "DEBUG", False):
            if not isinstance(request.user, AnonymousUser):
                return self.get_response(request)

            path = request.path
            if any(path.startswith(p) for p in EXEMPT_PATHS):
                return self.get_response(request)

            superuser = User.objects.filter(is_superuser=True).first()
            if superuser:
                request.user = superuser
                login(request, superuser, backend="django.contrib.auth.backends.ModelBackend")

        return self.get_response(request)
