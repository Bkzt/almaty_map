"""Run every feed → merge → ``data/projects.geojson``.

    python -m scraper.build                 # full crawl (~730 Krisha detail pages ≈ 20–25 min at 1.5 s/request)
    python -m scraper.build --limit 5       # smoke test → data/projects.sample.geojson (never clobbers the real file)
    python -m scraper.build --only roads    # one feed; other categories are kept from the existing file
    python -m scraper.build --refresh       # ignore the raw-HTML cache (scraper/.cache/)

Set CONTACT_EMAIL=you@example.com so the User-Agent carries a contact address (HANDOVER §7);
without it the crawl still runs, with a warning.

A feed that crashes keeps its previous features from the existing file, so a transient failure never
wipes the map. The exit code is 1 in that case so the cron run shows red.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import goszakup, krisha, roads
from .common import (
    DATA_DIR,
    PROJECTS_PATH,
    Geocoder,
    PoliteSession,
    Project,
    ScrapeAborted,
    read_geojson,
    today_utc,
    write_geojson,
)

log = logging.getLogger("build")

FEEDS: dict[str, tuple[str, ...]] = {  # feed → categories it owns
    "krisha": ("apartments",),
    "roads": ("road",),
    "goszakup": ("repair", "other"),
}
SAMPLE_PATH = DATA_DIR / "projects.sample.geojson"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only the first N ЖК from Krisha (smoke test)")
    ap.add_argument("--only", action="append", choices=sorted(FEEDS), help="run only this feed (repeatable)")
    ap.add_argument("--refresh", action="store_true", help="ignore cached HTML, re-fetch everything")
    ap.add_argument("--out", type=Path, help=f"output file (default {PROJECTS_PATH.relative_to(DATA_DIR.parent)})")
    ap.add_argument("--today", type=date.fromisoformat, help="pretend today is YYYY-MM-DD (deadline rules)")
    ap.add_argument("--min-delay", type=float, default=1.5, help="seconds between requests (never below 1.5)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    selected = set(args.only or FEEDS)
    today = args.today or today_utc()
    out = args.out or (SAMPLE_PATH if args.limit is not None else PROJECTS_PATH)

    if "krisha" in selected and args.limit is None and not os.environ.get("CONTACT_EMAIL"):
        log.warning(
            "CONTACT_EMAIL is not set – crawling with a User-Agent that has no contact address "
            "(HANDOVER §7 asks for one). Set it with: CONTACT_EMAIL=you@example.com python -m scraper.build"
        )

    session = PoliteSession(min_delay=max(1.5, args.min_delay), refresh=args.refresh)
    geocoder = Geocoder(os.environ.get("YANDEX_GEOCODER_KEY"), session)
    existing = read_geojson(out) or {"features": []}
    old_by_feed = {
        feed: [f for f in existing["features"] if f.get("properties", {}).get("category") in cats]
        for feed, cats in FEEDS.items()
    }

    results: list[Project | dict] = []
    feeds_report: dict[str, dict] = {}
    failed: list[str] = []
    for feed in FEEDS:
        if feed not in selected:
            results.extend(old_by_feed[feed])
            feeds_report[feed] = {"status": "skipped", "count": len(old_by_feed[feed])}
            continue
        try:
            if feed == "krisha":
                projects = krisha.run(session, limit=args.limit, today=today)
            elif feed == "roads":
                projects = roads.run(geocoder, today=today)
            else:
                projects = goszakup.run(session, geocoder, today=today)
        except ScrapeAborted as exc:
            log.error("%s: aborted – %s", feed, exc)
            projects = None
        except Exception:  # noqa: BLE001 – one feed must not take the others down
            log.exception("%s: crashed", feed)
            projects = None
        if projects is None:
            failed.append(feed)
            results.extend(old_by_feed[feed])
            feeds_report[feed] = {"status": "failed", "count": len(old_by_feed[feed])}
            log.warning("%s: keeping %d features from the previous file", feed, len(old_by_feed[feed]))
            continue
        results.extend(projects)
        feeds_report[feed] = {"status": "ok", "count": len(projects)}

    collection = write_geojson(
        results, out, generated_at=datetime.now(timezone.utc), extra={"feeds": feeds_report, "today": today.isoformat()}
    )
    log.info("wrote %s: %s", out, collection["counts"])
    for feed, rep in feeds_report.items():
        log.info("  %-9s %-8s %5d", feed, rep["status"], rep["count"])
    if args.limit is not None and out == SAMPLE_PATH:
        log.info("smoke test written to %s (pass --out data/projects.geojson to overwrite the real file)", out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
