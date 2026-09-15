"""Feed: roads (HANDOVER §3.2).

No machine-readable source exists, so ``data/roads.csv`` is maintained by hand::

    name,street,from_street,to_street,work_type,deadline,source_url,notes

Both ends of a segment (``street × from_street``, ``street × to_street``) are geocoded with the
Yandex Geocoder (results cached in ``data/geocode_cache.json``) and emitted as one ``LineString``.
Optional extra columns: ``status`` (planned | in_progress | done) and ``contractor``.
"""
from __future__ import annotations

import csv
import hashlib
import logging
from datetime import date
from pathlib import Path

from .common import DATA_DIR, STATUSES, Geocoder, Project, parse_deadline_text, today_utc

log = logging.getLogger(__name__)

CSV_PATH = DATA_DIR / "roads.csv"
COLUMNS = ["name", "street", "from_street", "to_street", "work_type", "deadline", "source_url", "notes"]
CITY = "Алматы"
# Tried in order until the geocoder returns a point inside Almaty. Adjust after the first real run.
QUERY_TEMPLATES = (
    "{city}, {a}, {b}",
    "{city}, пересечение {a} и {b}",
)


def read_rows(path: Path = CSV_PATH) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}; expected header {','.join(COLUMNS)}")
        rows = []
        for raw in reader:
            row = {k: (v or "").strip() for k, v in raw.items() if k}
            if not row.get("street") or row["street"].startswith("#"):
                continue
            rows.append(row)
    return rows


def road_id(row: dict[str, str]) -> str:
    key = "|".join(row.get(c, "").casefold() for c in ("street", "from_street", "to_street", "work_type"))
    return "road:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def intersection(geocoder: Geocoder, street: str, cross: str) -> tuple[float, float] | None:
    for template in QUERY_TEMPLATES:
        hit = geocoder.geocode(template.format(city=CITY, a=street, b=cross))
        if hit:
            return (hit["lon"], hit["lat"])
    return None


def row_to_project(row: dict[str, str], geocoder: Geocoder, today: date) -> Project | None:
    street, a, b = row["street"], row["from_street"], row["to_street"]
    label = f"{street} ({a} – {b})"
    if not row.get("source_url"):
        log.warning("roads: %s has no source_url – skipped (the schema requires one)", label)
        return None
    start = intersection(geocoder, street, a)
    end = intersection(geocoder, street, b)
    if not start or not end:
        log.warning("roads: could not geocode %s (start=%s, end=%s) – skipped", label, start, end)
        return None
    if start == end:
        log.warning("roads: both ends of %s resolved to the same point – skipped", label)
        return None
    status = row.get("status") or "in_progress"
    if status not in STATUSES:
        log.warning("roads: %s has unknown status %r → in_progress", label, status)
        status = "in_progress"
    return Project(
        id=road_id(row),
        category="road",
        name=row.get("name") or label,
        geometry={"type": "LineString", "coordinates": [list(start), list(end)]},
        source_url=row["source_url"],
        builder=row.get("contractor") or None,
        address=f"{street}, от {a} до {b}",
        status=status,
        deadline=parse_deadline_text(row.get("deadline")),
        stage_note=row.get("work_type") or None,
    )


def run(geocoder: Geocoder, *, path: Path = CSV_PATH, today: date | None = None) -> list[Project]:
    today = today or today_utc()
    path = Path(path)
    if not path.exists():
        log.warning("roads: %s not found – feed skipped", path)
        return []
    rows = read_rows(path)
    projects = [p for row in rows if (p := row_to_project(row, geocoder, today))]
    geocoder.save()
    log.info("roads: %d of %d rows mapped (%d geocoder requests)", len(projects), len(rows), geocoder.requests_made)
    return projects
