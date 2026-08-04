"""SEC submissions retrieval including archived filing history."""

from __future__ import annotations

import logging
from typing import Any

from equitytrace.sec.client import SecClient

logger = logging.getLogger(__name__)


def fetch_all_submissions(client: SecClient, cik: str) -> dict[str, Any]:
    """
    Fetch the main submissions document and merge archived filing history.

    The SEC splits older filings into separate files listed under ``files``.
    This function retrieves those archives so older filings are not excluded.
    """
    primary = client.get_submissions(cik)
    archives = list_archive_filenames(primary)
    archived_payloads: list[dict[str, Any]] = []
    for filename in archives:
        logger.info("Fetching archived submissions file %s for CIK %s", filename, cik)
        archived_payloads.append(client.get_archived_submissions(filename))
    return merge_submissions(primary, archived_payloads)


def list_archive_filenames(submissions: dict[str, Any]) -> list[str]:
    """Return archived submission filenames referenced by the main response."""
    filings = submissions.get("filings")
    candidates: list[Any] = []
    if isinstance(filings, dict):
        nested = filings.get("files")
        if isinstance(nested, list):
            candidates.extend(nested)
    top_level = submissions.get("files")
    if isinstance(top_level, list):
        candidates.extend(top_level)

    filenames: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip() and name.strip() not in seen:
            cleaned = name.strip()
            seen.add(cleaned)
            filenames.append(cleaned)
    return filenames


def merge_submissions(
    primary: dict[str, Any],
    archives: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge recent and archived filing arrays into one submissions document."""
    merged = dict(primary)
    recent = primary.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        recent = {}

    combined = {
        key: list(value) if isinstance(value, list) else [] for key, value in recent.items()
    }
    for archive in archives:
        for key, values in archive.items():
            if not isinstance(values, list):
                continue
            combined.setdefault(key, [])
            combined[key].extend(values)

    filings = dict(merged.get("filings") or {})
    filings["recent"] = combined
    merged["filings"] = filings
    return merged
