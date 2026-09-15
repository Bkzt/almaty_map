from datetime import date
from pathlib import Path

import pytest

from scraper import common, krisha

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "krisha_list_page1.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "krisha_detail_kaspii.html").read_text(encoding="utf-8")
DETAIL_URL = "https://krisha.kz/complex/show/almaty/kaspii/"
TODAY = date(2026, 9, 14)


def test_parse_list():
    items = krisha.parse_list(LIST_HTML)
    assert len(items) == krisha.CARDS_PER_PAGE == 12
    first = items[0]
    assert first.slug == "kaspii"
    assert first.alias == "almaty/kaspii"
    assert first.complex_id == "13619455"
    assert first.name == "ЖК Каспий"
    assert first.status_text == "Строящийся"
    assert first.address == "Алматы, Алатауский р-н, 20-й микрорайон, 22"
    assert first.url == DETAIL_URL
    assert {i.status_text for i in items} == {"Строящийся", "Сдан в эксплуатацию"}
    assert len({i.slug for i in items}) == 12
    estet = next(i for i in items if i.slug == "estet")
    assert "​" not in estet.address  # zero-width space in the source is stripped


def test_parse_total_pages():
    assert krisha.parse_total_pages(LIST_HTML) == 61
    assert krisha.parse_total_pages("<html></html>") is None


def test_parse_detail():
    d = krisha.parse_detail(DETAIL_HTML, DETAIL_URL)
    assert d.slug == "kaspii"
    assert d.complex_id == "13619455"
    assert d.name == "ЖК Каспий"
    assert d.builder == "ТОО SMR-EURASIA"
    assert d.status_text == "Строящийся"
    assert d.address == "Алматы, Алатауский р-н, 20-й микрорайон, 22"
    assert d.lat == pytest.approx(43.294864, abs=1e-5)
    assert d.lon == pytest.approx(76.830434, abs=1e-5)
    assert [s.date for s in d.stages] == [
        date(2023, 6, 30), date(2023, 12, 31), date(2025, 3, 31), date(2027, 9, 30),
        date(2027, 12, 31), date(2026, 9, 30), date(2027, 12, 31),
    ]
    assert [s.ordinal for s in d.stages] == [1, 2, 3, 4, 5, 6, 7]
    assert d.stages[0].label == "Первая очередь"
    assert d.params["Класс жилья"] == "эконом"
    assert d.params["Этажность"] == "5 этажей"
    assert d.params["Количество квартир"] == "1440"
    assert d.guarantee == "ДПГ-25-02-074/186 от 25.04.2025"
    assert d.progress_updated == date(2025, 8, 1)
    assert "I квартал 2022" in d.progress_titles


def test_clean_strips_zero_width_and_whitespace():
    assert krisha._clean("Алматы,  ул. \u200bРозыбакиева, 197/2") == "Алматы, ул. Розыбакиева, 197/2"
    assert krisha._clean("   ") is None
    assert krisha._clean(None) is None


def test_parse_detail_cleans_json_address():
    html = DETAIL_HTML.replace('"address":"Алматы, Алатауский р-н, 20-й микрорайон, 22"', '"address":"Алматы, ул. \\u200bБрауна 24/4"', 1)
    assert krisha.parse_detail(html, DETAIL_URL).address == "Алматы, ул. Брауна 24/4"


def test_parse_detail_without_url_uses_alias_from_json():
    d = krisha.parse_detail(DETAIL_HTML)
    assert d.url == DETAIL_URL


def test_to_project():
    d = krisha.parse_detail(DETAIL_HTML, DETAIL_URL)
    p = krisha.to_project(d, today=TODAY)
    assert p is not None
    assert p.id == "krisha:kaspii"
    assert p.category == "apartments"
    assert p.status == "in_progress"
    assert p.deadline == date(2027, 12, 31)  # latest of the not-yet-delivered очереди
    assert p.stage_note == "6-я очередь из 7, сдача Q3 2026"
    assert p.started_at == date(2022, 1, 1)  # earliest "ход строительства" entry: I квартал 2022
    assert p.builder == "ТОО SMR-EURASIA"
    assert p.source_url == DETAIL_URL
    assert p.geometry["type"] == "Point"
    assert p.geometry["coordinates"][0] == pytest.approx(76.830434, abs=1e-5)  # [lon, lat]
    assert p.geometry["coordinates"][1] == pytest.approx(43.294864, abs=1e-5)
    feature = p.to_feature()
    assert feature["properties"]["deadline"] == "2027-12-31"
    assert feature["properties"]["updated_at"].endswith("+00:00")


def _stages(*dates):
    return [krisha.Stage(label=f"{i + 1} очередь", raw="", ordinal=i + 1, date=d) for i, d in enumerate(dates)]


def test_compute_deadline_rules():
    # every date passed but still "строящийся" → overdue, deadline = latest stated date
    status, deadline, note = krisha.compute_deadline(_stages(date(2024, 6, 30), date(2025, 12, 31)), "in_progress", TODAY)
    assert (status, deadline) == ("in_progress", date(2025, 12, 31))
    assert "2 очереди" in note
    # delivered → done, deadline = last очередь
    assert krisha.compute_deadline(_stages(date(2024, 6, 30)), "done", TODAY)[:2] == ("done", date(2024, 6, 30))
    # no stated dates at all
    assert krisha.compute_deadline([], "in_progress", TODAY) == ("in_progress", None, None)
    # single future stage → no stage note
    assert krisha.compute_deadline(_stages(date(2027, 3, 31)), "in_progress", TODAY) == ("in_progress", date(2027, 3, 31), None)
    # a stage dated today counts as pending
    assert krisha.compute_deadline(_stages(TODAY), "in_progress", TODAY)[1] == TODAY


def test_parse_stage_variants():
    s = krisha.parse_stage("2-я очередь: 4 кв. 2027", 5)
    assert (s.ordinal, s.date, s.label) == (2, date(2027, 12, 31), "2-я очередь")
    s = krisha.parse_stage("IV квартал 2026 г.", 0)
    assert (s.ordinal, s.date, s.label) == (None, date(2026, 12, 31), "")
    s = krisha.parse_stage("Седьмая очередь - IV квартал 2027 г.", 6)
    assert (s.ordinal, s.date) == (7, date(2027, 12, 31))
    s = krisha.parse_stage("Сдан", 0)
    assert s.date is None and s.label == "Сдан"


def test_unknown_status_defaults_to_in_progress():
    d = krisha.parse_detail(DETAIL_HTML, DETAIL_URL)
    d.status_text = "Что-то новое"
    assert krisha.to_project(d, today=TODAY).status == "in_progress"


def test_missing_coordinates_skips_project():
    d = krisha.parse_detail(DETAIL_HTML, DETAIL_URL)
    d.lat = None
    assert krisha.to_project(d, today=TODAY) is None


class FakeSession:
    """Serves the fixtures; every other detail page is a 404."""

    stats = {"fake": True}

    def get_text(self, url, params=None, **kw):
        if url == krisha.LIST_URL:
            return LIST_HTML if (params or {}).get("page") == 1 else "<html></html>"
        if url == DETAIL_URL:
            return DETAIL_HTML
        raise common.NotFound(url)


def test_run_end_to_end_on_fixtures():
    projects = krisha.run(FakeSession(), limit=3, today=TODAY)
    assert [p.id for p in projects] == ["krisha:kaspii"]  # the other two ЖК 404 → skipped, run continues


def test_run_raises_when_list_markup_changes():
    class EmptySession(FakeSession):
        def get_text(self, url, params=None, **kw):
            return "<html><body>nothing here</body></html>"

    with pytest.raises(RuntimeError, match="markup"):
        krisha.run(EmptySession(), today=TODAY)
