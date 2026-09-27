"""wave-45d: learnt-skills edit prefill must survive titles starting with
characters from the ``Learned: `` charset.

Prod finding while seeding the FM: ``views.learnt_skills`` compared the
section heading to the wanted title via ``lstrip("Learned: ")`` — lstrip
takes a CHARACTER SET, not a prefix, so a title beginning with any of
``L e a r n d : ' '`` (e.g. "aggregateRating …") was mangled before the
comparison, the prefill came back EMPTY, and saving the editor would have
REPLACED the section with an empty body — silent data loss through the
safe-edit path.
"""

import pytest
from django.test import Client

SKILL_TEXT = (
    "---\nname: jsonld-extraction\ndescription: d\n---\n\nbaseline body\n"
    "## Learned: aggregateRating may be an array instead of a single object\n"
    "**Source:** job 404\n\nreal lesson body\n"
)


@pytest.fixture
def editor(db):
    from django.contrib.auth.models import User

    u = User.objects.create_user("prefill-editor", password="x")
    c = Client()
    c.force_login(u)
    return c


def test_prefill_survives_charset_colliding_title(editor, monkeypatch):
    import src.skills_store as store

    monkeypatch.setattr(store, "list_skills", lambda: ["jsonld-extraction"])
    monkeypatch.setattr(store, "read_skill", lambda name: SKILL_TEXT)
    r = editor.get(
        "/learnt-skills/",
        {
            "skill": "jsonld-extraction",
            "title": "aggregateRating may be an array instead of a single object",
        },
    )
    assert r.status_code == 200
    # The editor must pre-fill with the section's REAL text — an empty
    # prefill is the data-loss trap (save would wipe the section).
    assert "real lesson body" in r.context["edit_body"]


def test_prefill_still_matches_ordinary_title(editor, monkeypatch):
    import src.skills_store as store

    monkeypatch.setattr(store, "list_skills", lambda: ["jsonld-extraction"])
    monkeypatch.setattr(
        store,
        "read_skill",
        lambda name: (
            "---\nname: x\ndescription: d\n---\n\nbase\n"
            "## Learned: Coveo Search Platform — Button-Click Pagination\n"
            "**Source:** job 7\n\ncoveo lesson\n"
        ),
    )
    r = editor.get(
        "/learnt-skills/",
        {
            "skill": "jsonld-extraction",
            "title": "Coveo Search Platform — Button-Click Pagination",
        },
    )
    assert "coveo lesson" in r.context["edit_body"]
