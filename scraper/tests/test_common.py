from datetime import date, datetime, timezone

import pytest

from scraper.common import (
    FetchError,
    PoliteSession,
    Project,
    RobotsDisallowed,
    ScrapeAborted,
    parse_deadline_text,
    parse_russian_date,
    quarter_label,
    read_geojson,
    write_geojson,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("II квартал 2023 г.", date(2023, 6, 30)),
        ("IV квартал 2027 г.", date(2027, 12, 31)),
        ("I квартал 2025", date(2025, 3, 31)),
        ("III квартал 2026 г.", date(2026, 9, 30)),
        ("4 кв. 2026", date(2026, 12, 31)),
        ("2-й квартал 2026 года", date(2026, 6, 30)),
        ("Q3 2027", date(2027, 9, 30)),
        ("2 полугодие 2026", date(2026, 12, 31)),
        ("I полугодие 2027", date(2027, 6, 30)),
        ("декабрь 2026", date(2026, 12, 31)),
        ("конец 2026 года", date(2026, 12, 31)),
        ("2026-10-31", date(2026, 10, 31)),
        ("31.10.2026", date(2026, 10, 31)),
        ("1 августа 2025", date(2025, 8, 1)),
        ("", None),
        (None, None),
        ("Сдан", None),
    ],
)
def test_parse_deadline_text(text, expected):
    assert parse_deadline_text(text) == expected


def test_parse_russian_date():
    assert parse_russian_date("Обновлено 1 августа 2025") == date(2025, 8, 1)
    assert parse_russian_date("Обновлено недавно") is None


def test_quarter_label():
    assert quarter_label(date(2027, 9, 30)) == "Q3 2027"
    assert quarter_label(date(2026, 1, 1)) == "Q1 2026"
    assert quarter_label(None) is None


SCHEMA_FIELDS = {
    "id", "category", "name", "builder", "address", "status", "deadline", "started_at", "stage_note",
    "source_url", "updated_at",
}


def test_geojson_roundtrip(tmp_path):
    project = Project(
        id="krisha:x",
        category="apartments",
        name="ЖК X",
        geometry={"type": "Point", "coordinates": [76.8304342744, 43.2948641020]},
        source_url="https://krisha.kz/complex/show/almaty/x/",
        deadline=date(2027, 12, 31),
        updated_at=datetime(2026, 9, 14, 6, 41, 25, tzinfo=timezone.utc),
    )
    road = Project(
        id="road:abc",
        category="road",
        name="Абая (Достык – Розыбакиева)",
        geometry={"type": "LineString", "coordinates": [[76.95, 43.24], [76.89, 43.24]]},
        source_url="https://example.com",
        status="in_progress",
    )
    out = tmp_path / "p.geojson"
    write_geojson([road, project], out, generated_at=datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc))
    data = read_geojson(out)
    assert data["type"] == "FeatureCollection"
    assert data["generated_at"] == "2026-09-14T06:00:00+00:00"
    assert data["counts"] == {"apartments": 1, "road": 1}
    assert [f["id"] for f in data["features"]] == ["krisha:x", "road:abc"]  # sorted by id
    feature = data["features"][0]
    assert feature["geometry"]["coordinates"] == [76.830434, 43.294864]
    props = feature["properties"]
    assert set(props) == SCHEMA_FIELDS
    assert props["deadline"] == "2027-12-31"
    assert props["status"] == "in_progress"
    assert props["updated_at"] == "2026-09-14T06:41:25+00:00"
    assert props["builder"] is None
    # one feature per line keeps git diffs readable
    lines = out.read_text(encoding="utf-8").splitlines()
    assert sum(1 for ln in lines if ln.startswith('{"type":"Feature"')) == 2


def test_swapped_coordinates_rejected():
    project = Project(id="x", category="road", name="x", geometry={"type": "Point", "coordinates": [43.29, 76.83]}, source_url="u")
    with pytest.raises(ValueError):
        project.to_feature()


def test_bad_enum_rejected():
    project = Project(id="x", category="houses", name="x", geometry={"type": "Point", "coordinates": [76.9, 43.2]}, source_url="u")
    with pytest.raises(ValueError):
        project.to_feature()


class FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def test_polite_session_delay_cache_and_failures(tmp_path, monkeypatch):
    sleeps: list[float] = []
    session = PoliteSession(
        min_delay=1.5, cache_dir=tmp_path, respect_robots=False, sleep=sleeps.append,
        max_consecutive_failures=2, max_retries=0,
    )
    calls: list[str] = []

    def fake_get(url, headers=None, timeout=None, allow_redirects=True):
        calls.append(url)
        return FakeResponse(200, "<html>ok</html>")

    monkeypatch.setattr(session._http, "get", fake_get)
    assert session.get_text("https://example.com/a") == "<html>ok</html>"
    assert session.get_text("https://example.com/a") == "<html>ok</html>"  # served from disk cache
    assert calls == ["https://example.com/a"]
    assert session.stats["cache_hits"] == 1
    assert session.cache_path_for("https://example.com/a").exists()

    session.get_text("https://example.com/b")  # second network hit must wait for the 1.5 s gap
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.5

    monkeypatch.setattr(session._http, "get", lambda url, **kw: FakeResponse(403))
    with pytest.raises(FetchError):
        session.get_text("https://example.com/c")
    with pytest.raises(ScrapeAborted):  # 2 consecutive failures → stop the run
        session.get_text("https://example.com/d")
    assert session.get_text("https://example.com/a") == "<html>ok</html>"  # cache still works


def test_retry_on_503_then_success(tmp_path, monkeypatch):
    sleeps: list[float] = []
    session = PoliteSession(cache_dir=None, respect_robots=False, sleep=sleeps.append, max_retries=2)
    responses = [FakeResponse(503), FakeResponse(200, "fine")]
    monkeypatch.setattr(session._http, "get", lambda url, **kw: responses.pop(0))
    assert session.get_text("https://example.com/x") == "fine"
    assert any(s >= 2.0 for s in sleeps)  # backoff happened


def test_robots_disallow(monkeypatch):
    session = PoliteSession(cache_dir=None, sleep=lambda _: None)

    def fake_get(url, headers=None, timeout=None, allow_redirects=True):
        if url.endswith("/robots.txt"):
            return FakeResponse(200, "User-agent: *\nDisallow: /private/\n")
        return FakeResponse(200, "ok")

    monkeypatch.setattr(session._http, "get", fake_get)
    assert session.get_text("https://example.com/public") == "ok"
    with pytest.raises(RobotsDisallowed):
        session.get_text("https://example.com/private/x")
