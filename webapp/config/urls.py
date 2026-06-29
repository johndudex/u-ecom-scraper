from django.contrib import admin
from django.urls import path, include

from . import views as config_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("api/health/raw", config_views.health_check, name="health_raw"),
    path("", include("scraper.urls")),
]
