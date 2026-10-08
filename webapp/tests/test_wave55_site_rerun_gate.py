"""[round-10 GAP-6] site_rerun is a second output publisher — gate it too.

``site_rerun`` executes a promoted scraper outside the graph and writes
``output_*.json`` to the File Master under ``scrapers/{slug}/``, then stamps
``Site.product_count`` — with no ``run_execution`` behind it, so the finalize
row gates the wave-55 COMPLETED stamp applies never see the payload. A re-run
could therefore re-ship the exact string prices the main path repairs
(audit 1208/1215/1176: ``"price": "$65.00"``) and credit rows the main path
would have dropped. Same gate, same reuse — ``scraper.tasks._gate_output_rows``
— never a re-implementation.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_site_rerun_gate.py -q"
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from types import ModuleType
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib import messages  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402
from scraper.models import Site  # noqa: E402
from scraper.views import _gate_rerun_output  # noqa: E402

pytestmark = pytest.mark.django_db

SLUG = "rerungate"

# The promoted scraper: writes the audit's string-price shape next to itself
# (site_rerun runs `python3 <staged copy>` with cwd = the staging dir).
SCRAPER_SRC = """
import json

rows = [
    {"title": "p0", "price": "$65.00", "url": "https://x.example/p/0"},
    {"title": "p1", "price": "$19.50", "url": "https://x.example/p/1"},
    {"title": "p2", "price": "$4.00", "url": "https://x.example/p/2"},
]
with open("output_2026-09-20_050000_000001_7.json", "w", encoding="utf-8") as fh:
    json.dump({"products": rows}, fh)
"""


@contextlib.contextmanager
def _fm(read_texts: dict):
    """Both binding sites of ``src.artifacts`` (the bpo-30024 double-bind the
    wave-55 census documents): a stub in one and not the other makes the patch
    land on the wrong object depending on collection order."""
    import src

    fake = ModuleType("src.artifacts")
    fake.scrapers_key = lambda slug, *parts: "/".join(
        ["scrapers", slug, *[p.strip("/") for p in parts if p]])
    fake.read_text = lambda key: read_texts.get(key)
    fake.read_json = lambda key: json.loads(read_texts[key])
    fake.exists = lambda key: key in read_texts
    fake.list_keys = lambda prefix="": sorted(read_texts)
    published: dict = {}

    def _write(key, data):
        published[key] = bytes(data)

    def _write_json(key, data):
        published[key] = json.dumps(data).encode("utf-8")

    fake.write = _write
    fake.write_json = _write_json
    with patch.dict(sys.modules, {"src.artifacts": fake}):
        with patch.object(src, "artifacts", fake, create=True):
            yield published


def _site(**kw):
    defaults = dict(
        url=f"https://{SLUG}.example/", name=SLUG, slug=SLUG,
        scraping_method="requests", has_scraper=True,
        default_scraper_path=f"scrapers/{SLUG}/scraper.py",
    )
    defaults.update(kw)
    return Site.objects.create(**defaults)


@pytest.fixture
def client(db):
    User.objects.create_user(username="_t_rerun_gate", password="x")
    c = Client()
    c.login(username="_t_rerun_gate", password="x")
    return c


class TestRerunPublishesGatedRows:
    def test_the_published_output_holds_repaired_prices(
            self, client, tmp_path):
        site = _site()
        with _fm({f"scrapers/{SLUG}/scraper.py": SCRAPER_SRC}) as published:
            r = client.post(reverse("site_rerun", args=[site.id]))
        assert r.status_code == 302
        keys = [k for k in published if k.endswith(".json")]
        assert keys, "the re-run must still publish its output"
        payload = json.loads(published[keys[0]])
        assert all(r_["price"] == float(p)
                   for r_, p in zip(payload["products"], (65.0, 19.5, 4.0)))
        site.refresh_from_db()
        assert site.product_count == 3

    def test_the_site_count_is_the_kept_row_count(self, client):
        """A row the price gate drops is a dropped credit here too — the
        Site row may not credit a row the published bytes do not hold."""
        src = SCRAPER_SRC.replace('"$4.00"', '"Save 20%"')
        site = _site()
        with _fm({f"scrapers/{SLUG}/scraper.py": src}) as published:
            client.post(reverse("site_rerun", args=[site.id]))
        payload = json.loads(
            published[f"scrapers/{SLUG}/output_2026-09-20_050000_000001_7.json"])
        assert len(payload["products"]) == 2
        site.refresh_from_db()
        assert site.product_count == 2


    def test_a_browser_lane_200_with_no_output_bytes_is_not_a_500(self, client):
        """[GAP-A r12] A 200 with no output bytes is not a credit and not a
        500: scraper_runner answers HTTP 200 with ``output_content=""`` when
        the scraper exits 0 and ``_find_output_file`` finds nothing, so neither
        the gated arm nor its except arm used to assign and the count read
        blew up with UnboundLocalError — the whole re-run, including the
        ``last_scraped_at`` stamp, died on a reachable path."""
        from types import SimpleNamespace

        site = _site(scraping_method="playwright")
        with _fm({f"scrapers/{SLUG}/scraper.py": SCRAPER_SRC}), patch(
                "agents.tools.browser_http.post_scrape_with_retry") as post:
            post.return_value = SimpleNamespace(ok=True, error="", data={
                "output_content": "", "output_name": "", "product_count": 0})
            r = client.post(reverse("site_rerun", args=[site.id]))
        assert r.status_code == 302, getattr(r, "exception", None)
        site.refresh_from_db()
        assert site.product_count == 0
        assert site.last_scraped_at is not None, \
            "the crash also killed the last_scraped_at stamp"

    def test_the_browser_lane_credits_the_kept_row_count(self, client):
        """[GAP-A] The browser lane recomputes the count from the gated payload
        and then overwrote it with the service's own count — the service never
        saw the gate, so a dropped row came back to life on the Site row."""
        from types import SimpleNamespace

        site = _site(scraping_method="playwright")
        payload = json.dumps({"products": [
            {"title": "p0", "price": "$65.00", "url": "https://x/p/0"},
            {"title": "p1", "price": "$19.50", "url": "https://x/p/1"},
            {"title": "p2", "price": "Save 20%", "url": "https://x/p/2"}]})
        with _fm({f"scrapers/{SLUG}/scraper.py": SCRAPER_SRC}) as published, \
                patch(
                    "agents.tools.browser_http.post_scrape_with_retry") as post:
            post.return_value = SimpleNamespace(ok=True, error="", data={
                "output_content": payload, "output_name": "output_a.json",
                "product_count": 3})
            client.post(reverse("site_rerun", args=[site.id]))
        published_rows = json.loads(
            published[f"scrapers/{SLUG}/output_a.json"])["products"]
        assert len(published_rows) == 2
        site.refresh_from_db()
        assert site.product_count == 2


    def test_a_rerun_that_drops_rows_says_so_on_the_site_page(
            self, client, caplog):
        """[GAP-B] The gate's dropped-reason record must reach the operator —
        rendered on the site page the re-run redirects to, and in the celery
        log. (The audit's M3 mechanism statement: once the workspace is gone
        the job row is the only record, and a re-run's operator surface is the
        site page.) The store alone is not the surface — before base.html grew
        the messages block the note was stored and never shown anywhere but
        the learnt-skills page, so this asserts the bytes the operator reads."""
        import logging

        src = SCRAPER_SRC.replace('"$4.00"', '"Save 20%"')
        site = _site()
        with _fm({f"scrapers/{SLUG}/scraper.py": src}):
            with caplog.at_level(logging.WARNING, logger="scraper.views"):
                client.post(reverse("site_rerun", args=[site.id]))
        html = client.get(
            reverse("site_detail", args=[site.id])).content.decode()
        assert "[PRICE-GATE]" in html and "dropped" in html, "gate note never rendered"
        logged = " | ".join(r.getMessage() for r in caplog.records)
        assert "[PRICE-GATE]" in logged and "dropped" in logged, logged

    def test_the_browser_lane_says_so_too(self, client):
        """[GAP-B r12] The browser lane's ``messages.warning`` arm is the same
        operator surface — a lane is not exempt because its payload came back
        from browser_service instead of a tmp dir."""
        from types import SimpleNamespace

        site = _site(scraping_method="playwright")
        payload = json.dumps({"products": [
            {"title": "p0", "price": "$65.00", "url": "https://x/p/0"},
            {"title": "p1", "price": "Save 20%", "url": "https://x/p/1"}]})
        with _fm({f"scrapers/{SLUG}/scraper.py": SCRAPER_SRC}) as published, \
                patch(
                    "agents.tools.browser_http.post_scrape_with_retry") as post:
            post.return_value = SimpleNamespace(ok=True, error="", data={
                "output_content": payload, "output_name": "output_a.json",
                "product_count": 2})
            response = client.post(reverse("site_rerun", args=[site.id]))
        stored = [str(m) for m in messages.get_messages(response.wsgi_request)]
        assert any("[PRICE-GATE]" in m and "dropped" in m for m in stored), stored
        assert len(json.loads(
            published[f"scrapers/{SLUG}/output_a.json"])["products"]) == 1


class TestGateRerunOutputShape:
    def test_a_top_level_array_is_gated_in_place(self):
        rows = [{"title": "p", "price": "$3.99"}]
        out, notes = _gate_rerun_output(rows)
        assert out == [{"title": "p", "price": 3.99}]
        assert "[PRICE-GATE] 1 row(s) repaired" in notes[0]
        assert len(notes) == 2  # + [CURRENCY-MISSING]: the row names no currency

    def test_a_products_container_is_gated_under_its_key(self):
        out, notes = _gate_rerun_output(
            {"products": [{"title": "p", "price": "$3.99"}], "meta": {"n": 1}})
        assert out["products"] == [{"title": "p", "price": 3.99}]
        assert out["meta"] == {"n": 1}
        assert notes

    def test_a_non_json_payload_passes_through_untouched(self):
        assert _gate_rerun_output("not json") == ("not json", [])
        assert _gate_rerun_output(None) == (None, [])


class TestBrowserLaneFailureShapes:
    """[GAP-A r12] Every reachable shape of the browser-service reply. The
    gated arm used to be the ONLY assignment of ``product_count`` on a 200, so
    any shape it skipped blew up with UnboundLocalError at the count read.

    Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest
    tests/test_wave55_site_rerun_gate.py::TestBrowserLaneFailureShapes -q"
    """

    def _post(self, client, data, slug=SLUG, ok=True):
        from types import SimpleNamespace

        site = _site(scraping_method="playwright", slug=slug)
        with _fm({f"scrapers/{SLUG}/scraper.py": SCRAPER_SRC}), patch(
                "agents.tools.browser_http.post_scrape_with_retry") as post:
            post.return_value = SimpleNamespace(
                ok=ok, error="" if ok else "browser_service scrape failed",
                data=data)
            r = client.post(reverse("site_rerun", args=[site.id]))
        site.refresh_from_db()
        assert site.last_scraped_at is not None, \
            "the crash also killed the last_scraped_at stamp"
        return r, site

    def test_empty_output_content_keeps_the_service_count(self, client):
        """scraper_runner returns ``output_content=""`` with HTTP 200 when the
        scraper exits 0 and ``_find_output_file`` finds nothing — no bytes
        published means no credit, and no 500."""
        r, site = self._post(
            client, {"output_content": "", "output_name": "output_a.json",
                     "product_count": 0})
        assert r.status_code == 302, getattr(r, "exception", None)
        assert site.product_count == 0

    def test_missing_output_name_keeps_the_service_count_and_publishes_nothing(
            self, client):
        """Contract note: with no ``output_name`` the ``_content and _name and
        slug`` guard is False, so nothing is published and the service's count
        stands. That is the intended contract (a half-named artifact would be
        an unfindable key on the next read), not an accident — the count the
        service reports still describes the run it just did, and zeroing it
        would un-credit a real re-run on a naming hiccup."""
        from types import SimpleNamespace

        site = _site(scraping_method="playwright")
        with _fm({f"scrapers/{SLUG}/scraper.py": SCRAPER_SRC}) as published, \
                patch(
                    "agents.tools.browser_http.post_scrape_with_retry") as post:
            post.return_value = SimpleNamespace(ok=True, error="", data={
                "output_content": json.dumps(
                    {"products": [{"title": "p", "price": "$1.00"}]}),
                "output_name": "", "product_count": 7})
            r = client.post(reverse("site_rerun", args=[site.id]))
        assert r.status_code == 302, getattr(r, "exception", None)
        assert not [k for k in published if k.endswith(".json")], published
        site.refresh_from_db()
        assert site.product_count == 7
        assert site.last_scraped_at is not None, \
            "the crash also killed the last_scraped_at stamp"

    def test_a_non_ok_reply_stays_a_redirect_at_zero(self, client):
        r, site = self._post(client, {}, ok=False)
        assert r.status_code == 302, getattr(r, "exception", None)
        assert site.product_count == 0

    def test_a_non_json_payload_keeps_the_service_count(self, client):
        """A payload that is not JSON keeps the service's count — never zero a
        real re-run because the bytes could not be read."""
        r, site = self._post(
            client, {"output_content": "<html>no</html>",
                     "output_name": "output_a.json", "product_count": 5})
        assert r.status_code == 302, getattr(r, "exception", None)
        assert site.product_count == 5

    def test_an_empty_slug_falls_through_to_the_service_count(self, client):
        """A Site row with no slug yet is the same fall-through: no publish
        key, but the reply was a good one and the count survives it."""
        r, site = self._post(
            client, {"output_content": json.dumps(
                {"products": [{"title": "p", "price": "$1.00"}]}),
                "output_name": "output_a.json", "product_count": 4},
            slug="")
        assert r.status_code == 302, getattr(r, "exception", None)
        assert site.product_count == 4


class TestMessageSurfaceAndCountProvenance:
    """[GAP round 13] The three residuals round 12 banked nowhere: a message
    that renders twice because a child template kept its own loop, a gated
    publisher whose bytes and credited count can describe different row sets,
    and an operator surface that shipped as unstyled plain text."""

    def test_a_message_renders_exactly_once_on_the_learnt_skills_page(
            self, client):
        """[GAP-B-r12(a)] base.html renders the messages loop OUTSIDE
        ``{% block content %}``, so it fires on every child page — and
        learnt_skills.html still carried the identical loop inside its content
        block, double-rendering every operator notice (learnt_skill_update and
        learnt_skill_delete write ``messages.success``/``messages.error`` then
        redirect to learnt_skills). base.html owns the surface repo-wide now;
        the POST below lands on the ``messages.error`` arm (a nonexistent skill
        name is an honest not-found, no FM mutation), and the count is taken
        from the bytes the operator reads."""
        r = client.post(
            reverse("learnt_skill_update", args=["wave55-render-probe"]),
            {"title": "x", "body": "y"})
        assert r.status_code == 302
        html = client.get(reverse("learnt_skills")).content.decode()
        assert html.count("wave55-render-probe") == 1, \
            f"the notice rendered {html.count('wave55-render-probe')} times"

    def test_when_the_recount_blows_up_the_bytes_and_the_count_agree(
            self, client):
        """[GAP-A-r12] The browser lane overwrote the service's bytes with the
        gated payload BEFORE re-counting, so an exception in the count reached
        the except arm, which credits the service's UNGATED count while the
        already-gated bytes go to the File Master — the audit's C4 shape
        (count, output_file and bytes describing three different things) on a
        bypass publisher. The count now runs first, so a blow-up leaves the
        service's own bytes in place and publisher and count agree."""
        from types import SimpleNamespace

        site = _site(scraping_method="playwright")
        payload = json.dumps({"products": [
            {"title": "p0", "price": "$65.00", "url": "https://x/p/0"},
            {"title": "p1", "price": "$19.50", "url": "https://x/p/1"},
            {"title": "p2", "price": "Save 20%", "url": "https://x/p/2"}]})
        with _fm({f"scrapers/{SLUG}/scraper.py": SCRAPER_SRC}) as published, \
                patch("agents.tools.browser_http.post_scrape_with_retry") as post, \
                patch("src.content_types.count_items_in_output",
                      side_effect=RuntimeError("re-count unavailable")):
            post.return_value = SimpleNamespace(ok=True, error="", data={
                "output_content": payload, "output_name": "output_a.json",
                "product_count": 3})
            client.post(reverse("site_rerun", args=[site.id]))
        published_rows = json.loads(
            published[f"scrapers/{SLUG}/output_a.json"])["products"]
        site.refresh_from_db()
        assert site.product_count == 3
        assert len(published_rows) == 3, published_rows
        assert site.product_count == len(published_rows), \
            "the credited count does not describe the published bytes"

    def test_the_message_surface_is_styled(self, client):
        """[GAP-B-r12(b)] The dropped-rows notice is not undifferentiated plain
        text in the dark UI: base.html styles ``.messages`` and the Django tag
        classes with the palette the file already uses, and the rule reaches
        the rendered page."""
        html = client.get(reverse("learnt_skills")).content.decode()
        assert ".messages>div" in html, "no container rule for .messages"
        assert ".messages>.warning" in html, "no warning tag rule"
        assert ".messages>.error" in html, "no error tag rule"
        assert ".messages>.success" in html, "no success tag rule"
