# JSON Schema Upload on `/intake` — Design

> Status: **Designed; not yet implemented.** Origin: 6-agent design investigation.
> Related: [`code-writer-summarization-plan.md`](./code-writer-summarization-plan.md), [`page-types-roadmap.md`](./page-types-roadmap.md).

## TL;DR — the one decision

Let users upload/paste a JSON schema on `/intake` as an alternative to typing fields. **Validate it → flatten to top-level field names → populate the existing field chips → leave the pipeline 100% unchanged.** No model changes, no migrations. Add one AJAX endpoint + one pure validator module + the UI. Use `jsonschema` for meta-validation of standard JSON-Schema inputs, hand-roll everything else.

## Why flatten (don't thread a rich schema through)

The pipeline is a **flat field-name engine end-to-end**. 6 agents independently confirmed there is no reader for types/nesting/required anywhere:

- `tasks.py:514-519` overwrites every user field to `{name, label=name, type:"text", required:True}` — the type-stripping point.
- `normalize_fields._prune_to_schema` (`normalize_fields.py:115-129`), `validate_coverage` (`validate_coverage.py:118-120`), `_prune_output_to_schema` (`tasks.py:571-615`), and the code_writer message (`subagents.py:1191-1226`, `2048+`) all treat fields as a **set of top-level names** with no recursion.
- `type` is **never read** from `output_schema["fields"]`; `required` is read **once** (`subagents.py:141`) for advisory prompt text only.
- `Site.output_schema` is persisted even thinner — `[{"name": f}]` only (`tasks.py:903-907`).

**A rich/typed/nested schema uploaded *without* pipeline changes would break coverage validation**: it would inflate the `core` set with dotted/nested names the analyzer never produces → coverage drops to ~0 → job interrupts at the low-coverage gate.

`variants`/`category_tree` only work today *by convention* (LLM emits a top-level key; `variants` is on a hardcoded pass-through allowlist at `subagents.py:221-228`; user adds the flat name as a chip). Threading a real schema through would be 8–10 coordinated changes including a research-level redefinition of "nested coverage" — **explicitly out of scope**.

## Architecture

### 1. UI — `webapp/scraper/templates/scraper/intake.html`
A `.seg` mode toggle (**Fields / JSON schema**) inside the existing `#fields-block` (alongside the chip input), a paste `<textarea>` + file-upload (reusing the paperclip icon at `intake.html:436`), and inline errors via the existing `.url-error` styling (`:182`). On a valid schema, extract field names client-side and `addChip()` each → `fieldsArr` (the single source of truth at `:836`) stays the contract, so run-sample gate, POST, config panel, and save are unchanged. File upload reuses the `wireFileAttach()` pattern (`:1131-1147`) via `FileReader.readAsText`.

### 2. Endpoint — one new AJAX view
`POST /intake/validate-schema/` (`@login_required`, POST+AJAX guard matching `intake_create_job` at `views.py:2261`, CSRF via existing `X-CSRFToken` header). Accepts both `schema_text` (POST) and `schema_file` (FILES). Returns, always 200 for content errors (matches `intake_check_site`'s 200-with-`known_site:false` convention):
```json
{ "valid": true, "issues": [{"code","message","severity","path"}],
  "derived_fields": ["title","price"], "detected_content_type": "product" }
```
Route: `webapp/scraper/urls.py` → `path("intake/validate-schema/", views.intake_validate_schema, name="intake_validate_schema")` (after the `check-site` line).

### 3. Validator — new pure module `src/schema_validation.py`
`validate_user_schema(raw) → SchemaValidationResult(valid, issues, shape, normalized)`. Never raises — every failure is a `SchemaIssue(code, message, severity, path)`. No Django imports (unit-testable). Placed beside `src/content_types.py` (the consumer). The intake view and a defensive gate in `intake_create_job` both call it.

### 4. Dialect handling — accept multiple, normalize
Auto-detect and reduce all to a flat field-name list:
- **Internal** `{content_type, fields:[{name,label,type,required}]}` — matches `Site.output_schema`; what `intake_check_site` already parses (`views.py:2211-2217`).
- **Standard JSON Schema** `{type:"object", properties:{...}, required:[...]}` → meta-validate, then map `properties` keys → field names (types/required logged for the user, not stored).
- **Bare array** `["title","price"]` and **flat map** `{field: type}` — lenient shortcuts.

Top-level names only; nested objects/arrays collapse to their container name (the `variants` convention). `detected_content_type` is **advisory** — the user's explicit page-type choice always wins at job creation.

### 5. `jsonschema` hybrid (the "do both" decision)
- Add `jsonschema>=4.20,<5.0` to `webapp/requirements.txt` (transitive: `attrs`, `rpds-py`, `referencing`, `jsonschema-specifications` — all wheels, no C toolchain in slim image).
- Use `Draft202012Validator.check_schema(doc)` **only on the standard-schema branch** for meta-validation (catches malformed JSON Schemas with good messages).
- **Hand-roll** everything else: dialect detection, internal-shape validation, all semantic checks, all security. `jsonschema` cannot do these.
- Graceful fallback: if the import fails, degrade the standard branch to a light hand-rolled structural check (don't hard-fail).

## Validation rules (condensed)

**Structural (A):** non-empty; size ≤ 256 KiB; `json.loads` (surfaces `lineno`/`colno`); catch `RecursionError` (deep-nesting DoS, CPython limit 1000); top-level must be a JSON object; shape detection; if standard → `check_schema`; if internal → must have a `fields` list of dicts.

**Semantic (B):** ≥1 field; names non-empty, ≤64 chars, unique; supported types; nesting depth ≤5; remote `$ref` rejected. Severity `error` (blocks) vs `warning` (proceed, e.g. "name has spaces — underscores recommended").

A canonical error-string catalog lives in the validator (e.g. `INVALID_JSON`, `TOO_DEEP`, `DUP_PROP`, `UNSUPPORTED_TYPE`, `REMOTE_REF`, `NO_FIELDS`).

## Security
256 KiB in-helper cap (Django's 2.5 MB default backs this); `RecursionError` catch for billion-laughs-style nesting; reject remote `$ref`/`$id` (no SSRF, no `RefResolver` fetch); never `eval`/`exec`/`pickle`/`yaml.load`; store the **normalized** shape, never raw untrusted input, so it doesn't flow verbatim into agent prompts; `@login_required` + existing CSRF/AJAX guards; log failures at WARNING with codes only (never the raw body).

## Storage decision (MVP)
Derive flat field names → store on the existing `ScrapeJob.target_fields` (`models.py:164`) → POST as the existing comma-joined `target_fields` string. **No new columns, no migration.** The normalized `output_schema` dict is returned to the UI for preview but **not** persisted as rich (the pipeline would discard types anyway, and `tasks.py:903-907` writes only names).

## Tests
New `TestIntakeValidateSchemaView` in `webapp/tests/test_views.py` (the first real intake AJAX tests) + `tests/test_schema_validation.py` for the helper. Pattern: `SimpleUploadedFile` + the critical `HTTP_X_REQUESTED_WITH="XMLHttpRequest"` header + `override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=...)` for oversize. Cases: valid→derived fields; invalid JSON→error+line/col; non-object→error; missing `name`→error; GET/non-AJAX→400; oversize→rejected; standard-JSON-Schema→converted; `RecursionError` path.

## Files to touch
- `webapp/scraper/urls.py` — +1 route
- `webapp/scraper/views.py` — +`intake_validate_schema` view, +defensive gate in `intake_create_job`
- `src/schema_validation.py` — new validator module
- `webapp/requirements.txt` — +`jsonschema>=4.20,<5.0`
- `webapp/scraper/templates/scraper/intake.html` — UI + JS
- `webapp/tests/test_views.py` (+ `tests/test_schema_validation.py`) — tests

**Prior art to copy:** `SiteForm.clean_input_urls_file` (`forms.py:97-107`) — already a JSON-file-upload + manual-validate pattern.

## Out of scope (future)
- Honoring types/nesting/required end-to-end (needs the 8–10 pipeline changes + a nested-coverage definition).
- Advisory schema echo to the LLM (pass original schema string into `_user_requirements_section` so code_writer sees intended structure — the existing `variants` pattern; cheap, additive, can be a fast-follow).
- Round-tripping a rich schema through `Site.output_schema` on re-run.
