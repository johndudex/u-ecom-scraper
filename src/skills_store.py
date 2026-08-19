"""Skills store — the File Master is the source of truth for skill content.

Why this module exists
    Skill knowledge was split across two divergent copies: the git/image copy
    (``.opencode/skills/``) and whatever the learning agents had appended at
    runtime. On Railway the runtime copy lived in the ephemeral container
    layer and was LOST on every redeploy; locally it landed in the repo via
    the bind-mount and had to be hand-salvaged into commits. This module
    makes the File Master authoritative: agents read and write skills over
    HTTP (``skills/{name}/SKILL.md``), git keeps a read-only *seed + fallback*
    copy, and learnings survive redeploys without ever dirtying git.

Layout (see docs/skills-fm-and-log-scroll-plan.md)
    skills/{skill-name}/SKILL.md        — the live skill (baseline + learned)
    skills/{name}/.seed.yaml             — seed bookkeeping (sha/git-sha/date)
    skills/_audit.jsonl                  — append-only record of every write

Pure-python like src/artifacts.py (no Django import) so the worker, django,
and tests can all use it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The two Claude-UI authoring skills: ~2.7MB of the tree, irrelevant to
# scraping agents. They stay image-only and are excluded from the
# description scan (the per-build full-file reads at subagents.py were the
# real perf bug — this exclusion kills 94% of those bytes).
IMAGE_ONLY_SKILLS = frozenset({"impeccable", "ui-ux-pro-max"})

_FM_KEY = "skills/{name}/SKILL.md"
_SEED_KEY = "skills/{name}/.seed.yaml"
_AUDIT_KEY = "skills/_audit.jsonl"
_VERSION_KEY = "skills/.version"


def _skills_prefix() -> str:
    return "skills/"


def _image_skills_dir() -> Path:
    """The seed/fallback copy shipped inside the image (or repo checkout)."""
    return Path(__file__).resolve().parent.parent / ".opencode" / "skills"


# ─── read path ───────────────────────────────────────────────────────────────


def list_skills(include_image_only: bool = False) -> list[str]:
    """Skill names available to agents (sorted).

    FM first; on FM error falls back to the image copy's directory listing
    with a WARNING (the frozen copy is better than an error mid-job). Missing
    FM (404/empty) is NOT an error here — a fresh volume legitimately lists
    nothing until seeded; the image fallback still answers.
    """
    try:
        import src.artifacts as artifacts

        names = sorted({
            k.split("/")[1]
            for k in artifacts.list_keys(_skills_prefix())
            if k.endswith("/SKILL.md") and len(k.split("/")) >= 3
        })
        if names:
            if not include_image_only:
                return [n for n in names if n not in IMAGE_ONLY_SKILLS]
            return names
        # Empty FM (not yet seeded) — fall through to image listing.
    except RuntimeError:
        logger.warning("skills_store: FILE_MASTER_URL unset — using image skills")
    except Exception as exc:
        logger.warning("skills_store: FM list failed (%s) — using image skills", exc)
    return _list_image_skills(include_image_only)


def _list_image_skills(include_image_only: bool = False) -> list[str]:
    base = _image_skills_dir()
    if not base.is_dir():
        return []
    names = sorted(
        p.parent.name for p in base.glob("*/SKILL.md")
        if include_image_only or p.parent.name not in IMAGE_ONLY_SKILLS
    )
    return names


def read_skill(name: str) -> Optional[str]:
    """Read one skill's SKILL.md. Returns None when not found anywhere.

    Per-key fallback semantics (the sharp edge — see plan):
      - FM has it            → FM content (source of truth)
      - FM 404 for it        → image copy (covers unseeded FM *and* the
                               image-only UI skills) — NO warning spam, this
                               is the designed path for both
      - FM unreachable/unset → image copy + one WARNING per call
    """
    _name = _validate_name(name)
    if _name is None:
        return None
    try:
        import src.artifacts as artifacts

        return artifacts.read_text(_FM_KEY.format(name=_name), timeout=_SKILLS_TIMEOUT_S)
    except FileNotFoundError:
        # 404: designed path for unseeded FM + image-only skills.
        return _read_image_skill(_name)
    except RuntimeError:
        logger.warning("skills_store: FILE_MASTER_URL unset — read '%s' from image", _name)
        return _read_image_skill(_name)
    except Exception as exc:
        logger.warning("skills_store: FM read failed for '%s' (%s) — image fallback", _name, exc)
        return _read_image_skill(_name)


def read_skill_description(name: str) -> str:
    """The frontmatter description for one skill (falls back to the name)."""
    text = read_skill(name) or ""
    return _extract_frontmatter_field(text, "description") or name


def skill_exists(name: str) -> bool:
    _name = _validate_name(name)
    if _name is None:
        return False
    try:
        import src.artifacts as artifacts

        if artifacts.exists(_FM_KEY.format(name=_name)):
            return True
    except Exception:
        pass
    return (_image_skills_dir() / _name / "SKILL.md").is_file()


def _read_image_skill(name: str) -> Optional[str]:
    p = _image_skills_dir() / name / "SKILL.md"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _extract_frontmatter_field(text: str, field: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k.strip() == field:
            return v.strip().strip("\"'")
    return ""


# ─── boot snapshot for the description scan ────────────────────────────────
# _get_skill_descriptions runs on EVERY agent build (every node of every
# job); a per-build FM round-trip per skill would multiply latency. Snapshot
# at first use + invalidate on write. No TTL: descriptions only change on
# create_new_skill (learn_skill appends below the frontmatter and never
# touches it — verified subagents.py:419 reads frontmatter only).

_SNAPSHOT: Optional[dict[str, str]] = None
_SNAPSHOT_AT: float = 0.0


def descriptions_snapshot(force: bool = False) -> dict[str, str]:
    """{skill_name: description} for all non-image-only skills (cached).

    Cached in-process; invalidated by any write through this module and by
    force=True. Cross-process staleness (prefork sibling creates a skill) is
    acceptable per the plan — within-job freshness is not required
    (nav_skill_review runs post-cleanup).
    """
    global _SNAPSHOT, _SNAPSHOT_AT
    if _SNAPSHOT is not None and not force and (time.time() - _SNAPSHOT_AT) < 3600:
        return _SNAPSHOT
    snap: dict[str, str] = {}
    for name in list_skills():
        snap[name] = read_skill_description(name)
    _SNAPSHOT, _SNAPSHOT_AT = snap, time.time()
    return _SNAPSHOT


def _invalidate_snapshot() -> None:
    global _SNAPSHOT
    _SNAPSHOT = None


# ─── write path (learn_skill / create_new_skill) ───────────────────────────
# The RMW is guarded by an flock lock file: the worker is NOT the single FM
# writer (django writes FM too) and runs --concurrency=2 — two
# nav_skill_review agents can race a naive read-append-write into a silent
# lost update. flock works across prefork children (same machine).

_LEARNED_HEADER_RE = re.compile(r"^## Learned:", re.MULTILINE)
_LOCK_TIMEOUT_S = 15.0
# Skills reads sit on the agent-build hot path (descriptions_snapshot does
# 1 list + N sequential reads on the FIRST build per process). An FM that
# accepts TCP but stalls would block boot for minutes at the 120s default —
# 5s fails fast into the image fallback instead (critique gap (b)).
_SKILLS_TIMEOUT_S = 5.0


def append_learned(name: str, title: str, source: str, applicability: str, body: str,
                   *, actor: str = "") -> dict:
    """Append a ``## Learned:`` section (append-only, format enforced here).

    Builds the canonical block the prompts used to merely *ask* the LLM to
    produce; refuses to modify anything above the last existing learned
    header or the frontmatter. Locks around the FM read-modify-write.
    Returns {"ok": bool, "error"/"skill": ..., "appended": bool}.
    """
    _name = _validate_name(name)
    if _name is None:
        return {"ok": False, "error": f"invalid skill name: {name!r}"}
    title = (title or "").strip() or "Untitled learning"
    block = (
        f"\n## Learned: {title}\n"
        f"**Source:** {source or 'unknown'}\n"
        f"**Applicability:** {applicability or 'site-specific'}\n\n"
        f"{(body or '').rstrip()}\n"
    )
    with _skills_lock():
        current = _fm_read_or_fail(_name)  # writes never fall back silently
        if current is None:
            return {"ok": False, "error":
                    f"skill '{_name}' not found in FM (seed first); refusing to "
                    "create via append (use create_new_skill)"}
        if _normalize_match(current, title):
            return {"ok": True, "skill": _name, "appended": False,
                    "note": "a learned section with this title already exists"}
        new_text = current.rstrip("\n") + "\n" + block
        _fm_write(_name, new_text)
        _audit("append", _name, title, actor)
        _invalidate_snapshot()
    return {"ok": True, "skill": _name, "appended": True}


def create_skill(name: str, description: str, body: str, *, actor: str = "") -> dict:
    """Create a brand-new skill. Fails if it already exists anywhere."""
    _name = _validate_name(name)
    if _name is None:
        return {"ok": False, "error": f"invalid skill name: {name!r}"}
    if skill_exists(_name):
        return {"ok": False, "error": f"skill '{_name}' already exists"}
    if _name in IMAGE_ONLY_SKILLS:
        return {"ok": False, "error": f"'{_name}' is reserved (image-only)"}
    front = "---\nname: {n}\ndescription: {d}\n---\n\n".format(
        n=_name, d=(description or _name).replace("\n", " ").strip())
    with _skills_lock():
        _fm_write(_name, front + (body or "").rstrip() + "\n")
        _audit("create", _name, description[:80], actor)
        _invalidate_snapshot()
    return {"ok": True, "skill": _name}


def update_skill_text(name: str, new_text: str, *, actor: str = "") -> dict:
    """Full-text replace (admin/UI path — NOT exposed to agents).

    RISK NOTE: replaces the whole file, so a concurrent append between the
    caller's read and this write is lost. UI callers should prefer
    replace_learned_section (title-scoped, match-inside-lock, keeps any
    concurrent appends to OTHER sections).
    """
    _name = _validate_name(name)
    if _name is None:
        return {"ok": False, "error": f"invalid skill name: {name!r}"}
    with _skills_lock():
        current = _fm_read_or_fail(_name)
        if current is None:
            return {"ok": False, "error": f"skill '{_name}' not found in FM"}
        _fm_write(_name, new_text)
        _audit("update", _name, f"{len(current)}→{len(new_text)} chars", actor)
        _invalidate_snapshot()
    return {"ok": True, "skill": _name}


def replace_learned_section(name: str, title: str, new_section: str, *, actor: str = "") -> dict:
    """Replace ONE ``## Learned:`` section by exact title, inside the lock.

    The admin UI's safe edit path: reads the CURRENT text under the lock,
    swaps only the matching section, writes back. A concurrent agent append
    to any other section survives (unlike a full-text replace built from a
    read taken before the edit form was even opened — the lost-update window
    the critique flagged).
    """
    _name = _validate_name(name)
    if _name is None:
        return {"ok": False, "error": f"invalid skill name: {name!r}"}
    with _skills_lock():
        current = _fm_read_or_fail(_name)
        if current is None:
            return {"ok": False, "error": f"skill '{_name}' not found in FM"}
        pattern = re.compile(
            r"\n?## Learned: " + re.escape((title or "").strip()) + r"\n.*?(?=\n## Learned: |\Z)",
            re.DOTALL,
        )
        m = pattern.search(current)
        if not m:
            return {"ok": False, "error": f"no learned section titled {title!r}"}
        new_text = current[:m.start()] + "\n" + new_section.strip() + "\n" + current[m.end():]
        _fm_write(_name, new_text)
        _audit("replace_learned", _name, title, actor)
        _invalidate_snapshot()
    return {"ok": True, "skill": _name, "replaced": title}


def delete_learned_section(name: str, title: str, *, actor: str = "") -> dict:
    """Remove ONE ``## Learned:`` section by exact title (admin/UI path)."""
    _name = _validate_name(name)
    if _name is None:
        return {"ok": False, "error": f"invalid skill name: {name!r}"}
    with _skills_lock():
        current = _fm_read_or_fail(_name)
        if current is None:
            return {"ok": False, "error": f"skill '{_name}' not found in FM"}
        pattern = re.compile(
            r"\n?## Learned: " + re.escape((title or "").strip()) + r"\n.*?(?=\n## Learned: |\Z)",
            re.DOTALL,
        )
        m = pattern.search(current)
        if not m:
            return {"ok": False, "error": f"no learned section titled {title!r}"}
        _fm_write(_name, current[:m.start()] + current[m.end():])
        _audit("delete_learned", _name, title, actor)
        _invalidate_snapshot()
    return {"ok": True, "skill": _name, "removed": title}


# ─── seeding (called from the celery worker_ready signal) ──────────────────


def seed_from_image(git_sha: str = "") -> dict:
    """Idempotently seed FM with the image's skills. NEVER clobbers learning.

    Per skill: absent in FM → write it. Present and byte-equal to the OLD
    seed → overwrite with the new baseline (git updated a baseline skill;
    FM copy was untouched). Present and drifted (has learned tail) → skip +
    log. Bookkeeping in skills/{name}/.seed.yaml {sha256, git, at}.
    """
    import hashlib

    base = _image_skills_dir()
    stats = {"seeded": [], "refreshed": [], "kept_learned": [], "errors": []}
    if not base.is_dir():
        stats["errors"].append("no image skills dir")
        return stats
    for sk_md in sorted(base.glob("*/SKILL.md")):
        name = sk_md.parent.name
        if name in IMAGE_ONLY_SKILLS:
            continue
        try:
            import src.artifacts as artifacts

            content = sk_md.read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            key = _FM_KEY.format(name=name)
            stamp = {"sha256": digest, "git": git_sha, "at": int(time.time())}
            if not artifacts.exists(key):
                artifacts.write_text(key, content)
                artifacts.write_text(_SEED_KEY.format(name=name),
                                     json.dumps(stamp))
                stats["seeded"].append(name)
                continue
            current = artifacts.read_text(key)
            try:
                old = json.loads(artifacts.read_text(_SEED_KEY.format(name=name)) or "{}")
            except Exception:
                old = {}
            if current == content:
                artifacts.write_text(_SEED_KEY.format(name=name), json.dumps(stamp))
                stats["refreshed"].append(name)  # byte-equal; stamp refresh
            elif old.get("sha256") and _sha256_text(current) == old["sha256"]:
                # Untouched since the last seed → safe baseline refresh.
                artifacts.write_text(key, content)
                artifacts.write_text(_SEED_KEY.format(name=name), json.dumps(stamp))
                stats["refreshed"].append(name)
            else:
                # Drifted (learned tail present) → keep FM version.
                stats["kept_learned"].append(name)
        except Exception as exc:
            stats["errors"].append(f"{name}: {exc}")
    logger.info(
        "skills_store seed: %d seeded, %d refreshed, %d kept-learned, %d errors",
        len(stats["seeded"]), len(stats["refreshed"]),
        len(stats["kept_learned"]), len(stats["errors"]),
    )
    return stats


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─── internals ───────────────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _validate_name(name: str) -> Optional[str]:
    raw = (name or "").strip()
    n = raw.lower()
    # Reject uppercase in the ORIGINAL (canonical skills are lowercase; we
    # do not silently case-fold a typo into a different skill directory).
    if raw != n:
        return None
    if not n or len(n) > 64 or not _NAME_RE.match(n):
        return None
    if ".." in n or "/" in n or "\\" in n:
        return None
    return n


def _normalize_match(current: str, title: str) -> bool:
    """Duplicate-title guard (case/punctuation-insensitive)."""
    want = re.sub(r"[^a-z0-9]+", "", title.lower())
    if not want:
        return False
    for m in _LEARNED_HEADER_RE.finditer(current):
        line_end = current.find("\n", m.end())
        header = current[m.end():line_end if line_end != -1 else len(current)]
        if re.sub(r"[^a-z0-9]+", "", header.lower()) == want:
            return True
    return False


def _fm_read_or_fail(name: str) -> Optional[str]:
    """Read for WRITE paths — no image fallback (would mask a failed write)."""
    import src.artifacts as artifacts

    try:
        return artifacts.read_text(_FM_KEY.format(name=name))
    except FileNotFoundError:
        # Writes must not silently fall back to the image copy; but the
        # skill may legitimately exist only in the image pre-seed. Callers
        # treat None as "not in FM".
        img = _read_image_skill(name)
        if img is not None:
            # Materialize from image then re-read (safe: fresh FM or new
            # skill; the lock is held by the caller).
            artifacts.write_text(_FM_KEY.format(name=name), img)
            return img
        return None
    except Exception as exc:
        logger.error("skills_store: FM read failed for write on '%s': %s", name, exc)
        return None


def _fm_write(name: str, text: str) -> None:
    import src.artifacts as artifacts

    artifacts.write_text(_FM_KEY.format(name=name), text)


def _audit(action: str, skill: str, detail: str, actor: str) -> None:
    """Append-only audit trail so bad learns are findable/undoable-by-hand."""
    try:
        import src.artifacts as artifacts

        existing = ""
        try:
            existing = artifacts.read_text(_AUDIT_KEY)
        except FileNotFoundError:
            pass
        entry = json.dumps({
            "at": int(time.time()), "action": action, "skill": skill,
            "detail": (detail or "")[:200], "actor": actor or "agent",
        })
        artifacts.write_text(_AUDIT_KEY, existing + entry + "\n")
    except Exception as exc:
        logger.warning("skills_store: audit write failed: %s", exc)


class _skills_lock:
    """flock-based lock over the skills RMW (safe across prefork children)."""

    def __init__(self) -> None:
        self._fd = None

    def __enter__(self):
        import fcntl

        lock_path = os.environ.get("SKILLS_LOCK_PATH", "/tmp/skills_store.lock")
        try:
            self._fd = open(lock_path, "a+")
            deadline = time.time() + _LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError("skills_store lock timeout")
                    time.sleep(0.2)
        except Exception as exc:
            logger.warning("skills_store: lock unavailable (%s) — proceeding WITHOUT lock", exc)
            if self._fd:
                try:
                    self._fd.close()
                except OSError:
                    pass
                self._fd = None
            return self

    def __exit__(self, *exc):
        if self._fd is not None:
            import fcntl

            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except OSError:
                pass
        return False
