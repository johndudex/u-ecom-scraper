"""Partner API v1 (docs/specs/sync_api.yaml — OpenAPI 3.1).

Plain Django views + JsonResponse (no DRF — the spec's error model is a
custom {code, message, details} envelope and every endpoint wraps an
existing helper; see plans/api-sync-implementation-plan.md D1).
"""
