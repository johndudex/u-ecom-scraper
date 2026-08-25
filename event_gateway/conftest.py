"""Shared fixtures: event loop policy + a real Postgres schema on the dev DB.

The gateway tests hit real Postgres + real Redis (same services the stack
runs) — the FM/browser_service precedent for service tests in this repo.
A dedicated schema keeps them isolated.
"""
import pytest


@pytest.fixture(scope="session")
def event_loop_policy():
    import asyncio

    return asyncio.get_event_loop_policy()
