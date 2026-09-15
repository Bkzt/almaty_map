from datetime import date

from scraper import roads

SRC = "https://example.com/plan-2026"
HEADER = "name,street,from_street,to_street,work_type,deadline,source_url,notes\n"


class FakeGeocoder:
    def __init__(self, table):
        self.table = table
        self.saved = False
        self.requests_made = 0

    def geocode(self, query):
        return self.table.get(query)

    def save(self):
        self.saved = True


def test_rows_become_linestrings(tmp_path):
    csv_path = tmp_path / "roads.csv"
    csv_path.write_text(
        HEADER
        + f",Абая,проспект Достык,Розыбакиева,благоустройство,Q4 2026,{SRC},\n"
        + f",Жарокова,Аль-Фараби,Толе би,благоустройство,,{SRC},\n"  # one end cannot be geocoded → skipped
        + f"Своё имя,Сатпаева,Луганского,Байзакова,реконструкция,2026-11-30,,\n",  # no source_url → skipped
        encoding="utf-8",
    )
    geo = FakeGeocoder(
        {
            "Алматы, Абая, проспект Достык": {"lon": 76.95, "lat": 43.24},
            "Алматы, пересечение Абая и Розыбакиева": {"lon": 76.89, "lat": 43.24},  # second template
            "Алматы, Жарокова, Аль-Фараби": {"lon": 76.90, "lat": 43.21},
        }
    )
    projects = roads.run(geo, path=csv_path, today=date(2026, 9, 14))
    assert len(projects) == 1
    r = projects[0]
    assert r.category == "road"
    assert r.name == "Абая (проспект Достык – Розыбакиева)"
    assert r.address == "Абая, от проспект Достык до Розыбакиева"
    assert r.geometry == {"type": "LineString", "coordinates": [[76.95, 43.24], [76.89, 43.24]]}
    assert r.deadline == date(2026, 12, 31)
    assert r.stage_note == "благоустройство"
    assert r.status == "in_progress"
    assert r.id.startswith("road:") and len(r.id) == 15
    assert r.source_url == SRC
    assert geo.saved
    assert r.to_feature()["geometry"]["type"] == "LineString"


def test_road_id_is_stable_and_case_insensitive():
    a = {"street": "Абая", "from_street": "Достык", "to_street": "Розыбакиева", "work_type": "благоустройство"}
    b = {**a, "street": "абая", "name": "anything", "deadline": "2027"}
    assert roads.road_id(a) == roads.road_id(b)


def test_seed_csv_is_well_formed():
    rows = roads.read_rows(roads.CSV_PATH)
    assert len(rows) >= 10
    for row in rows:
        assert row["street"] and row["from_street"] and row["to_street"], row
        assert row["source_url"].startswith("http"), row
    ids = [roads.road_id(r) for r in rows]
    assert len(ids) == len(set(ids))
