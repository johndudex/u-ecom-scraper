"""[wave-55 C1] every scraper template anchors ``src`` at the project root.

Prod evidence (audit 2026-10-05, C1): all six templates did
``sys.path.insert(0, os.path.join(dirname(__file__), ".."))``. For a draft at
``/app/workspace/{slug}/scraper_draft.py`` that puts ``/app/workspace`` at
``sys.path[0]``, so ``import src.X`` resolved a site-local
``workspace/src/`` shadow AHEAD of the real pipeline package. That shadow is
what made one writer's stray writes (job 1156) crash 30/79 jobs.

``browser_service/scraper_runner.py`` already pins ``PROJECT_ROOT`` in the
run env, and ``templates/undetected_chromedriver_scraper.py`` already inserts
it — the other six now do the same, with a repo-root fallback computed from
the draft's own path (workspace/{slug}/ → root, scrapers/{slug}/ → root).

No template may insert a parent-of-draft path. That single line is the whole
fix: PYTHONPATH ranks BELOW ``sys.path[0]``, so no env var can rescue a
parent-relative insert.

[round 2] The template list is DERIVED from templates/ (the audit's "all six"
under-counted: ``ssr_div_list_scraper.py`` and
``undetected_chromedriver_scraper.py`` are live too — template_selector.py and
subagents.py hand both to the writer), so a template can no longer hide
outside a hardcoded tuple.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

TEMPLATES_DIR = os.path.join(ROOT, "templates")
AGENTS_DIR = os.path.join(ROOT, ".opencode", "agents")
# Skills are the other runtime-served teacher: skills_store._read_image_skill()
# hands SKILL.md bodies to the writer through load_skill, so a banned insert
# taught there reaches a draft exactly like a prompt does.
SKILLS_DIR = os.path.join(ROOT, ".opencode", "skills")
# The FM mirror (the volume skills_store reads and writes, bind-mounted here).
# Its .seed.yaml stamps are what make seed_from_image able to tell an
# untouched baseline from a learned tail.
FM_SKILLS_DIR = os.path.join(ROOT, "shared-data", "skills")

# A ``src.*`` import anywhere in the file — the reason a template stages a
# sys.path at all (module-level or inside a function).
_SRC_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+src\.", re.MULTILINE)

# dirname(draft)/.. — resolves into workspace/ for a staged draft. The bug, in
# both spellings the live templates used: the literal dirname(abspath(__file__))
# form and the aliased / SCRIPT_DIR form.
PARENT_RELATIVE = 'os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")'
PARENT_RELATIVE_FORMS = (
    PARENT_RELATIVE,
    'os.path.join(SCRIPT_DIR, "..")',
    "os.path.join(SCRIPT_DIR, '..')",
)

# Same bug, spelled without abspath: for a staged absolute __file__ both
# dirname() forms resolve identically, so the pin has to match both (the plain
# string constant above only sees the abspath spelling — proxy-config/SKILL.md
# shipped the short one and sailed past it). The ``..`` operand takes either
# quote: the tail used to hard-code the double one, so every single-quoted
# spelling of the same insert was invisible (round-27 gap).
PARENT_RELATIVE_RE = re.compile(
    r"sys\.path\.insert\(0,\s*"
    r"os\.path\.join\(\s*"
    # the second ``)`` is abspath's — only present in the long spelling
    r"os\.path\.dirname\(\s*(?:os\.path\.abspath\(\s*)?__file__\s*\)(?:\s*\))?\s*,"
    r"\s*['\"]\.\.['\"]\s*\)\s*\)"
)

# browser_service/ is deliberately in none of the scanned sets below — its two live inserts are top-level service scripts (dirname/.. resolves to /app), not staged drafts.

# The anchor block starts at the first of these (the UC template defines
# SCRIPT_DIR on its own line and its PROJECT_ROOT fallback is a one-liner that
# references it, so that line has to be inside the executed block).
_ANCHOR_STARTS = ("SCRIPT_DIR = ", "PROJECT_ROOT = ")


def _src(name):
    with open(os.path.join(TEMPLATES_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _template_names():
    """Every template that stages a path for a ``src.*`` import."""
    return tuple(
        sorted(
            n
            for n in os.listdir(TEMPLATES_DIR)
            if n.endswith(".py") and "sys.path.insert" in _src(n)
        )
    )


def _normalized_src(name):
    """Templates that import os/sys under a local alias still spell the same
    bug — normalise the alias before matching."""
    return _src(name).replace("_os.", "os.").replace("_sys.", "sys.")


TEMPLATES = _template_names()


def _anchor_block(name):
    """The template's own path-setup statements (PROJECT_ROOT def + insert)."""
    lines = _src(name).splitlines()
    start = min(
        i
        for i, ln in enumerate(lines)
        if any(ln.startswith(prefix) for prefix in _ANCHOR_STARTS)
    )
    assert start is not None, f"{name}: no PROJECT_ROOT definition"
    end = next(
        (i for i, ln in enumerate(lines) if "sys.path.insert(0, PROJECT_ROOT)" in ln),
        None,
    )
    assert end is not None and start < end, (
        f"{name}: PROJECT_ROOT must be inserted into sys.path"
    )
    return "\n".join(lines[start:end + 1])


def _teaches_parent_relative(body):
    """The regex plus both literal forms — the aliased SCRIPT_DIR spelling the
    regex was never wide enough for (it is the shape proxy-config actually
    shipped, and the reason the teach-surface legs share this predicate
    instead of the bare regex)."""
    if PARENT_RELATIVE_RE.search(body):
        return True
    return any(form in body for form in PARENT_RELATIVE_FORMS)


def _assert_teach_surface_clean(text, label, hint=""):
    """One predicate + one message for every teach-surface leg — all four of
    them (the templates, the writer prompts, the skills image glob and the
    FM-served walk) call it, so widening the predicate can never leave a leg
    behind on the narrower shape. It did not always: the round-25 gap left the
    two skill legs on the bare regex after ``_teaches_parent_relative``
    existed, and the round-26 gap left the template and prompt legs on the
    ``PARENT_RELATIVE_FORMS`` literals — either way the short
    ``dirname(__file__)`` spelling proxy-config shipped stayed invisible on
    exactly the legs that did not call this (each leg now has a mutation pin
    that walks its own poison through the real leg).
    ``hint`` carries the leg-specific remedy (the FM leg's reseed hint)."""
    assert not _teaches_parent_relative(text), (
        f"{label}: teaches the parent-of-draft sys.path insert the templates "
        f"dropped — teach the PROJECT_ROOT anchor instead{hint}"
    )


def _assert_fm_seed_stamps_are_safe():
    """The FM seed-stamp contract over ``FM_SKILLS_DIR``.

    ``seed_from_image`` overwrites a skill when ``stamp == sha256(body)`` and
    files it under ``kept_learned`` — never repaired, never reseeded — when
    they disagree, so a stamp that does not describe the body is either a
    learned tail or a poison; only the body can tell them apart.

    Two shapes are safe, one is a fail:
    - body clean, stamp == sha256(body): reseedable — the next seed washes
      out anything that lands later.
    - body clean, stamp stale: a legitimate learned tail, exactly what the
      kept_learned arm preserves — repair it through the learnt-skills UI,
      never by reverting the learning.
    - body teaching the parent-of-draft insert: poison, whatever the stamp
      says — sealed, seed_from_image files it as "refreshed"; stale, as
      kept_learned. Neither reseed washes it out before it is served.
    """
    assert os.path.isdir(FM_SKILLS_DIR), (
        "no shared-data/skills mirror — the FM seed contract is unverified"
    )
    bodies = glob.glob(os.path.join(FM_SKILLS_DIR, "*", "SKILL.md"))
    stamped = glob.glob(os.path.join(FM_SKILLS_DIR, "*", ".seed.yaml"))
    assert bodies, "no FM mirror skills — the seed walk asserted nothing"
    body_dirs = {os.path.basename(os.path.dirname(p)) for p in bodies}
    stamp_dirs = {os.path.basename(os.path.dirname(p)) for p in stamped}
    assert stamp_dirs == body_dirs, (
        f"unsealed skills {sorted(body_dirs - stamp_dirs)} and orphaned "
        f"stamps {sorted(stamp_dirs - body_dirs)} — every FM skill needs "
        "a .seed.yaml or seed_from_image cannot tell stale from learned"
    )
    for path in sorted(bodies):
        name = os.path.basename(os.path.dirname(path))
        with open(os.path.join(FM_SKILLS_DIR, name, ".seed.yaml"),
                  encoding="utf-8") as fh:
            stamp = json.load(fh)
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        # the body decides, whatever the stamp says — a stamp-first branch was
        # vacuous here (a matching stamp on a poisoned body sailed through as
        # "reseedable", see the poison test below)
        assert not _teaches_parent_relative(body), (
            f"{name}: drifted with a poison tail — the served body teaches "
            f"the parent-of-draft insert (stamp {stamp.get('sha256')}, body "
            f"{digest}); reseed or repair the learned body (learnt-skills "
            "UI), the image walk cannot see the volume"
        )
        # clean body: a matching stamp is reseedable, a stale one is the
        # kept_learned arm's own shape — both stay green


class TestTemplateSet:
    def test_no_src_importing_template_escapes_the_contract(self):
        """The derivation is the guard: any file that imports ``src.*`` is in
        TEMPLATES, and nothing without one is (dagster_template.py imports
        neither sys.path nor src.* — it is a class library, not a staged
        draft)."""
        for n in os.listdir(TEMPLATES_DIR):
            if not n.endswith(".py"):
                continue
            if _SRC_IMPORT_RE.search(_src(n)):
                assert n in TEMPLATES, f"{n} imports src.* but is not in contract"
            else:
                assert n not in TEMPLATES, f"{n} imports no src.* — why in contract?"

    def test_the_undercounted_two_are_in_contract(self):
        """Round 1 enforced the contract over a hardcoded 6-name tuple that
        structurally could not see these two live templates."""
        assert "ssr_div_list_scraper.py" in TEMPLATES
        assert "undetected_chromedriver_scraper.py" in TEMPLATES


class TestNoParentRelativeInsert:
    def test_no_template_inserts_parent_of_draft(self):
        for name in TEMPLATES:
            _assert_teach_surface_clean(_normalized_src(name), name)

    def test_the_short_dirname_spelling_is_caught_on_the_template_leg(
        self, tmp_path, monkeypatch
    ):
        """The audit's C1 #4 surface itself carried the SHORT
        ``dirname(__file__)`` spelling, which the ``PARENT_RELATIVE_FORMS``
        literals never contained — a template leg still scanning the literals
        passes over exactly the line this wave removes. Both module globals
        are patched: the leg iterates ``TEMPLATES`` (computed at import) and
        ``_src`` reads ``TEMPLATES_DIR`` at call time."""
        import pytest

        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "requests_scraper.py").write_text(
            "import os, sys\n"
            "from src import artifacts\n"
            'sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))\n'
            "sys.path.insert(0, PROJECT_ROOT)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sys.modules[__name__], "TEMPLATES_DIR", str(tdir))
        monkeypatch.setattr(
            sys.modules[__name__], "TEMPLATES", ("requests_scraper.py",)
        )
        with pytest.raises(AssertionError, match="requests_scraper.py"):
            TestNoParentRelativeInsert().test_no_template_inserts_parent_of_draft()

    def test_the_single_quoted_short_spelling_is_caught_on_the_template_leg(
        self, tmp_path, monkeypatch
    ):
        """The same short spelling in single quotes: the regex tail hard-coded
        the double quote and the literal forms are double-quoted too, so the
        predicate was blind to the spelling a hand-written draft is likeliest
        to carry. Walked through the real leg like the double-quoted pin
        above."""
        import pytest

        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "requests_scraper.py").write_text(
            "import os, sys\n"
            "from src import artifacts\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
            "sys.path.insert(0, PROJECT_ROOT)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sys.modules[__name__], "TEMPLATES_DIR", str(tdir))
        monkeypatch.setattr(
            sys.modules[__name__], "TEMPLATES", ("requests_scraper.py",)
        )
        with pytest.raises(AssertionError, match="requests_scraper.py"):
            TestNoParentRelativeInsert().test_no_template_inserts_parent_of_draft()

    def test_every_template_inserts_project_root(self):
        for name in TEMPLATES:
            assert "sys.path.insert(0, PROJECT_ROOT)" in _src(name), name

    def test_no_writer_prompt_teaches_the_banned_insert(self):
        """The templates were not the only place the parent-of-draft insert
        was taught: the writer prompt directory shipped the same line as its
        Proxy Integration recipe, so a writer told to integrate proxies by
        hand re-introduces the C1 shadow path in a draft no template check
        can see. Whatever the prompts teach has to be the shipped anchor.

        Every known spelling, not just the abspath literal — proxy-config
        shipped the aliased SCRIPT_DIR form and sailed past the exact-string
        pin this test used to use (see this file's docstring)."""
        for name in sorted(os.listdir(AGENTS_DIR)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(AGENTS_DIR, name), encoding="utf-8") as fh:
                text = fh.read()
            _assert_teach_surface_clean(
                text, os.path.relpath(os.path.join(AGENTS_DIR, name), ROOT)
            )

    def test_the_short_dirname_spelling_is_caught_on_the_prompt_leg(
        self, tmp_path, monkeypatch
    ):
        """HEAD's proxy-config poison shipped the SHORT ``dirname(__file__)``
        spelling, which the ``PARENT_RELATIVE_FORMS`` literals never contained
        (the long abspath form and the aliased SCRIPT_DIR form only) — so a
        prompt leg still scanning the literals passes over exactly the line
        that reached prod. The pin walks that line through the real leg."""
        import pytest

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "code-writer.md").write_text(
            'sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(sys.modules[__name__], "AGENTS_DIR", str(agents))
        with pytest.raises(AssertionError, match="code-writer"):
            TestNoParentRelativeInsert().test_no_writer_prompt_teaches_the_banned_insert()

    def test_no_skill_teaches_the_banned_insert(self):
        """SKILL.md bodies are runtime-served to the writer via
        ``skills_store._read_image_skill``/``load_skill``, so a skill teaching
        the parent-of-draft insert re-creates the C1 shadow path in a draft no
        template or prompt check can see. proxy-config shipped exactly that on
        its Python-integration snippet."""
        for path in sorted(glob.glob(os.path.join(SKILLS_DIR, "*", "SKILL.md"))):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            _assert_teach_surface_clean(text, os.path.relpath(path, ROOT))

    def test_the_aliased_script_dir_spelling_is_caught_on_a_skill_leg(
        self, tmp_path, monkeypatch
    ):
        """The mutation that closed the narrow-leg gap: the skills image leg
        and the FM leg both kept the bare ``PARENT_RELATIVE_RE`` after
        ``_teaches_parent_relative`` existed, so the aliased SCRIPT_DIR
        spelling — the exact shape proxy-config shipped — was invisible on
        both (proved live: ``PARENT_RELATIVE_RE.search(...)`` is False on it,
        ``_teaches_parent_relative`` is True). The pin calls the REAL leg over
        a poisoned mirror — the monkeypatched ``SKILLS_DIR`` is read inside the
        leg's own glob at call time — so a leg that stops calling the helper,
        or a helper narrowed back to the bare regex, reddens here (a pin that
        re-implemented the glob inline stayed green under both mutations)."""
        import pytest

        mirror = tmp_path / "skills" / "proxy-config"
        mirror.mkdir(parents=True)
        (mirror / "SKILL.md").write_text(
            "## Proxy Integration\n"
            'sys.path.insert(0, os.path.join(SCRIPT_DIR, ".."))\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sys.modules[__name__], "SKILLS_DIR", str(tmp_path / "skills")
        )
        with pytest.raises(AssertionError, match="proxy-config"):
            TestNoParentRelativeInsert().test_no_skill_teaches_the_banned_insert()

    def test_the_runtime_served_fm_copy_is_clean_too(self, monkeypatch):
        """The walk above scans the image directory, but read_skill() is
        FM-FIRST — the image copy is only the 404/unreachable fallback, so a
        clean image with a stale FM copy ships green while the writer is still
        taught the banned insert (exactly how the round that added the skill
        pin passed).

        Two things keep this pin honest. It walks EVERY served skill (one
        stale FM body out of 24 ships green otherwise), and it proves the FM
        leg was actually exercised: read_skill() swallows every FM error and
        quietly serves the image copy, so an FM-less host would otherwise
        re-read the image here and pass while asserting nothing about what
        the writer is really served."""
        import pytest

        import src.artifacts as artifacts
        import src.skills_store as ss

        # read_skill re-imports src.artifacts at call time, so patching the
        # module attribute intercepts it. Recording only SUCCESSFUL reads is
        # the point: a read that raises (FILE_MASTER_URL unset, FM erroring)
        # is what triggers the image fallback, and that leg must never be
        # allowed to pose as an FM verdict.
        calls: list[str] = []
        real = artifacts.read_text

        def _recording_read_text(key, timeout=None):
            text = real(key, timeout=timeout)
            calls.append(key)
            return text

        monkeypatch.setattr(artifacts, "read_text", _recording_read_text)

        served = list(ss.list_skills())
        assert served, "no served skills — the FM walk asserted nothing"

        for name in served:
            if not ss.skill_exists(name):
                pytest.skip(f"{name} not served")
            calls.clear()
            body = ss.read_skill(name)
            assert any(
                c.startswith("skills/") and c.endswith("/SKILL.md") for c in calls
            ), (
                f"read_skill fell back to the image copy for {name} — FM leg "
                "not exercised; FILE_MASTER_URL unset or FM erroring"
            )
            _assert_teach_surface_clean(
                body or "",
                f"the FM-served {name} body",
                hint=" — reseed the FM from the image "
                "(skills_store.seed_from_image); read_skill() is FM-FIRST, the "
                "image glob above is only the fallback",
            )

    def test_every_fm_seed_stamp_matches_the_body_it_stamps(self):
        """``seed_from_image`` can only keep an FM copy clean while its
        ``.seed.yaml`` stamp describes the body that is actually there: it
        overwrites a skill when ``stamp == sha256(body)``, and files the skill
        under ``kept_learned`` — never repaired, never reseeded — when they
        disagree. Set equality keeps every skill sealed; the per-skill walk
        decides stale-vs-poison from the body (the two tests below pin its
        branches), and the image walk above cannot see the volume either way."""
        _assert_fm_seed_stamps_are_safe()

    def test_a_poison_tail_fails_even_when_the_stamp_describes_it(
        self, tmp_path, monkeypatch
    ):
        """The stamp pin this file used to use was vacuous for a poisoned body
        whose stamp was refreshed to match it: seed_from_image saw a clean
        seal and kept serving the insert. The walk reads the body, so a
        poisoned mirror fails whatever the stamp says."""
        import pytest

        body = (
            "## Proxy Integration\n"
            "import os, sys\n"
            'sys.path.insert(0, os.path.join(SCRIPT_DIR, ".."))\n'
        )
        mirror = tmp_path / "skills" / "proxy-config"
        mirror.mkdir(parents=True)
        (mirror / "SKILL.md").write_text(body, encoding="utf-8")
        (mirror / ".seed.yaml").write_text(
            json.dumps(
                {
                    "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "git": "",
                    "at": 0,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sys.modules[__name__], "FM_SKILLS_DIR", str(tmp_path / "skills")
        )
        with pytest.raises(AssertionError, match="drifted with a poison tail"):
            _assert_fm_seed_stamps_are_safe()

    def test_a_benign_learned_tail_is_not_a_poison(self, tmp_path, monkeypatch):
        """A learned tail is what the kept_learned arm exists to preserve —
        the old strict message invited 'fixing' this red by reverting real
        learning. Both safe shapes stay green: the tail resealed (reseedable)
        and the tail left stale (kept_learned, body still clean)."""
        mirror = tmp_path / "skills" / "some-skill"
        mirror.mkdir(parents=True)
        base = "## Proxy Integration\nteach the PROJECT_ROOT anchor\n"
        body = base + "## Learned: note\n"
        (mirror / "SKILL.md").write_text(body, encoding="utf-8")
        stamp = mirror / ".seed.yaml"
        stamp.write_text(
            json.dumps(
                {
                    "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "git": "",
                    "at": 0,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sys.modules[__name__], "FM_SKILLS_DIR", str(tmp_path / "skills")
        )
        _assert_fm_seed_stamps_are_safe()  # resealed → reseedable
        # the same tail with the stamp left at the pre-tail sha is the
        # kept_learned shape — a clean body, so still not a poison
        stamp.write_text(
            json.dumps(
                {
                    "sha256": hashlib.sha256(base.encode("utf-8")).hexdigest(),
                    "git": "",
                    "at": 0,
                }
            ),
            encoding="utf-8",
        )
        _assert_fm_seed_stamps_are_safe()

    def test_proxy_config_skill_is_in_the_scanned_set(self):
        """The walk above must not silently go empty: proxy-config is the skill
        that carried the banned insert, so its absence from the glob means the
        pin stopped covering the case that motivated it."""
        assert os.path.isfile(os.path.join(SKILLS_DIR, "proxy-config", "SKILL.md"))
        scanned = {
            os.path.basename(os.path.dirname(p))
            for p in glob.glob(os.path.join(SKILLS_DIR, "*", "SKILL.md"))
        }
        assert "proxy-config" in scanned

    def test_the_regex_sees_both_spellings(self):
        """dirname(__file__) and dirname(abspath(__file__)) resolve the same way
        for a staged absolute draft path — a pin that only matches one of them
        passes while the hazard is live. Both quote styles too: the tail used
        to hard-code the double quote, so the single-quoted insert (and the
        single-quoted SCRIPT_DIR alias, which only the literal forms can see —
        the regex keys on ``__file__``) sailed past every leg."""
        for line in (
            'sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))',
            'sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))',
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))",
            "sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))",
        ):
            assert PARENT_RELATIVE_RE.search(line), line
        for line in (
            "sys.path.insert(0, os.path.join(SCRIPT_DIR, '..'))",
            'os.path.join(SCRIPT_DIR, "..")',
            "os.path.join(SCRIPT_DIR, '..')",
        ):
            assert _teaches_parent_relative(line), line
        # ...and must stay quiet on the shipped anchor and its env-pinned form
        for line in (
            "sys.path.insert(0, PROJECT_ROOT)",
            'sys.path.insert(0, os.environ.get("PROJECT_ROOT", "/app"))',
            'sys.path.insert(1, os.path.join(os.path.dirname(__file__), ".."))',
            "sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))",
        ):
            assert not _teaches_parent_relative(line), line

    def test_anchor_mentions_the_root_fallback(self):
        """The fallback must be repo-root math (two levels above the draft),
        not the parent-of-file form this wave removes."""
        for name in TEMPLATES:
            flat = " ".join(_anchor_block(name).split())
            assert '"..", ".."' in flat, name
            # env var first — the run env pins it, the path math is the
            # fallback (UC's one-liner and the multi-line blocks both qualify)
            assert any(
                form in flat
                for form in (
                    'os.environ.get( "PROJECT_ROOT",',
                    'os.environ.get("PROJECT_ROOT",',
                )
            ), name


class TestAnchorMath:
    """Execute the template's own anchor lines against a staged draft path.

    This is the assertion that matters: the resolved root must be the REPO
    ROOT above the draft, never the parent directory the old insert used.
    """

    def _resolve(self, name, draft_path, monkeypatch, env_root=None):
        if env_root is None:
            monkeypatch.delenv("PROJECT_ROOT", raising=False)
        else:
            monkeypatch.setenv("PROJECT_ROOT", env_root)
        namespace = {"__file__": str(draft_path)}
        exec("import os, sys\n" + _anchor_block(name), namespace)  # noqa: S102
        return namespace["PROJECT_ROOT"]

    def test_workspace_draft_resolves_to_repo_root(self, tmp_path, monkeypatch):
        slug_dir = tmp_path / "workspace" / "underarmour-com"
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper_draft.py"
        draft.write_text("# staged draft\n")
        for name in TEMPLATES:
            assert self._resolve(name, draft, monkeypatch) == str(tmp_path), name

    def test_finalized_scraper_resolves_to_repo_root(self, tmp_path, monkeypatch):
        slug_dir = tmp_path / "scrapers" / "underarmour-com"
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper.py"
        draft.write_text("# promoted scraper\n")
        for name in TEMPLATES:
            assert self._resolve(name, draft, monkeypatch) == str(tmp_path), name

    def test_staged_run_dir_uses_the_env_var(self, tmp_path, monkeypatch):
        """browser_service stages drafts into a throwaway run dir where no
        path math can find the root — it sets PROJECT_ROOT in the run env, and
        the template must honour it."""
        run_dir = tmp_path / "run" / "abc123"
        run_dir.mkdir(parents=True)
        draft = run_dir / "scraper_draft.py"
        draft.write_text("# staged\n")
        for name in TEMPLATES:
            assert (
                self._resolve(name, draft, monkeypatch, env_root="/app") == "/app"
            ), name
