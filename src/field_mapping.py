"""[wave-36] User field-chip → canonical output-key mapping.

docs/plans/wave36-field-mapping-plan.md §1. The intake chips are free text
(``'product name'``, ``'avaliability'``); nine consumers treat them VERBATIM as
the output-key contract while generated scrapers emit canonical keys — job 658
shipped name-less records because the finalize prune deleted exactly those
canonical keys.

Layers (resolve_chain):
1. alias table (measured cases; cheap, deterministic)
2. one-shot small LLM over the UNRESOLVED chips only (single attempt — the
   classified 6-retry ladder would put ~2 min on the job-start critical path;
   round-1 F6), cached per (site_slug, chips-hash)  [lands with task 1b wiring]
3. abstaining fuzzy fallback (threshold 0.78; abstain on near-ties)
4. verbatim passthrough == today's behavior

Kill-switch: ``FIELD_MAPPING_ENABLED=0`` → identity (verbatim chips).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re

# ── Alias table (measured from prod failures + registry vocabulary) ─────────
# Keys are lowercased, stripped chips. Values are canonical REGISTRY field
# names (validated against the content-type registry at resolve time) or
# "CUSTOM" (keep the chip, sanitized, as an explicit extraction target).
ALIAS_FIELDS: dict[str, str] = {
    # measured 658-class chips
    "product name": "title",
    "product title": "title",
    "name": "title",
    "avaliability": "availability",
    "avalibility": "availability",
    "availibility": "availability",
    "in-stock?": "availability",
    "in stock": "availability",
    "stock status": "availability",
    "rrp": "original_price",
    "was-price": "original_price",
    "was price": "original_price",
    "list price": "original_price",
    "sku code": "sku",
    "sku": "sku",
    "item no": "sku",
    "item number": "sku",
    "desc": "description",
    "product description": "description",
    "cost": "price",
    "sales price": "price",
    "sale-price": "price",
    "discounted price": "price",
    "url": "url",
    "product url": "url",
    "link": "url",
    "img": "images",
    "image": "images",
    "product image": "images",
    "currency code": "currency",
}

FIELD_MAPPING_ENABLED_ENV = "FIELD_MAPPING_ENABLED"
# Bump when the alias table or the registry field vocabulary changes in a way
# that must invalidate persisted blobs and Site caches.
REGISTRY_VERSION = "wave36-1"

_FUZZY_THRESHOLD = 0.78

_CUSTOM_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def sanitize_custom_key(chip: str) -> str:
    """CHIP-VERBATIM key kept for CUSTOM targets — snake_cased when it already
    matches the key charset, else the chip passes through untouched (the
    record-rename is identity for it and the writer is told to extract it)."""
    guess = re.sub(r"[^a-z0-9_]+", "_", chip.lower()).strip("_")
    if guess and _CUSTOM_RE.fullmatch(guess):
        return guess
    return chip


def is_custom_target(target: str | None) -> bool:
    return target == "CUSTOM"


def custom_keys_from_mapping(mapping: dict | None) -> list[str]:
    """Chip keys whose resolved target is CUSTOM — they stay in the record
    contract verbatim and must be admitted by every pre-rename consumer."""
    out: list[str] = []
    for chip, entry in (_blob_mapping(mapping) or {}).items():
        if isinstance(entry, dict) and entry.get("target") == "CUSTOM":
            out.append(sanitize_custom_key(chip))
    return out


def union_output_fields(state: dict) -> list[str]:
    """The TWO-VOCABULARY contract (round-2 B1): drafts emit BOTH canonical and
    chip-verbatim record keys, so every consumer that runs BEFORE the
    record-key rename admits resolved ∪ raw ∪ custom, deduped, resolved first.
    NEVER use this at the finalize prune — that one is post-rename and takes
    ``resolved_fields`` only."""
    resolved = list(state.get("resolved_fields") or [])
    raw = list(state.get("target_fields") or [])
    custom = custom_keys_from_mapping(state.get("field_mapping"))
    out: list[str] = []
    for name in resolved + raw + custom:
        if name and name not in out:
            out.append(name)
    return out


def alias_lookup(chip: str) -> str | None:
    """Layer 1: exact alias-table hit (canonical name or "CUSTOM"), else None."""
    return ALIAS_FIELDS.get(chip.strip().lower())


# ── Persisted-contract accessors (ScrapeJob.field_mapping blob) ─────────────
# The column stores {"mapping": {...}, "resolved_fields": [...],
# "content_hash": "..."} — the finalize prune reads the SAME contract the
# pipeline enforced (plan §1b). The bare inner ``{chip: entry}`` dict is
# accepted too (resolver call sites pass it around).


def _blob_mapping(blob: dict | None) -> dict | None:
    """Accept the full blob or the bare chip→entry mapping; None when neither."""
    if not isinstance(blob, dict) or not blob:
        return None
    mapping = blob.get("mapping")
    if isinstance(mapping, dict):
        return mapping or None
    # Bare-mapping shape: every value is an entry dict carrying "target".
    if all(isinstance(v, dict) and "target" in v for v in blob.values()):
        return blob
    return None


def resolved_fields_from_mapping(blob: dict | None) -> list[str] | None:
    """Ordered canonical names from a persisted mapping blob. None when
    absent/empty → the caller falls back to identity
    (``content_types.schema_field_names``) — legacy byte-compat."""
    mapping = _blob_mapping(blob)
    if not mapping:
        return None
    out: list[str] = []
    for chip, entry in mapping.items():
        target = entry.get("target") if isinstance(entry, dict) else None
        if target == "CUSTOM":
            out.append(sanitize_custom_key(chip))
        elif target:
            out.append(str(target))
    return out or None


def rename_map_from_mapping(blob: dict | None) -> dict[str, str]:
    """{raw record key: resolved key} for keys that actually CHANGE.
    CUSTOM chips map to their sanitized key; identity targets are omitted."""
    mapping = _blob_mapping(blob)
    if not mapping:
        return {}
    out: dict[str, str] = {}
    for chip, entry in mapping.items():
        target = entry.get("target") if isinstance(entry, dict) else None
        resolved = sanitize_custom_key(chip) if target == "CUSTOM" else (
            str(target) if target else None
        )
        if resolved and resolved != chip:
            out[chip] = resolved
    return out


# ── Resolver (plan §1b): alias → LLM leg → abstaining fuzzy → verbatim ──────
# Pure orchestration here; the LLM adapter is injected by the caller (tasks.py
# wiring) so this module stays import-light and testable without Django.


def mapping_enabled() -> bool:
    """Kill-switch: FIELD_MAPPING_ENABLED=0 → identity everywhere."""
    return str(os.environ.get(FIELD_MAPPING_ENABLED_ENV, "1")).strip().lower() \
        not in ("0", "false", "no", "off")


def content_hash_for(chips: list[str], page_type: str) -> str:
    """Idempotency key: (chips, page_type, registry-version) — a chip edit or
    a registry bump re-resolves; an identical contract reuses the blob (F3)."""
    payload = repr((sorted(str(c).strip() for c in chips), str(page_type),
                    REGISTRY_VERSION))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def registry_field_names(page_type: str) -> set[str]:
    """Canonical target names admitted by the content-type registry. Empty
    when the page type is unknown (validation then admits alias targets —
    an unknown page type has no registry to contradict them)."""
    try:
        from src.content_types import get_content_type

        ct = get_content_type((page_type or "").lower())
    except Exception:
        return set()
    if ct is None:
        return set()
    return {f.name for f in ct.all_fields}


def _fuzzy_lookup(chip: str, candidates: dict[str, str]) -> str | None:
    """Abstaining fuzzy match (threshold 0.78; abstain on near-ties within
    0.1 — a near-tie means the chip is ambiguous, not misnamed). Candidates
    maps canonical name → match text (name + label). Returns the name or
    None to abstain."""
    lowered = chip.strip().lower()
    scored = sorted(
        ((difflib.SequenceMatcher(None, lowered, text.lower()).ratio(), name)
         for name, text in candidates.items()),
        reverse=True,
    )
    if not scored or scored[0][0] < _FUZZY_THRESHOLD:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.1:
        return None  # near-tie: ambiguous chip
    return scored[0][1]


def resolve_mapping(
    chips: list[str],
    page_type: str,
    *,
    llm_fn=None,
    site_context: str = "",
) -> tuple[dict[str, dict], list[str]]:
    """Resolve intake chips to the record contract. NEVER raises.

    Chain per chip: alias table → LLM leg (unresolved chips only, via the
    injected ``llm_fn``) → abstaining fuzzy → verbatim passthrough (today's
    behavior). Deterministic validation afterwards: canonical targets must
    exist in the registry (else CUSTOM); duplicate targets → highest
    confidence wins, the rest demoted to CUSTOM and flagged.

    Returns ``(mapping, warnings)`` where mapping is
    ``{chip: {target, confidence, rationale, source}}``.
    """
    mapping: dict[str, dict] = {}
    warnings: list[str] = []
    clean_chips = [str(c).strip() for c in (chips or []) if str(c).strip()]
    if not clean_chips:
        return mapping, warnings

    try:
        from src.content_types import get_content_type

        ct = get_content_type((page_type or "").lower())
    except Exception:
        ct = None
    names = {f.name for f in ct.all_fields} if ct is not None else set()
    # Fuzzy candidates: canonical name → "name label" match text.
    candidates: dict[str, str] = {}
    if ct is not None:
        for f in ct.all_fields:
            candidates[f.name] = f"{f.name} {f.label}".strip()
    else:
        candidates = {n: n for n in names}

    unresolved: list[str] = []
    for chip in clean_chips:
        target = alias_lookup(chip)
        if target and target != "CUSTOM":
            if names and target not in names:
                mapping[chip] = {
                    "target": "CUSTOM", "confidence": 0.9,
                    "rationale": f"alias '{target}' not in registry",
                    "source": "alias",
                }
            else:
                mapping[chip] = {
                    "target": target, "confidence": 0.97,
                    "rationale": "alias", "source": "alias",
                }
        elif target == "CUSTOM":
            mapping[chip] = {
                "target": "CUSTOM", "confidence": 0.9,
                "rationale": "alias verdict: domain term", "source": "alias",
            }
        else:
            unresolved.append(chip)

    if unresolved and llm_fn is not None:
        try:
            llm_result = llm_fn(
                unresolved, page_type,
                registry_block="\n".join(
                    f"- {n}" for n in sorted(candidates)
                ),
                site_context=site_context,
            ) or {}
        except Exception:
            llm_result = {}
        for chip in unresolved:
            entry = llm_result.get(chip)
            if isinstance(entry, dict) and entry.get("target"):
                mapping[chip] = {
                    "target": str(entry["target"]),
                    "confidence": float(entry.get("confidence") or 0.5),
                    "rationale": str(entry.get("rationale") or "llm"),
                    "source": "llm",
                }
            else:
                _fuzzy_fallback(mapping, warnings, chip, candidates)
    elif unresolved:
        for chip in unresolved:
            _fuzzy_fallback(mapping, warnings, chip, candidates)

    # Verbatim passthrough for anything still unresolved == today's behavior.
    for chip in clean_chips:
        if chip not in mapping:
            mapping[chip] = {
                "target": chip, "confidence": 1.0,
                "rationale": "verbatim", "source": "verbatim",
            }

    _validate_mapping(mapping, warnings, names)
    return mapping, warnings


def _fuzzy_fallback(
    mapping: dict, warnings: list[str], chip: str, candidates: dict[str, str],
) -> None:
    if not candidates:
        return  # no registry vocabulary → straight to verbatim
    hit = _fuzzy_lookup(chip, candidates)
    if hit:
        mapping[chip] = {
            "target": hit, "confidence": round(
                difflib.SequenceMatcher(
                    None, chip.strip().lower(), hit
                ).ratio(), 2
            ),
            "rationale": "fuzzy", "source": "fuzzy",
        }
    # else: leave unresolved → verbatim below.


def _validate_mapping(
    mapping: dict[str, dict], warnings: list[str], names: set[str],
) -> None:
    """Load-bearing deterministic validation (plan §1b): registry membership,
    duplicate-target demotion, charset discipline."""
    seen: dict[str, tuple[str, float]] = {}  # canonical → (chip, confidence)
    for chip, entry in mapping.items():
        if entry.get("source") == "verbatim":
            # Verbatim == "no mapping" (today's behavior): the record key IS
            # the chip, charset-valid or not. Registry discipline applies to
            # MAPPED keys only — demoting these would silently rewrite the
            # contracts of jobs where nothing needed mapping.
            continue
        target = entry.get("target")
        if not target or target == "CUSTOM":
            entry["target"] = "CUSTOM"
            entry["target_key"] = sanitize_custom_key(chip)
            continue
        if names and target not in names:
            entry["target"] = "CUSTOM"
            entry["target_key"] = sanitize_custom_key(chip)
            warnings.append(
                f"'{chip}' → '{target}' is not a registry field; kept as "
                "custom field"
            )
            continue
        prev = seen.get(target)
        if prev and prev[0] != chip:
            # Duplicate canonical target: higher confidence keeps it.
            if float(entry.get("confidence") or 0) > prev[1]:
                old = mapping[prev[0]]
                old["target"] = "CUSTOM"
                old["target_key"] = sanitize_custom_key(prev[0])
                old["demoted"] = True
                warnings.append(
                    f"'{prev[0]}' demoted to custom — '{chip}' also mapped "
                    f"to '{target}' with higher confidence"
                )
                seen[target] = (chip, float(entry.get("confidence") or 0))
            else:
                entry["target"] = "CUSTOM"
                entry["target_key"] = sanitize_custom_key(chip)
                entry["demoted"] = True
                warnings.append(
                    f"'{chip}' demoted to custom — '{prev[0]}' already "
                    f"mapped to '{target}'"
                )
        else:
            seen[target] = (chip, float(entry.get("confidence") or 0))


def rekey_field_notes(
    field_notes: dict | None, mapping: dict | None,
) -> dict:
    """[F4] W27-4 per-field instructions are keyed by RAW chip; every consumer
    matches them against the RESOLVED key. Re-key at resolve time or they
    silently vanish from prompts and the persisted Site.output_schema."""
    notes = field_notes if isinstance(field_notes, dict) else {}
    if not notes or not isinstance(mapping, dict) or not mapping:
        return dict(notes)
    out: dict = {}
    for chip, note in notes.items():
        entry = mapping.get(str(chip))
        key = entry.get("target") if isinstance(entry, dict) else None
        if key == "CUSTOM":
            key = entry.get("target_key") or sanitize_custom_key(str(chip))
        out[str(key) if key else str(chip)] = note
    return out
