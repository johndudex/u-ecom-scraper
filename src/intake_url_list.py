"""[wave-40 T5] Intake-side repair + validation for url_list jobs.

Prod 09-20/21: 21 jobs submitted as bare PDP URLs were coerced to url_list
(W37-NEW-C), created, queued, then died in ~4s in setup_workspace because
neither the textarea nor scrapers/{slug}/input_urls.json held any URLs.
Repair: seed the coerced PDP URL (that flow works — prod 758). Gate: reject
with 422 only when genuinely nothing exists. Stdlib-only; importable from
src/ and webapp/.
"""


def _has_url_text(list_urls: str) -> bool:
    for line in str(list_urls or "").splitlines():
        if line.strip():
            return True
    return False


def coerced_seed_url(url: str, input_mode: str, list_urls: str) -> str:
    """The URL to seed a coerced url_list job with, or "".

    Repair-on-coercion (wave-40 T5): when intake's W37-NEW-C coercion turned
    a PDP submission into url_list, that URL IS the item list. The caller
    must apply the same predicate the coercion used — this helper only
    checks mode/emptiness/shape.
    """
    if (input_mode or "").lower() != "url_list":
        return ""
    if _has_url_text(list_urls):
        return ""
    u = str(url or "").strip()
    return u if u.startswith(("http://", "https://")) else ""


def missing_url_list_reason(
    input_mode: str,
    list_urls: str,
    site_has_urls: bool,
    fm_file_exists: bool | None,
    seed_url: str = "",
) -> str:
    """'' when the intake payload is acceptable, else a human reason.

    fm_file_exists: True/False from the File-Master check, None when the
    check could not run (transient FM error) — None fails OPEN so a brief
    FM outage cannot lock out legitimate re-scrapes.
    """
    if (input_mode or "").lower() != "url_list":
        return ""
    if _has_url_text(list_urls) or site_has_urls or seed_url:
        return ""
    if fm_file_exists is None or fm_file_exists:
        return ""
    return (
        "input_mode url_list requires at least one product URL: paste URLs "
        "in the list box (one per line), or pick a mode that discovers URLs."
    )
