"""Partner API v1 routes (docs/specs/sync_api.yaml paths)."""
from django.urls import path

from . import readers

urlpatterns = [
    path("check-site", readers.check_site, name="api_check_site"),
    path("validate-schema", readers.validate_schema, name="api_validate_schema"),
    path("jobs", readers.list_jobs, name="api_list_jobs"),
    path("jobs/<int:job_id>", readers.job_status, name="api_job_status"),
]
