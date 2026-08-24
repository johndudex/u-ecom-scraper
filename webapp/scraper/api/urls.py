"""Partner API v1 routes (docs/specs/sync_api.yaml paths)."""
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import readers
from . import sse as sse_views
from . import writers


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


urlpatterns = [
    path("check-site", readers.check_site, name="api_check_site"),
    path("validate-schema", readers.validate_schema, name="api_validate_schema"),
    path("jobs", _jobs_dispatch, name="api_jobs"),
    path("jobs/<int:job_id>", readers.job_status, name="api_job_status"),
    path("jobs/<int:job_id>/cancel", writers.cancel_job, name="api_cancel_job"),
    path("jobs/<int:job_id>/callback", _callback_dispatch, name="api_job_callback"),
    path("jobs/<int:job_id>/sample", writers.get_job_sample, name="api_job_sample"),
    path("jobs/<int:job_id>/output", writers.get_job_output, name="api_job_output"),
    path("jobs/<int:job_id>/output/download", writers.download_job_output, name="api_job_output_download"),
    path("jobs/<int:job_id>/events", sse_views.job_events_sse, name="api_job_events"),
    path("ws-token", sse_views.ws_token, name="api_ws_token"),
]
