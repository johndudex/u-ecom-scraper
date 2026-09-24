"""Partner API v1 routes (docs/specs/sync_api.yaml paths)."""
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import discovery, extractors, readers, writers
from . import sse as sse_views


# csrf_exempt must sit on the OUTERMOST view Django resolves — the wrapped
# api_view's own csrf_exempt does not propagate through a plain dispatcher.
@csrf_exempt
def _callback_dispatch(request, job_id: int):
    if request.method == "PATCH":
        return writers.patch_job_callback(request, job_id)
    return writers.get_job_callback(request, job_id)

@csrf_exempt
def _jobs_dispatch(request, **kwargs):
    if request.method == "POST":
        return writers.create_job(request)
    return readers.list_jobs(request)

@csrf_exempt
def _extractor_slug_dispatch(request, slug: str):
    return extractors.extractor_dispatch(request, slug=slug)


urlpatterns = [
    path("check-site", readers.check_site, name="api_check_site"),
    path("discover-fields", discovery.discover_fields, name="api_discover_fields"),
    path("validate-schema", readers.validate_schema, name="api_validate_schema"),
    # [wave-27 W27-6] extractor resource — Sites scoped to the key's own jobs.
    # archive/unarchive/list are api_view-wrapped (they carry their own
    # csrf_exempt attribute); only the GET/PATCH slug dispatcher needs the
    # outer wrapper.
    path("extractors", extractors.list_extractors, name="api_extractors"),
    path("extractors/<str:slug>/archive", extractors.archive_extractor, name="api_extractor_archive"),
    path("extractors/<str:slug>/unarchive", extractors.unarchive_extractor, name="api_extractor_unarchive"),
    path("extractors/<str:slug>", _extractor_slug_dispatch, name="api_extractor"),
    path("jobs", _jobs_dispatch, name="api_jobs"),
    path("jobs/<int:job_id>", readers.job_status, name="api_job_status"),
    path("jobs/<int:job_id>/cancel", writers.cancel_job, name="api_cancel_job"),
    path("jobs/<int:job_id>/callback", _callback_dispatch, name="api_job_callback"),
    path("jobs/<int:job_id>/sample", writers.get_job_sample, name="api_job_sample"),
    path("jobs/<int:job_id>/output", writers.get_job_output, name="api_job_output"),
    path("jobs/<int:job_id>/output/download", writers.download_job_output, name="api_job_output_download"),
    path("jobs/<int:job_id>/scraper-code", writers.get_job_scraper_code, name="api_job_scraper_code"),
    path("jobs/<int:job_id>/events", sse_views.job_events_sse, name="api_job_events"),
    path("ws-token", sse_views.ws_token, name="api_ws_token"),
]
