"""Feed: apartments (ЖК) from the Krisha.kz catalogue (HANDOVER §3.1).

List pages   https://krisha.kz/complex/search/almaty/?state[]=1&state[]=2&page=N
             12 cards per page, each is ``div.complex-card[data-complex-alias]``.
Detail pages https://krisha.kz/complex/show/almaty/<slug>/ – facts live in three places:
             * ``window.data.complex`` JSON blob: name, address, stateText, map.lat/lon, constructionProgress;
             * sidebar ``.complex__sidebar-info`` blocks: Срок сдачи (per очередь), Застройщик, Статус;
             * ``.complex-docs__list-item`` (КЖК guarantee) and ``dl.complex-parameters__block`` (parameters).
Selectors were pinned against fixtures fetched 2026-09-14 (scraper/tests/fixtures/).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator

from bs4 import BeautifulSoup, Tag

from .common import (
    FetchError,
    NotFound,
    PoliteSession,
    Project,
    in_almaty,
    parse_deadline_text,
    parse_russian_date,
    quarter_label,
    quarter_of,
    quarter_start,
    today_utc,
)

log = logging.getLogger(__name__)

BASE_URL = "https://krisha.kz"
LIST_URL = f"{BASE_URL}/complex/search/almaty/"
LIST_PARAMS = {"state[]": ["1", "2"]}  # 1 = Строящийся, 2 = Сдан в эксплуатацию – the catalogue's own default
CARDS_PER_PAGE = 12

STATUS_MAP = {
    "строящийся": "in_progress",
    "строительство приостановлено": "in_progress",
    "сдан в эксплуатацию": "done",
    "сдан": "done",
    "проектируется": "planned",
    "планируется": "planned",
}

_ORDINAL_WORDS = [
    ("одиннадцат", 11), ("двенадцат", 12), ("перв", 1), ("втор", 2), ("трет", 3), ("четверт", 4),
    ("четвёрт", 4), ("пят", 5), ("шест", 6), ("седьм", 7), ("восьм", 8), ("девят", 9), ("десят", 10),
]
_SEPARATOR = re.compile(r"\s*[-–—:]\s+|\s+[-–—:]\s*")
_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")
_WINDOW_DATA = re.compile(r"window\.data\s*=\s*")
_ALIAS_IN_URL = re.compile(r"/complex/show/([^/?#]+/[^/?#]+)")


def _clean(value: object) -> str | None:
    """Collapse whitespace and drop zero-width characters (Krisha has "ул. \u200bРозыбакиева")."""
    if value is None:
        return None
    t = " ".join(_ZERO_WIDTH.sub("", str(value)).split())
    return t or None


def _text(el: Tag | None) -> str | None:
    return _clean(el.get_text(" ", strip=True)) if el is not None else None


# --------------------------------------------------------------------------- list
@dataclass(frozen=True)
class ListItem:
    slug: str
    alias: str  # "almaty/kaspii"
    name: str
    status_text: str | None
    address: str | None
    complex_id: str | None = None

    @property
    def url(self) -> str:
        return f"{BASE_URL}/complex/show/{self.alias}/"


def list_params(page: int) -> dict:
    return {**LIST_PARAMS, "page": page}


def parse_list(html: str) -> list[ListItem]:
    soup = BeautifulSoup(html, "lxml")
    items: list[ListItem] = []
    for card in soup.select("div.complex-card[data-complex-alias]"):
        alias = (card.get("data-complex-alias") or "").strip("/")
        if not alias:
            continue
        # the direct child .complex-card__state is the status; a nested one is the "Есть видео" badge
        state = card.find("div", class_="complex-card__state", recursive=False)
        items.append(
            ListItem(
                slug=alias.rsplit("/", 1)[-1],
                alias=alias,
                name=_text(card.select_one(".complex-card__title")) or card.get("data-name") or alias,
                status_text=_text(state),
                address=_text(card.select_one(".complex-card__address")),
                complex_id=card.get("data-complex-id"),
            )
        )
    return items


def parse_total_pages(html: str) -> int | None:
    soup = BeautifulSoup(html, "lxml")
    pages = [int(n) for a in soup.select("a.paginator__btn[href]") for n in re.findall(r"page=(\d+)", a["href"])]
    return max(pages) if pages else None


def iter_list(session: PoliteSession, *, max_pages: int | None = None) -> Iterator[ListItem]:
    """Yield every ЖК in the catalogue, paginating until a page has nothing new."""
    seen: set[str] = set()
    page, total = 1, None
    while True:
        html = session.get_text(LIST_URL, params=list_params(page))
        items = parse_list(html)
        if page == 1:
            total = parse_total_pages(html)
            if not items:
                raise RuntimeError(
                    "Krisha list page 1 has no ЖК cards – markup changed? "
                    "(selector: div.complex-card[data-complex-alias])"
                )
            log.info("krisha: %s pages in the catalogue", total or "?")
        new = 0
        for item in items:
            if item.slug in seen:
                continue
            seen.add(item.slug)
            new += 1
            yield item
        if not items or new == 0:
            return
        page += 1
        if (total and page > total) or (max_pages and page > max_pages):
            return


# --------------------------------------------------------------------------- detail
@dataclass
class Stage:
    label: str  # "Первая очередь" or "" for a single deadline
    raw: str
    ordinal: int | None
    date: date | None


@dataclass
class Detail:
    slug: str
    alias: str
    url: str
    name: str
    status_text: str | None
    address: str | None
    builder: str | None
    lon: float | None
    lat: float | None
    stages: list[Stage] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)  # "Класс жилья" → "эконом", "Этажность" → "5 этажей" ...
    docs: dict[str, str] = field(default_factory=dict)
    guarantee: str | None = None  # КЖК guarantee, e.g. "ДПГ-25-02-074/186 от 25.04.2025"
    progress_updated: date | None = None  # "Ход строительства – Обновлено 1 августа 2025"
    progress_titles: list[str] = field(default_factory=list)  # ["I квартал 2022", ...]
    complex_id: str | None = None


def parse_stage(line: str, position: int) -> Stage:
    """"Первая очередь - II квартал 2023 г." → Stage(label="Первая очередь", ordinal=1, date=2023-06-30)."""
    raw = " ".join(_ZERO_WIDTH.sub("", line).split()).strip(" .;,")
    label, dt = "", None
    parts = _SEPARATOR.split(raw, maxsplit=1)
    if len(parts) == 2 and (dt := parse_deadline_text(parts[1])):
        label = parts[0].strip()
    else:
        dt = parse_deadline_text(raw)
        if dt is None:
            label = raw  # e.g. "Сдан" / "уточняется"
    ordinal = None
    if label:
        if m := re.search(r"\d+", label):
            ordinal = int(m.group())
        else:
            low = label.lower()
            for stem, n in _ORDINAL_WORDS:
                if stem in low:
                    ordinal = n
                    break
    return Stage(label=label, raw=raw, ordinal=ordinal, date=dt)


def _window_data(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        m = _WINDOW_DATA.search(text)
        if not m:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, m.end())
        except json.JSONDecodeError as exc:
            log.warning("window.data is not valid JSON: %s", exc)
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _sidebar(soup: BeautifulSoup) -> dict[str, Tag]:
    """Sidebar facts keyed by their title ("Застройщик") and by "@<data-name>" ("@deadline")."""
    out: dict[str, Tag] = {}
    for block in soup.select(".complex__sidebar-info"):
        body = block.select_one(".complex__sidebar-info-text")
        if body is None:
            continue
        title = _text(block.select_one(".complex__sidebar-info-title"))
        if title:
            out[title] = body
        if block.get("data-name"):
            out["@" + block["data-name"]] = body
    return out


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_detail(html: str, url: str = "") -> Detail:
    soup = BeautifulSoup(html, "lxml")
    data = _window_data(soup) or {}
    cx = data.get("complex") if isinstance(data.get("complex"), dict) else {}

    alias = (cx.get("urlAlias") or "").strip("/")
    if not alias and (m := _ALIAS_IN_URL.search(url or "")):
        alias = m.group(1)
    if not alias:
        raise ValueError("cannot determine the ЖК alias (window.data.complex.urlAlias missing)")
    slug = alias.rsplit("/", 1)[-1]
    url = url or f"{BASE_URL}/complex/show/{alias}/"

    sidebar = _sidebar(soup)
    name = _clean(" ".join(str(x) for x in (cx.get("prefix"), cx.get("name")) if x)) or _text(soup.find("h1")) or slug
    address = _clean(cx.get("address")) or _text(sidebar.get("Расположение"))
    status_text = _clean(cx.get("stateText")) or _text(sidebar.get("@home.state")) or _text(sidebar.get("Статус строительства"))
    builder = _text(sidebar.get("Застройщик"))
    coords = cx.get("map") if isinstance(cx.get("map"), dict) else {}
    lat, lon = _float(coords.get("lat")), _float(coords.get("lon"))

    stages: list[Stage] = []
    deadline_block = sidebar.get("@deadline") or sidebar.get("Срок сдачи")
    if deadline_block is not None:
        lines = [ln for ln in deadline_block.get_text("\n").split("\n") if ln.strip()]
        stages = [parse_stage(ln, i) for i, ln in enumerate(lines)]

    params: dict[str, str] = {}
    for dl in soup.select("dl.complex-parameters__block"):
        dt, dd = dl.find("dt"), dl.find("dd")
        if dt is not None and dd is not None and _text(dt):
            params[_text(dt)] = _text(dd) or ""

    docs: dict[str, str] = {}
    for li in soup.select(".complex-docs__list-item"):
        spans = li.find_all("span")
        if len(spans) >= 2 and _text(spans[0]):
            docs[_text(spans[0]).rstrip(":")] = _text(spans[1]) or ""
    guarantee = next((v for k, v in docs.items() if k.lower().startswith("гарантия")), None)

    progress_updated = parse_russian_date(_text(soup.select_one(".construction-progress__subtitle")))
    progress_titles = [
        str(e["title"]) for e in (cx.get("constructionProgress") or []) if isinstance(e, dict) and e.get("title")
    ]

    return Detail(
        slug=slug, alias=alias, url=url, name=name, status_text=status_text, address=address, builder=builder,
        lon=lon, lat=lat, stages=stages, params=params, docs=docs, guarantee=guarantee,
        progress_updated=progress_updated, progress_titles=progress_titles,
        complex_id=str(cx["id"]) if cx.get("id") is not None else None,
    )


# --------------------------------------------------------------------------- rules
def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def compute_deadline(stages: list[Stage], status: str, today: date) -> tuple[str, date | None, str | None]:
    """HANDOVER §3.1 deadline rule → (status, deadline, stage_note).

    deadline = latest очередь date among not-yet-delivered очереди (date >= today);
    delivered ЖК → status "done", deadline = last очередь; every date in the past but not delivered → overdue.
    """
    n = len(stages)
    dated = [s for s in stages if s.date]
    if status == "done":
        deadline = max((s.date for s in dated), default=None)
        return status, deadline, (_plural(n, "очередь сдана", "очереди сданы", "очередей сдано") if n > 1 else None)
    pending = [s for s in dated if s.date >= today]
    if pending:
        deadline = max(s.date for s in pending)
        nxt = min(pending, key=lambda s: s.date)
        note = None
        if n > 1:
            ordinal = nxt.ordinal or (stages.index(nxt) + 1)
            note = f"{ordinal}-я очередь из {n}, сдача {quarter_label(nxt.date)}"
        return status, deadline, note
    if dated:  # everything is past due and the ЖК is still not delivered
        deadline = max(s.date for s in dated)
        note = f"{_plural(n, 'очередь', 'очереди', 'очередей')}, последняя — {quarter_label(deadline)}" if n > 1 else None
        return status, deadline, note
    return status, None, None


def to_project(d: Detail, today: date | None = None) -> Project | None:
    today = today or today_utc()
    if d.lon is None or d.lat is None:
        log.warning("%s: no coordinates in window.data.complex.map – skipped", d.slug)
        return None
    if not in_almaty(d.lon, d.lat):
        log.warning("%s: coordinates %s,%s are outside Almaty – skipped", d.slug, d.lon, d.lat)
        return None
    status = STATUS_MAP.get((d.status_text or "").strip().lower())
    if status is None:
        log.warning("%s: unknown status %r → in_progress", d.slug, d.status_text)
        status = "in_progress"
    status, deadline, note = compute_deadline(d.stages, status, today)
    started = None
    progress_dates = [q for t in d.progress_titles if (q := parse_deadline_text(t))]
    if progress_dates:  # earliest dated "ход строительства" entry (HANDOVER §3.1, optional inference)
        first = min(progress_dates)
        started = quarter_start(first.year, quarter_of(first))
    return Project(
        id=f"krisha:{d.slug}",
        category="apartments",
        name=d.name,
        geometry={"type": "Point", "coordinates": [d.lon, d.lat]},
        source_url=d.url,
        builder=d.builder,
        address=d.address,
        status=status,
        deadline=deadline,
        started_at=started,
        stage_note=note,
    )


# --------------------------------------------------------------------------- runner
def run(
    session: PoliteSession,
    *,
    limit: int | None = None,
    max_pages: int | None = None,
    today: date | None = None,
) -> list[Project]:
    """Crawl the catalogue → list[Project]. ~730 detail pages ≈ 20–25 min at 1.5 s/request."""
    today = today or today_utc()
    projects: list[Project] = []
    skipped = 0
    n = 0
    for item in iter_list(session, max_pages=max_pages):
        if limit is not None and n >= limit:
            break
        n += 1
        try:
            html = session.get_text(item.url)
        except NotFound:
            log.warning("%s: 404 – skipped", item.url)
            skipped += 1
            continue
        except FetchError as exc:  # one bad page; ScrapeAborted (5 in a row) propagates and stops the run
            log.warning("%s: %s – skipped", item.url, exc)
            skipped += 1
            continue
        try:
            detail = parse_detail(html, item.url)
        except Exception:  # noqa: BLE001 – one unparsable page must not kill the crawl
            log.exception("%s: parse failed – skipped", item.url)
            skipped += 1
            continue
        detail.status_text = detail.status_text or item.status_text
        detail.address = detail.address or item.address
        project = to_project(detail, today)
        if project is None:
            skipped += 1
            continue
        projects.append(project)
        if n % 25 == 0:
            log.info("krisha: %d ЖК processed (%d skipped)", n, skipped)
    log.info("krisha: %d projects, %d skipped, session stats %s", len(projects), skipped, session.stats)
    return projects
