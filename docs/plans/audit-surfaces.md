# Live-Surface Audit — User-Facing Probes

**Date:** 2026-08-25
**Method:** direct probes only (curl / docker exec / read-only Python). No pytest, no test suites.
**Base URL:** http://localhost:8000
**Auth:** existing jar `/tmp/intake.jar` (sessionid still valid — verified `GET /admin/` → 200, no re-login needed).

**Result: 10 / 11 PASS. One hard FAIL (item 10 — flower down). Items 5/6/7 pass
with an auth-enforcement caveat noted below (dev auto-login masks the login wall).**

| # | Surface | Result | Evidence |
|---|---------|--------|----------|
| 1 | `/admin/scraper/joblisting/` + both recompute buttons | **PASS** | `HTTP 200 \| 168373 bytes`; `grep -c "Preview date-reliability recompute"` = 1; `grep -c "APPLY recompute (--write)"` = 1 |
| 2 | `/admin/scraper/eventoutbox/` | **PASS** | `HTTP 200 \| 133269 bytes` (35 EventOutbox rows behind it) |
| 3 | `/admin/scraper/jobcallback/` no secret leak | **PASS** | `HTTP 200 \| 107127 bytes`; `grep -oiE 'secret…'` → **zero matches**; only 40-char token on page is the CSRF middleware token (`csrfmiddlewaretoken`, admin chrome); `JobCallbackAdmin.exclude = ("secret",)`; table empty (0 rows) so nothing to leak |
| 4 | `/admin/scraper/apikey/` | **PASS** | `HTTP 200 \| 112552 bytes` (5 ApiKey rows) |
| 5 | `/jobs-dashboard/` | **PASS** | auth'd `HTTP 200 \| 35994 bytes`, `<title>Jobs Dashboard` |
| 6 | `/intake/` | **PASS** (with caveat) | auth'd `HTTP 200 \| 149798 bytes`, `<title>Extract — data extractor setup` |
| 7 | `/docs/sync_api` + `/docs/async_api` (+ raw view) | **PASS** | both `HTTP 200` logged-in (1133 B / 43009 B); `?view=raw` on async → 200 with **4** `x-status: live` matches (spec required ≥2; exact `$`-anchored count = 3, plus 1 in the header comment = 4) |
| 8 | API 401 JSON envelope ×7 | **PASS** | all 7 endpoints → `status=401 type=application/json` body `{"code": "unauthorized", "message": "Missing or invalid X-API-Key."}` — incl. `/api/v1/jobs/1/events` (same envelope, no divergence) |
| 9 | Event gateway health | **PASS** | `{"status":"ok","service":"event-gateway","ts":1787674546.687842}` (HTTP 200) |
| 10 | All 10 services healthy | **FAIL** | 9 running+healthy; **`flower` exited** — `Exited (1) 2 hours ago`. Crash: `ModuleNotFoundError: No module named 'src'` (`webapp/scraper/urls.py` line 3 → `views.py` line 29 → `from src.schema_validation import …`). Reproducible in its logs. |
| 11 | WS handshake refused without auth | **PASS** | raw curl upgrade → `HTTP/1.1 403 Forbidden` (Content-Length: 0); python `websockets` client → `REFUSED: InvalidStatus: server rejected WebSocket connection: HTTP 403` |

## FAIL detail

### Item 10 — flower container down (the only hard failure)

`docker compose ps -a` shows 10 defined, 9 running+healthy, 1 exited:

```
flower   exited   Exited (1) 2 hours ago
```

All others report `healthy`: browser_service (4d), celery-beat (2h), celery-events (2h),
celery-worker (2h), django (12m), event-gateway (42m), file-master (5d), postgres (5d), redis (4d).

Crash traceback (`docker compose logs flower`):

```
File "/app/webapp/scraper/urls.py", line 3, in <module>
    from . import views
File "/app/webapp/scraper/views.py", line 29, in <module>
    from src.schema_validation import validate_user_schema
ModuleNotFoundError: No module named 'src'
```

Flower (`celery -A config flower`) imports the Django app, which pulls in
`webapp/scraper/urls.py` → `views.py` → `src.schema_validation`, and the `src`
package is not importable in that container's environment. Every other service
in the file shares the same image/build context, so this is a container-PATH /
workdir or PYTHONPATH difference specific to the flower service definition
(`docker-compose.yml` line 246), not a missing file.

Secondary note: flower is under `profiles: ["full"]`. The stack was evidently
started with the full profile (it ran for weeks), so the profile is not the
reason it is down — the import error is.

### Items 5/6/7 — "login required" is satisfied only by dev middleware (observation, not counted as FAIL)

The spec for items 5–7 says "login required". Both views are genuinely
`@login_required` (`webapp/scraper/views.py` lines 2153, 2275, 2958, 2964), but
in this environment `config.middleware.DebugAutoLoginMiddleware` is active:

- `.env` line 2: `DEBUG_AUTO_LOGIN=True`
- `docker compose exec django … settings.DEBUG` → `True`
- `settings.DEBUG_AUTO_LOGIN` → `True`

Consequence: an unauthenticated `curl` (fresh jar, no session) to
`/jobs-dashboard/` and `/intake/` also returns **200 with full page content** —
the middleware auto-authenticates as the first superuser. So the pages load
(PASS on the letter of "loads 200"), but "login required" cannot be observed
from the outside in this configuration. The middleware self-documents as
dev/test-only and hard-gates on `DEBUG=True or "pytest" in sys.modules`, so
production (Railway, DEBUG=False) is not exposed. Recorded here because the
promise being audited is user-facing surfaces responding — they do.

### Item 7 addendum

`/docs/sync_api?view=raw` contains **0** `x-status: live` markers — the spec's
≥2 requirement applies only to the async document, which it meets (4).
Sync's raw view renders the same standalone page shell; its `x-status` markers
are `planned` only (the sync API is greenfield, matching the memory note
"`/api/v1` implementation itself is greenfield").

## Environment notes

- Job 1 does not exist (`ScrapeJob.objects.filter(id=1).exists()` → False), so
  `/api/v1/jobs/1*` auth checks were exercised as intended (rejected before any
  lookup) — the 401 envelope is auth-layer, independent of object existence.
- Row counts behind the admin pages: JobListing 14,969; EventOutbox 35;
  ApiKey 5; JobCallback 0.
