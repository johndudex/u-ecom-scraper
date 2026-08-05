"""User-supplied schema validation for the /intake "JSON schema" input.

The scraper pipeline is a flat field-name engine: it only honors a list of
top-level field names (see ``src/content_types.schema_field_names``). This
module validates whatever the user pastes/uploads, accepts several common
dialects (the internal ``{fields:[...]}`` shape, standard JSON Schema, a bare
array of names, or a flat ``{field: type}`` map), normalizes them to a flat
field-name list, and reports issues with user-friendly messages.

Design:
- Pure Python, no Django imports → unit-testable in isolation.
- Never raises for bad input — every failure becomes a :class:`SchemaIssue`.
- Two layers: structural (parse/size/shape) then semantic (names/types/depth).
- ``jsonschema`` is used ONLY to meta-validate standard JSON-Schema inputs
  (``Draft202012Validator.check_schema``); it degrades gracefully if the
  dependency is absent. Everything else is hand-rolled here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── limits (single source of truth; import from views + tests) ───────────────
MAX_SCHEMA_BYTES = 262_144          # 256 KiB — schemas are small; fail fast
MAX_PROPERTIES = 100
MAX_PROPERTY_NAME_LEN = 64
MAX_NESTING_DEPTH = 5

# Internal field-type vocabulary (mirrors src/content_types.py FieldDef.field_type)
INTERNAL_FIELD_TYPES: tuple[str, ...] = ("text", "number", "datetime", "list", "url")

# JSON Schema primitive type → internal field type. Aggregates/objects collapse
# because the internal model cannot represent them (the "variants" convention:
# nested data survives as an opaque blob under a top-level name).
_JS_TYPE_TO_INTERNAL: dict[str, str] = {
    "string": "text",
    "integer": "number",
    "number": "number",
    "boolean": "text",
    "array": "list",
    "object": "text",
}

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Lightweight content-type signatures for advisory detection. Ordered so the
# best match wins; the user's explicit page-type choice always overrides this.
_CONTENT_TYPE_SIGNATURES: tuple[tuple[str, frozenset[str]], ...] = (
    ("product", frozenset({"title", "price", "availability", "sku", "brand"})),
    ("article", frozenset({"title", "author", "content", "publish_date", "published"})),
    ("job_posting", frozenset({"title", "company", "location", "description", "salary"})),
    ("forum_thread", frozenset({"title", "author", "posts", "replies"})),
    ("serp", frozenset({"rank", "url", "title", "snippet"})),
)


# ── result types ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SchemaIssue:
    code: str                       # canonical key from the catalog below
    message: str                    # fully-rendered, user-facing string
    severity: str = "error"         # "error" (blocks) | "warning" (proceed)
    path: str = ""                  # JSON pointer, e.g. "/properties/foo"


@dataclass(frozen=True)
class SchemaValidationResult:
    valid: bool                     # True iff no error-severity issues
    issues: list[SchemaIssue] = field(default_factory=list)
    shape: str = "unknown"          # "standard" | "internal" | "flat_map" | "array" | "unknown"
    normalized: dict[str, Any] | None = None   # {"content_type", "fields":[{"name"}]} on success
    derived_fields: list[str] = field(default_factory=list)
    detected_content_type: str | None = None

    @property
    def errors(self) -> list[SchemaIssue]:
        return [i for i in self.issues if i.severity == "error"]


# ── optional jsonschema meta-validation (graceful fallback) ──────────────────
try:  # pragma: no cover - import guard
    from jsonschema import Draft202012Validator  # type: ignore

    _HAS_JSONSCHEMA = True
except Exception:  # pragma: no cover  # noqa: BLE001 - optional dep; degrade gracefully
    _HAS_JSONSCHEMA = False


# ── public API ───────────────────────────────────────────────────────────────
def validate_user_schema(raw: str | bytes | dict[str, Any]) -> SchemaValidationResult:
    """Validate a user-pasted/uploaded schema. Never raises.

    Accepts JSON text (str/bytes) or an already-parsed dict. Returns the
    normalized internal shape on success; ``normalized`` is None on failure.
    """
    issues: list[SchemaIssue] = []

    # Already-parsed dict → skip parse/size guards (still bound below).
    if isinstance(raw, dict):
        doc: Any = raw
    else:
        text = _to_text(raw)
        empty = _parse_and_size(text, issues)
        if empty or issues:
            return SchemaValidationResult(valid=False, issues=issues)
        doc = _parse_json(text, issues)
        if doc is None or issues:
            return SchemaValidationResult(valid=False, issues=issues)

    # Top-level must be an object (dict) or, leniently, an array of names.
    if isinstance(doc, list):
        names = _names_from_array(doc, issues)
        return _finalize("array", names, issues)
    if not isinstance(doc, dict):
        issues.append(SchemaIssue("NOT_OBJECT", f"Schema must be a JSON object — got {_typename(doc)}."))
        return SchemaValidationResult(valid=False, issues=issues)

    shape = _detect_shape(doc)
    if shape == "standard":
        names = _validate_standard(doc, issues)
    elif shape == "internal":
        names = _validate_internal(doc, issues)
    else:  # flat_map
        names = _validate_flat_map(doc, issues)

    return _finalize(shape, names, issues)


# ── structural helpers ───────────────────────────────────────────────────────
def _to_text(raw: str | bytes | dict[str, Any]) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _parse_and_size(text: str, issues: list[SchemaIssue]) -> bool:
    """Returns True if the input is empty (caller should stop). Appends issues."""
    if not text or not text.strip():
        issues.append(SchemaIssue("EMPTY", "Schema is empty — paste a JSON schema or upload a .json file."))
        return True
    if len(text) > MAX_SCHEMA_BYTES:
        issues.append(SchemaIssue(
            "TOO_LARGE",
            f"Schema is too large ({len(text):,} bytes). The limit is {MAX_SCHEMA_BYTES // 1024} KiB — "
            "remove nested definitions or unused properties.",
        ))
        return True
    return False


def _parse_json(text: str, issues: list[SchemaIssue]) -> Any:
    try:
        return json.loads(text)
    except RecursionError:
        issues.append(SchemaIssue(
            "TOO_DEEP",
            "Schema is too deeply nested (overflowed the parser stack). Flatten nested objects to "
            f"≤ {MAX_NESTING_DEPTH} levels.",
        ))
        return None
    except json.JSONDecodeError as e:
        issues.append(SchemaIssue(
            "INVALID_JSON",
            f"Invalid JSON: {e.msg} at line {e.lineno}, column {e.colno}.",
        ))
        return None


def _typename(v: Any) -> str:
    return {
        dict: "object", list: "array", str: "string", int: "number",
        float: "number", bool: "boolean", type(None): "null",
    }.get(type(v), type(v).__name__)


def _detect_shape(doc: dict[str, Any]) -> str:
    if "$schema" in doc or (doc.get("type") == "object" and isinstance(doc.get("properties"), dict)):
        return "standard"
    if isinstance(doc.get("fields"), list):
        return "internal"
    return "flat_map"


# ── per-dialect validators → return list[str] of derived names ──────────────
def _validate_standard(doc: dict[str, Any], issues: list[SchemaIssue]) -> list[str]:
    # Meta-validate the JSON Schema document itself (best-effort).
    if _HAS_JSONSCHEMA:
        try:
            Draft202012Validator.check_schema(doc)
        except Exception as e:  # jsonschema.exceptions.SchemaError  # noqa: BLE001 - surface any meta-validation failure
            ptr = "/" + "/".join(str(p) for p in getattr(e, "absolute_path", []) or getattr(e, "path", []) or [])
            issues.append(SchemaIssue(
                "BAD_META",
                f"This is not a valid JSON Schema: {getattr(e, 'message', str(e))} (at {ptr or '/'}).",
                path=ptr,
            ))
    else:  # graceful fallback: light structural checks
        if "properties" in doc and not isinstance(doc["properties"], dict):
            issues.append(SchemaIssue("BAD_META", "'properties' must be an object."))
        if "required" in doc and not (isinstance(doc["required"], list) and all(isinstance(x, str) for x in doc["required"])):
            issues.append(SchemaIssue("BAD_META", "'required' must be an array of strings."))

    props = doc.get("properties")
    if not isinstance(props, dict) or not props:
        issues.append(SchemaIssue("MUST_BE_OBJECT", "Schema must define an object with 'properties'."))
        return []

    if len(props) > MAX_PROPERTIES:
        issues.append(SchemaIssue("TOO_MANY_PROPS", f"Schema defines {len(props)} properties; the limit is {MAX_PROPERTIES}."))
        return []

    # Reject remote $ref (SSRF guard) and check nesting depth.
    _reject_remote_refs(doc, issues)
    if _object_depth(doc) > MAX_NESTING_DEPTH:
        issues.append(SchemaIssue("NESTED_TOO_DEEP", f"Schema nests objects deeper than {MAX_NESTING_DEPTH} levels — flatten it."))

    names: list[str] = []
    for key in props:
        _check_name(key, issues, path=f"/properties/{key}")
        names.append(key)
    return names


def _validate_internal(doc: dict[str, Any], issues: list[SchemaIssue]) -> list[str]:
    fields = doc.get("fields")
    if not isinstance(fields, list) or not fields:
        issues.append(SchemaIssue("INTERNAL_NO_FIELDS", "Schema is missing a 'fields' list (or it is empty)."))
        return []
    if len(fields) > MAX_PROPERTIES:
        issues.append(SchemaIssue("TOO_MANY_PROPS", f"Schema defines {len(fields)} fields; the limit is {MAX_PROPERTIES}."))
        return []

    names: list[str] = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            issues.append(SchemaIssue("INTERNAL_BAD_FIELD", f"Field at position {i} is not an object.", path=f"/fields/{i}"))
            continue
        name = f.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(SchemaIssue("INTERNAL_BAD_FIELD", f"Field at position {i} is missing a 'name'.", path=f"/fields/{i}"))
            continue
        _check_name(name, issues, path=f"/fields/{i}")
        t = f.get("type")
        if t is not None and t != "" and t not in INTERNAL_FIELD_TYPES:
            issues.append(SchemaIssue(
                "UNSUPPORTED_TYPE",
                f"Field '{name}': unsupported type '{t}'. Supported: {', '.join(INTERNAL_FIELD_TYPES)}.",
                path=f"/fields/{i}",
            ))
        names.append(name)
    return names


def _validate_flat_map(doc: dict[str, Any], issues: list[SchemaIssue]) -> list[str]:
    if not doc:
        issues.append(SchemaIssue("NO_FIELDS", "Schema must define at least one field."))
        return []
    if len(doc) > MAX_PROPERTIES:
        issues.append(SchemaIssue("TOO_MANY_PROPS", f"Schema defines {len(doc)} properties; the limit is {MAX_PROPERTIES}."))
        return []
    names: list[str] = []
    for key in doc:
        _check_name(key, issues, path=f"/{key}")
        names.append(key)
    return names


def _names_from_array(doc: list[Any], issues: list[SchemaIssue]) -> list[str]:
    if not doc:
        issues.append(SchemaIssue("NO_FIELDS", "Schema must define at least one field."))
        return []
    if len(doc) > MAX_PROPERTIES:
        issues.append(SchemaIssue("TOO_MANY_PROPS", f"Schema defines {len(doc)} entries; the limit is {MAX_PROPERTIES}."))
        return []
    names: list[str] = []
    for i, item in enumerate(doc):
        if isinstance(item, str):
            _check_name(item, issues, path=f"/{i}")
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            _check_name(item["name"], issues, path=f"/{i}")
            names.append(item["name"])
        else:
            issues.append(SchemaIssue("BAD_PROP_NAME", f"Entry at position {i} is not a field name.", path=f"/{i}"))
    return names


# ── shared semantic checks ───────────────────────────────────────────────────
def _check_name(name: str, issues: list[SchemaIssue], path: str = "") -> None:
    if not isinstance(name, str) or not name.strip():
        issues.append(SchemaIssue("BAD_PROP_NAME", "A field name is empty or missing.", path=path))
        return
    if len(name) > MAX_PROPERTY_NAME_LEN:
        issues.append(SchemaIssue("BAD_PROP_NAME", f"Field '{name}' is too long ({len(name)} > {MAX_PROPERTY_NAME_LEN} chars).", path=path))
    if not _NAME_RE.match(name):
        issues.append(SchemaIssue(
            "WARN_PROP_CHARS",
            f"Field '{name}' has spaces or special characters — underscores are recommended.",
            severity="warning", path=path,
        ))


def _reject_remote_refs(node: Any, issues: list[SchemaIssue], path: str = "") -> None:
    """Walk a node and reject any $ref that is not a local #/$defs/... pointer."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#/$defs/"):
            issues.append(SchemaIssue(
                "REMOTE_REF",
                f"Uses a remote $ref '{ref}'. Only local #/$defs/... references are allowed.",
                path=path,
            ))
        for k, v in node.items():
            _reject_remote_refs(v, issues, path=f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _reject_remote_refs(v, issues, path=f"{path}/{i}")


def _object_depth(node: Any) -> int:
    """Max nesting depth of object-typed schemas with their own properties."""
    if not isinstance(node, dict):
        return 0
    if node.get("type") == "object" and isinstance(node.get("properties"), dict):
        children = node.get("properties", {}).values()
        return 1 + max((_object_depth(c) for c in children), default=0)
    # account for array-of-objects too
    if node.get("type") == "array" and isinstance(node.get("items"), dict):
        return 1 + _object_depth(node["items"])
    return 0


# ── finalize: dedupe, detect content type, build normalized shape ────────────
def _finalize(shape: str, names: list[str], issues: list[SchemaIssue]) -> SchemaValidationResult:
    # Uniqueness (case-sensitive) — duplicates are an error.
    seen: set[str] = set()
    deduped: list[str] = []
    for n in names:
        if n in seen:
            issues.append(SchemaIssue("DUP_PROP", f"Duplicate field '{n}'."))
            continue
        seen.add(n)
        deduped.append(n)

    if not deduped and not [i for i in issues if i.severity == "error"]:
        issues.append(SchemaIssue("NO_FIELDS", "Schema must define at least one field."))

    has_errors = any(i.severity == "error" for i in issues)
    detected = _detect_content_type(deduped) if deduped else None

    normalized = None
    if not has_errors:
        normalized = {
            "content_type": detected,
            "fields": [{"name": n} for n in deduped],
        }

    return SchemaValidationResult(
        valid=not has_errors,
        issues=issues,
        shape=shape,
        normalized=normalized,
        derived_fields=deduped,
        detected_content_type=detected,
    )


def _detect_content_type(names: list[str]) -> str | None:
    if not names:
        return None
    field_set = {n.lower() for n in names}
    best, best_hits = None, 0
    for ct, sig in _CONTENT_TYPE_SIGNATURES:
        hits = len(field_set & {f.lower() for f in sig})
        if hits > best_hits:
            best, best_hits = ct, hits
    return best if best_hits >= 2 else None
