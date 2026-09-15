"""Feed: repairs / other state construction from the goszakup.gov.kz OWS v3 API (HANDOVER §3.3). **STUB.**

Disabled until the ``GOSZAKUP_TOKEN`` env var exists – the token is not self-service, it needs a letter
to the Ministry of Finance (template in HANDOVER §3.3). Everything below follows the v3 docs
(https://old.goszakup.gov.kz/ru/developer/ows_v3, https://ows.goszakup.gov.kz/help/) but has never run
against the live API. When the token arrives::

    GOSZAKUP_TOKEN=... python -m scraper.build --only goszakup -v

then fix field names marked TODO(token), fill CUSTOMER_BINS, and remove the "stub" note in README.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Iterator

from .common import Geocoder, PoliteSession, Project, parse_deadline_text, today_utc

log = logging.getLogger(__name__)

BASE_URL = "https://ows.goszakup.gov.kz"
TRD_BUY_PATH = "/v3/trd-buy"  # announcements (объявления о закупке)
LOTS_PATH = "/v3/lots"  # lots (лоты) – TODO(token): docs also mention /v3/search/lots
CONTRACT_PATH = "/v3/contract"  # contracts (договоры) – the completion date lives here
GRAPHQL_PATH = "/v3/graphql"  # alternative: one query for announcement + lots + contract
PAGE_SIZE = 50  # fixed by the API; cursor pagination via ?page=next&search_after=<last id>
ANNOUNCE_URL = "https://goszakup.gov.kz/ru/announce/index/{id}"

# BIN → department. Almaty akimat customers whose lots belong on the map. Collect from the portal
# (e.g. Управление градостроительного контроля г. Алматы is participant id 346218 – look up its BIN).
CUSTOMER_BINS: dict[str, str] = {
    # "000000000000": "Управление городской мобильности г. Алматы",
    # "000000000000": "Управление градостроительного контроля г. Алматы",
    # "000000000000": "Управление образования г. Алматы",
}

KEEP_RE = re.compile(r"строительств|\bСМР\b|реконструкц|капитальн\w*\s+ремонт|благоустройств", re.I)
ROAD_RE = re.compile(r"дорог|улиц\w*|проезд|тротуар|асфальт|перекрёст|перекрест", re.I)


def enabled() -> bool:
    return bool(os.environ.get("GOSZAKUP_TOKEN"))


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def iter_pages(session: PoliteSession, token: str, path: str, params: dict) -> Iterator[dict]:
    """Walk a v3 list endpoint with cursor pagination (50 items per page)."""
    search_after = None
    while True:
        query = {"limit": PAGE_SIZE, **params}
        if search_after is not None:
            query.update({"page": "next", "search_after": search_after})
        data = session.get_json(f"{BASE_URL}{path}", params=query, headers=_headers(token), cache=False)
        items = data.get("items") if isinstance(data, dict) else data  # TODO(token): confirm envelope shape
        if not items:
            return
        yield from items
        search_after = items[-1].get("id")
        if search_after is None or len(items) < PAGE_SIZE:
            return


def _category(text: str) -> str:
    return "road" if ROAD_RE.search(text) else "repair"


def lot_to_project(anno: dict, lot: dict, contract: dict | None, geocoder: Geocoder, today: date) -> Project | None:
    text = " ".join(str(lot.get(k) or "") for k in ("name_ru", "description_ru"))
    if not KEEP_RE.search(text):
        return None
    place = lot.get("full_delivery_place_name_ru") or lot.get("delivery_place_ru")  # TODO(token): field name
    if not place:
        log.info("goszakup: lot %s has no delivery place – skipped", lot.get("id"))
        return None
    hit = geocoder.geocode(f"Алматы, {place}")
    if not hit:
        return None
    deadline = None
    if contract:  # TODO(token): confirm the completion-date field (ec_end_date / end_date / plan_exec_date)
        deadline = parse_deadline_text(contract.get("ec_end_date") or contract.get("end_date") or "")
    status = "in_progress" if contract else "planned"
    if deadline and deadline < today and contract and str(contract.get("ref_contract_status_id")) in {"390"}:
        status = "done"  # TODO(token): map contract status codes (refs/ref_contract_status) → done
    return Project(
        id=f"gz:{lot.get('id')}",
        category=_category(text),
        name=str(lot.get("name_ru") or anno.get("name_ru") or "").strip()[:160],
        geometry={"type": "Point", "coordinates": [hit["lon"], hit["lat"]]},
        source_url=ANNOUNCE_URL.format(id=anno.get("id")),
        builder=(contract or {}).get("supplier_name_ru") or anno.get("customer_name_ru"),
        address=place,
        status=status,
        deadline=deadline,
        stage_note=f"лот {lot.get('lot_number') or lot.get('id')} · {anno.get('number_anno') or ''}".strip(" ·"),
    )


def run(session: PoliteSession, geocoder: Geocoder, *, today: date | None = None, since: date | None = None) -> list[Project]:
    today = today or today_utc()
    if not enabled():
        log.info("goszakup: GOSZAKUP_TOKEN not set – feed disabled (HANDOVER §3.3), map ships without it")
        return []
    if not CUSTOMER_BINS:
        log.warning("goszakup: CUSTOMER_BINS is empty – fill it in scraper/goszakup.py before enabling")
        return []
    token = os.environ["GOSZAKUP_TOKEN"]
    since = since or date(today.year - 2, 1, 1)
    projects: list[Project] = []
    for bin_, department in CUSTOMER_BINS.items():
        log.info("goszakup: %s (%s)", department, bin_)
        # TODO(token): confirm filter names; docs list customer_bin and publish_date as searchable fields
        for anno in iter_pages(session, token, TRD_BUY_PATH, {"customer_bin": bin_}):
            published = parse_deadline_text(str(anno.get("publish_date") or ""))
            if published and published < since:
                continue
            lots = anno.get("Lots") or anno.get("lots")
            if lots is None:
                lots = list(iter_pages(session, token, LOTS_PATH, {"trd_buy_id": anno.get("id")}))
            contracts = list(iter_pages(session, token, CONTRACT_PATH, {"trd_buy_id": anno.get("id")}))
            contract = contracts[0] if contracts else None
            for lot in lots:
                project = lot_to_project(anno, lot, contract, geocoder, today)
                if project:
                    projects.append(project)
    geocoder.save()
    log.info("goszakup: %d projects", len(projects))
    return projects
