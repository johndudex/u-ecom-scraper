"""The spec's single error shape: {code, message, details?} (sync_api.yaml
Error schema). ApiError carries an HTTP status; api_view converts uncaught
ones to 500 with a trace id.
"""
from __future__ import annotations

import secrets


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def body(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


def unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", "Missing or invalid X-API-Key.")


def forbidden(reason: str = "Key revoked or account inactive.") -> ApiError:
    return ApiError(403, "forbidden", reason)


def not_found(what: str = "Job") -> ApiError:
    # 404 (not 403) on cross-tenant reads — the spec's non-oracle rule.
    return ApiError(404, "not_found", f"{what} does not exist.")


def rate_limited(limit: str) -> ApiError:
    return ApiError(429, "rate_limited", "Per-key rate limit exceeded.",
                    {"limit": limit})


def internal_error() -> ApiError:
    return ApiError(500, "internal_error", "Unexpected server error.",
                    {"trace_id": secrets.token_hex(8)})
