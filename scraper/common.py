"""Shared building blocks for every feed.

* ``Project`` – the single schema every feed emits (HANDOVER §4).
* ``write_geojson`` / ``read_geojson`` – the ``data/projects.geojson`` file.
* ``PoliteSession`` – rate-limited, disk-cached, robots-aware HTTP (HANDOVER §7).
* Russian date helpers (``"II квартал 2023 г."`` → ``2023-06-30``).
* ``Geocoder`` – Yandex Geocoder HTTP API with a committed JSON cache.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import robotparser
from urllib.parse import urlencode, urlsplit

import requests

log = logging.getLogger(__name__)

VERSION = "0.1"
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROJECTS_PATH = DATA_DIR / "projects.geojson"
GEOCODE_CACHE_PATH = DATA_DIR / "geocode_cache.json"
CACHE_DIR = ROOT / "scraper" / ".cache"  # raw HTML cache, git-ignored

CATEGORIES = ("apartments", "road", "repair", "other")
STATUSES = ("planned", "in_progress", "done")

# Almaty, lon/lat. Used to sanity-check coordinates and to bias the geocoder.
ALMATY_BBOX = (76.70, 43.10, 77.10, 43.45)
ALMATY_CENTER = (76.889, 43.238)


# --------------------------------------------------------------------------- schema
@dataclass
class Project:
    """One construction project. ``geometry`` is GeoJSON, coordinates ``[lon, lat]``."""

    id: str  # stable: krisha:<slug>, road:<hash>, gz:<lot_id>
    category: str  # apartments | road | repair | other
    name: str
    geometry: dict
    source_url: str
    builder: str | None = None
    address: str | None = None
    status: str = "in_progress"  # planned | in_progress | done
    deadline: date | None = None
    started_at: date | None = None
    stage_note: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def validate(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"{self.id}: bad category {self.category!r}")
        if self.status not in STATUSES:
            raise ValueError(f"{self.id}: bad status {self.status!r}")
        gtype = self.geometry.get("type")
        coords = self.geometry.get("coordinates")
        if gtype == "Point":
            _check_lonlat(self.id, coords)
        elif gtype == "LineString":
            if not isinstance(coords, list) or len(coords) < 2:
                raise ValueError(f"{self.id}: LineString needs >= 2 points")
            for c in coords:
                _check_lonlat(self.id, c)
        else:
            raise ValueError(f"{self.id}: unsupported geometry {gtype!r}")
        if not self.name or not self.source_url:
            raise ValueError(f"{self.id}: name and source_url are required")

    def to_feature(self) -> dict:
        self.validate()
        geometry = dict(self.geometry)
        geometry["coordinates"] = _round_coords(geometry["coordinates"])
        return {
            "type": "Feature",
            "id": self.id,
            "geometry": geometry,
            "properties": {
                "id": self.id,
                "category": self.category,
                "name": self.name,
                "builder": self.builder or None,
                "address": self.address or None,
                "status": self.status,
                "deadline": self.deadline.isoformat() if self.deadline else None,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "stage_note": self.stage_note or None,
                "source_url": self.source_url,
                "updated_at": self.updated_at.replace(microsecond=0).isoformat(),
            },
        }


def _check_lonlat(pid: str, c: Any) -> None:
    if not (isinstance(c, (list, tuple)) and len(c) == 2):
        raise ValueError(f"{pid}: coordinate must be [lon, lat], got {c!r}")
    lon, lat = c
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"{pid}: coordinate out of range {c!r}")
    if not (60 < lon < 90 and 40 < lat < 50):
        # Every project on this map is in Almaty. Swapped lon/lat is the classic bug.
        raise ValueError(f"{pid}: coordinate {c!r} is not near Almaty (expected [lon, lat])")


def in_almaty(lon: float, lat: float, pad: float = 0.15) -> bool:
    w, s, e, n = ALMATY_BBOX
    return (w - pad) <= lon <= (e + pad) and (s - pad) <= lat <= (n + pad)


def _round_coords(coords: Any) -> Any:
    if isinstance(coords, (int, float)):
        return round(float(coords), 6)
    return [_round_coords(c) for c in coords]


# --------------------------------------------------------------------------- geojson
def write_geojson(
    items: Iterable[Project | dict],
    path: Path = PROJECTS_PATH,
    *,
    generated_at: datetime | None = None,
    extra: dict | None = None,
) -> dict:
    """Write a FeatureCollection, one feature per line (git-diff friendly). Returns the collection."""
    features = [it.to_feature() if isinstance(it, Project) else it for it in items]
    features.sort(key=lambda f: f["properties"]["id"])
    counts: dict[str, int] = {}
    for f in features:
        cat = f["properties"]["category"]
        counts[cat] = counts.get(cat, 0) + 1
    stamp = (generated_at or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    collection = {
        "type": "FeatureCollection",
        "generated_at": stamp,
        "counts": counts,
        **(extra or {}),
        "features": features,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("{\n")
        for key, value in collection.items():
            if key == "features":
                continue
            fh.write(f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},\n")
        fh.write('"features": [\n')
        for i, f in enumerate(features):
            fh.write(json.dumps(f, ensure_ascii=False, separators=(",", ":")))
            fh.write(",\n" if i < len(features) - 1 else "\n")
        fh.write("]\n}\n")
    os.replace(tmp, path)
    return collection


def read_geojson(path: Path = PROJECTS_PATH) -> dict | None:
    path = Path(path)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- dates
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
_MONTH_RE = (
    r"(?P<month>январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|"
    r"августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])"
)
_MONTH_INDEX = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]

_ISO_DATE = re.compile(r"\b(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})\b")
_DMY_DATE = re.compile(r"\b(?P<d>\d{1,2})\.(?P<m>\d{2})\.(?P<y>20\d{2})\b")
_DAY_MONTH_YEAR = re.compile(r"\b(?P<d>\d{1,2})\s+" + _MONTH_RE + r"\s+(?P<y>20\d{2})", re.I)
_QUARTER = re.compile(
    r"(?:(?P<roman>IV|I{1,3})|(?P<num>[1-4]))\s*(?:-?\s*(?:й|я|го|ый|ой)\s*)?"
    r"(?:кв(?:артал[еа]?)?\.?|q)\s*(?P<y>20\d{2})",
    re.I,
)
_Q_PREFIX = re.compile(r"\bQ(?P<num>[1-4])\s*[-/ ]?\s*(?P<y>20\d{2})", re.I)
_HALF = re.compile(
    r"(?:(?P<roman>II|I)|(?P<num>[12]))\s*(?:-?\s*(?:е|ое|го)\s*)?(?:полугодие|пол\.|п/г)\s*(?P<y>20\d{2})", re.I
)
_MONTH_YEAR = re.compile(_MONTH_RE + r"\s*(?P<y>20\d{2})", re.I)
_YEAR = re.compile(r"\b(?P<y>20\d{2})\b")


def quarter_end(year: int, q: int) -> date:
    month = q * 3
    return date(year, month, calendar.monthrange(year, month)[1])


def quarter_start(year: int, q: int) -> date:
    return date(year, q * 3 - 2, 1)


def quarter_of(d: date) -> int:
    return (d.month - 1) // 3 + 1


def quarter_label(d: date | None) -> str | None:
    """2027-09-30 → "Q3 2027"."""
    return f"Q{quarter_of(d)} {d.year}" if d else None


def _month_number(word: str) -> int:
    w = word.lower()
    for i, stem in enumerate(_MONTH_INDEX):
        if w.startswith(stem):
            return i + 1
    raise ValueError(word)


def parse_deadline_text(text: str | None) -> date | None:
    """Turn the many ways Kazakh sites write a deadline into a date (last day of the period).

    "II квартал 2023 г." → 2023-06-30; "4 кв. 2026" → 2026-12-31; "Q3 2027" → 2027-09-30;
    "2 полугодие 2026" → 2026-12-31; "декабрь 2026" → 2026-12-31; "2027" → 2027-12-31;
    "2026-10-31" and "31.10.2026" are taken literally. Returns None if nothing matches.
    """
    if not text:
        return None
    t = " ".join(str(text).split())
    if m := _ISO_DATE.search(t):
        return date(int(m["y"]), int(m["m"]), int(m["d"]))
    if m := _DMY_DATE.search(t):
        return date(int(m["y"]), int(m["m"]), int(m["d"]))
    if m := _DAY_MONTH_YEAR.search(t):
        return date(int(m["y"]), _month_number(m["month"]), int(m["d"]))
    if m := _Q_PREFIX.search(t):
        return quarter_end(int(m["y"]), int(m["num"]))
    if m := _QUARTER.search(t):
        q = _ROMAN[m["roman"].upper()] if m["roman"] else int(m["num"])
        return quarter_end(int(m["y"]), q)
    if m := _HALF.search(t):
        h = len(m["roman"]) if m["roman"] else int(m["num"])
        return quarter_end(int(m["y"]), h * 2)
    if m := _MONTH_YEAR.search(t):
        y, mo = int(m["y"]), _month_number(m["month"])
        return date(y, mo, calendar.monthrange(y, mo)[1])
    if m := _YEAR.search(t):
        return date(int(m["y"]), 12, 31)
    return None


def parse_russian_date(text: str | None) -> date | None:
    """"Обновлено 1 августа 2025" → 2025-08-01."""
    if not text:
        return None
    m = _DAY_MONTH_YEAR.search(" ".join(text.split()))
    if not m:
        return None
    return date(int(m["y"]), _month_number(m["month"]), int(m["d"]))


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- HTTP
class ScrapeAborted(RuntimeError):
    """Too many consecutive failures – stop the run instead of fighting the site."""


class RobotsDisallowed(RuntimeError):
    """robots.txt forbids this URL for us."""


class NotFound(RuntimeError):
    """HTTP 404 – a data fact, not a failure."""


class FetchError(RuntimeError):
    """One URL failed after retries."""


def default_user_agent() -> str:
    """Honest bot UA. Put your contact e-mail in the CONTACT_EMAIL env var (HANDOVER §7)."""
    contact = os.environ.get("CONTACT_EMAIL", "").strip() or "contact not set"
    repo = os.environ.get("PROJECT_URL", "").strip()
    parts = [f"almaty-build-map/{VERSION}", "non-commercial open-data map of Almaty construction deadlines"]
    if repo:
        parts.append(f"+{repo}")
    parts.append(f"contact: {contact}")
    return f"{parts[0]} ({'; '.join(parts[1:])})"


class PoliteSession:
    """requests.Session with the HANDOVER §7 rules baked in.

    * >= ``min_delay`` seconds between network requests, single thread;
    * honest User-Agent; robots.txt respected;
    * retry with backoff on 429 / 5xx / network errors;
    * ``ScrapeAborted`` after ``max_consecutive_failures`` failures in a row;
    * raw responses cached on disk so a re-parse never re-hits the site.
    """

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        min_delay: float = 1.5,
        user_agent: str | None = None,
        cache_dir: Path | None = CACHE_DIR,
        use_cache: bool = True,
        max_consecutive_failures: int = 5,
        max_retries: int = 3,
        timeout: float = 30.0,
        respect_robots: bool = True,
        refresh: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_delay = min_delay
        self.refresh = refresh  # True → ignore cached bodies for every request (still writes the cache)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.use_cache = use_cache and self.cache_dir is not None
        self.max_consecutive_failures = max_consecutive_failures
        self.max_retries = max_retries
        self.timeout = timeout
        self.respect_robots = respect_robots
        self._sleep = sleep
        self._http = requests.Session()
        self._http.headers.update(
            {"User-Agent": user_agent or default_user_agent(), "Accept-Language": "ru,en;q=0.8"}
        )
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last_request_at = 0.0
        self.consecutive_failures = 0
        self.stats = {"network": 0, "cache_hits": 0, "failures": 0}

    # -- public -------------------------------------------------------------
    def get_text(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        refresh: bool = False,
        cache: bool = True,
        respect_robots: bool | None = None,
    ) -> str:
        """GET ``url`` and return the body as text (cached copy if available).

        ``respect_robots`` overrides the session default for this one call. Pass ``False`` for an
        authenticated call to an official API (e.g. the Geocoder) – robots.txt governs web crawlers
        discovering pages, not a developer's own key-authenticated request to a documented HTTP API.
        """
        full_url = self._full_url(url, params)
        cache_path = self._cache_path(full_url) if (self.use_cache and cache) else None
        if cache_path and not (refresh or self.refresh) and cache_path.exists():
            self.stats["cache_hits"] += 1
            return cache_path.read_text(encoding="utf-8")
        resp = self._fetch(full_url, headers, respect_robots)
        text = resp.text
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text, encoding="utf-8")
            meta = {"url": full_url, "status": resp.status_code, "fetched_at": datetime.now(timezone.utc).isoformat()}
            cache_path.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        return text

    def get_json(self, url: str, **kw: Any) -> Any:
        return json.loads(self.get_text(url, **kw))

    def cache_path_for(self, url: str, params: dict | None = None) -> Path | None:
        """Where ``get_text(url, params=...)`` would cache its body (used by tests / cache seeding)."""
        return self._cache_path(self._full_url(url, params)) if self.cache_dir else None

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _full_url(url: str, params: dict | None) -> str:
        if not params:
            return url
        query = urlencode(params, doseq=True)
        return f"{url}{'&' if '?' in url else '?'}{query}"

    def _cache_path(self, full_url: str) -> Path:
        assert self.cache_dir is not None
        key = hashlib.sha1(full_url.encode("utf-8")).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.body"

    def _wait_turn(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_delay:
            self._sleep(self.min_delay - elapsed)

    def _raw_get(self, url: str, headers: dict | None) -> requests.Response:
        self._wait_turn()
        try:
            return self._http.get(url, headers=headers, timeout=self.timeout, allow_redirects=True)
        finally:
            self._last_request_at = time.monotonic()
            self.stats["network"] += 1

    def _allowed_by_robots(self, url: str, respect_robots: bool | None) -> bool:
        if not (self.respect_robots if respect_robots is None else respect_robots):
            return True
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                resp = self._raw_get(f"{host}/robots.txt", None)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp = None  # no robots.txt → everything allowed
            except requests.RequestException as exc:
                log.warning("robots.txt for %s unavailable (%s); assuming allowed", host, exc)
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch("almaty-build-map", url)

    def _fetch(self, url: str, headers: dict | None, respect_robots: bool | None = None) -> requests.Response:
        if not self._allowed_by_robots(url, respect_robots):
            raise RobotsDisallowed(url)
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._raw_get(url, headers)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("GET %s failed (%s), attempt %d/%d", url, last_error, attempt + 1, self.max_retries + 1)
                self._backoff(attempt, None)
                continue
            if resp.status_code == 200:
                self.consecutive_failures = 0
                return resp
            if resp.status_code == 404:
                self.consecutive_failures = 0
                raise NotFound(url)
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code in self.RETRY_STATUSES and attempt < self.max_retries:
                log.warning("GET %s → %s, backing off (attempt %d)", url, last_error, attempt + 1)
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            break
        self._register_failure(url, last_error)
        raise FetchError(f"{url}: {last_error}")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = 2.0 * (2**attempt)
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        self._sleep(min(delay, 120.0))

    def _register_failure(self, url: str, error: str) -> None:
        self.consecutive_failures += 1
        self.stats["failures"] += 1
        log.error("GET %s failed: %s (%d consecutive)", url, error, self.consecutive_failures)
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise ScrapeAborted(
                f"{self.consecutive_failures} consecutive failures, last: {url} ({error}). "
                "Stopping – if the site is blocking us, switch to an alternative source (HANDOVER §3.1)."
            )


# --------------------------------------------------------------------------- geocoder
class Geocoder:
    """Yandex Geocoder HTTP API (free tier: 1 000 requests/day) with a committed JSON cache.

    Without an API key it still serves cached results, so CI works after the first run.
    """

    URL = "https://geocode-maps.yandex.ru/v1/"

    def __init__(
        self,
        api_key: str | None,
        session: PoliteSession,
        *,
        cache_path: Path = GEOCODE_CACHE_PATH,
        bbox: tuple[float, float, float, float] = ALMATY_BBOX,
    ) -> None:
        self.api_key = api_key or None
        self.session = session
        self.cache_path = Path(cache_path)
        self.bbox = bbox
        self.cache: dict[str, dict] = {}
        self.requests_made = 0
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")) or {}
            except json.JSONDecodeError:
                log.warning("geocode cache %s is not valid JSON – starting empty", self.cache_path)

    def geocode(self, query: str) -> dict | None:
        """→ {"lon", "lat", "text", "kind", "precision"} or None. Cached by query string."""
        query = " ".join(query.split())
        if query in self.cache:
            return self.cache[query]
        if not self.api_key:
            log.warning("no YANDEX_GEOCODER_KEY and %r not in cache – skipped", query)
            return None
        w, s, e, n = self.bbox
        params = {
            "apikey": self.api_key,
            "geocode": query,
            "format": "json",
            "lang": "ru_RU",
            "results": 1,
            "bbox": f"{w},{s}~{e},{n}",
            "rspn": 1,
        }
        self.requests_made += 1
        # never cache the key on disk; robots.txt governs crawlers, not our own key-authenticated API call
        data = self.session.get_json(self.URL, params=params, cache=False, respect_robots=False)
        members = data.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
        if not members:
            log.warning("geocoder: nothing found for %r", query)
            return None
        obj = members[0]["GeoObject"]
        lon, lat = (float(x) for x in obj["Point"]["pos"].split())
        meta = obj.get("metaDataProperty", {}).get("GeocoderMetaData", {})
        result = {
            "lon": lon,
            "lat": lat,
            "text": meta.get("text"),
            "kind": meta.get("kind"),
            "precision": meta.get("precision"),
        }
        if not in_almaty(lon, lat):
            log.warning("geocoder: %r resolved outside Almaty (%s) – ignored", query, result["text"])
            return None
        self.cache[query] = result
        self.save()
        return result

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
