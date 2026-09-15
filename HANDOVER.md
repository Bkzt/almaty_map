# Almaty Construction Map — Handover for Claude Code

_Last updated: 2026-09-14. Owner: Bekzat. Status: planning done, no code written yet._

## 1. Goal (read this first)

One web page. Open it → see a **Yandex map of Almaty** with pins for every current construction project and its **deadline**. Categories: apartments (ЖК), roads, repairs / other state construction. Popup per pin: name, who is building it, deadline, link to source. That's the whole product. No accounts, no backend, no search — a map with pins and a category filter.

**Non-goals (do not build):** comps/valuation logic, price analytics, scraping developer websites one by one, scraping 2GIS's website, anything requiring login or CAPTCHA bypass, storing personal data (agent/owner phone numbers, ИИН).

## 2. Architecture (decided)

```
scraper (Python, weekly)  ──►  data/projects.geojson  ──►  index.html (Yandex Maps JS API 3.0)
        │                                                        ▲
        └── GitHub Actions cron commits the file ────────────────┘   served by GitHub Pages
```

- **Static site, no server, no database.** `index.html` fetches `data/projects.geojson` and renders it.
- **One schema for every feed** (see §4). Feeds are interchangeable behind it.
- **Python 3.11+**, `requests` + `beautifulsoup4` (or `httpx` + `selectolax`), `sqlite3` only as a scrape cache if needed. No Scrapy, no headless browser unless a feed truly requires it (homeportal might).
- **Yandex Maps JS API 3.0** for the map; **Yandex Geocoder HTTP API** for feeds that only have addresses. Both on the free tier (Geocoder: 1,000 req/day). One API key covers both.

## 3. Data sources — what was verified and what to use

### 3.1 Apartments / ЖК — build FIRST (Phase 1)

**Source: Krisha.kz ЖК catalog.**
- List: `https://krisha.kz/complex/search/almaty/` — 729 ЖК in Almaty as of 2026‑09‑14, ~61 pages, server‑rendered HTML with pagination.
- Detail: `https://krisha.kz/complex/show/almaty/<slug>/` (example: `/complex/show/almaty/kaspii/`).
- Fields confirmed present on a detail page: застройщик (e.g. "ТОО SMR-EURASIA"), status ("Строящийся" / "Сдан в эксплуатацию"), класс, этажность, number of buildings, number of apartments, full address ("Алатауский р-н, 20-й микрорайон, 22"), **сдача per очередь** (e.g. 1‑я — Q2 2023 … 7‑я — Q4 2027), a "Ход строительства" photo section with last‑update date, КЖК guarantee reference ("ДПГ-25-02-074/186 от 25.04.2025").
- **Map coordinates are embedded in the detail page** (map widget init) — extract lat/lon from there; no geocoding needed for this feed. Verify the exact JS/JSON blob on the first fetch and pin the selector.
- Deadline rule: `deadline` = latest очередь date among not‑yet‑delivered очереди; if all delivered → status `done`, exclude from "current" by default (keep in file, filter in UI).
- Krisha does **not** publish construction start date. Optional: infer `started_at` from the earliest dated ход строительства photo.

Alternatives with the same data if Krisha blocks: `korter.kz` (новостройки Алматы), `homsters.kz` (has per‑ЖК "course-of-construction" pages, e.g. `homsters.kz/rams/rams-city/course-of-construction`), `novostroyki-almaty.kz` (per‑ЖК `/construction` pages, per‑developer counts). Do not build these unless Krisha fails.

### 3.2 Roads — Phase 1, hand-maintained

No machine-readable source exists. The akimat (Управление городской мобильности) announces yearly street lists as text; media (bes.media) redraw them in Yandex Map Constructor by hand; per‑segment deadlines are usually not published.

**Decision:** `data/roads.csv` maintained by hand:
```
name,street,from_street,to_street,work_type,deadline,source_url,notes
```
Script geocodes the two intersections (`street × from_street`, `street × to_street`) with Yandex Geocoder (cache results in `data/geocode_cache.json`) and emits a `LineString` feature. ~10–30 rows/year. Seed rows from the 2026 list, e.g. "Абая – от проспекта Достык до Розыбакиева", "Жарокова – от Аль-Фараби до Толе би", "Сатпаева – от Луганского до Байзакова" (sources: bes.media 2026 map article; zakon.kz 2026‑08‑20 list).

### 3.3 Repairs / other state construction — Phase 2 (blocked on token)

**Source: goszakup.gov.kz official API.**
- Base `https://ows.goszakup.gov.kz/`, v3 REST + GraphQL (`/v3/graphql`), Bearer token, 50 items/page, cursor pagination (`page=next&search_after=<id>`).
- Docs: `https://old.goszakup.gov.kz/ru/developer/ows_v3`, `https://ows.goszakup.gov.kz/help/`.
- **Token is NOT self‑service**: requires a letter to the Ministry of Finance ("Настоящим, просим дать доступ к унифицированным сервисам Портала государственных закупок и выпустить токен для авторизации. Данные унифицированных сервисов планируется использовать для …"). Send it early; expect weeks.
- Plan: pull `/v3/trd-buy` + lots filtered by publishDate and customer BIN (Almaty akimat departments, e.g. Управление градостроительного контроля г. Алматы is supplier/customer id 346218 on the portal; collect BINs of Управление городской мобильности, Управление образования, etc.), keep lots whose name matches `строительств|СМР|реконструкц|капитальн.*ремонт|благоустройств`, geocode the delivery address text with Yandex Geocoder, `deadline` = contract completion date.
- Until the token arrives this feed is empty and the map is still shipped.

### 3.4 Sources checked and rejected / parked

- `building.uaig.kz` (akimat's 2021 "карта строительства") → iframe to ArcGIS Web AppBuilder on `gis.uaig.kz`. **Server's TLS certificate is expired, nothing loads (checked 2026‑09‑14).** Would be the best source (ArcGIS REST query → JSON with geometry) if it ever returns. Re‑check quarterly; do not depend on it.
- `homeportal.kz/shared-construction-objects` (КЖК registry of ЖК with долевое permits/guarantees: developer, contractor, permit/guarantee type, сдача, floors, apartments). Client‑rendered Nuxt app — find the JSON endpoint via devtools Network tab before scraping. Optional enrichment only; Krisha already shows the guarantee number.
- `data.egov.kz` — has a "Строительство и ЖКХ" category but no Almaty permits/notifications dataset found. Option: file a dataset request on the portal for the registry of уведомления о начале СМР (Управление градостроительного контроля). If it ever appears, it becomes the master list.
- `zakup.sk.kz` (Samruk‑Kazyna) — no public API found; HTML only; low value for this map.
- `tenderbot.kz`, `mitwork.kz`, `tenderplus.kz` — paid aggregators, ToS forbid scraping. Don't.
- **2GIS** — do not scrape the website. Official Places API (`catalog.api.2gis.com/3.0/items?q=…&location=lon,lat&key=…`) demo key = 1,000 requests **total**; docs say it returns only active (operating) objects, so under‑construction buildings may be absent. Not needed for Phase 1 (Krisha gives coordinates). Park.
- Developer websites (BI Group, BAZIS‑A, RAMS) — JS‑heavy, each different, publish only marketing data (no start dates, no permits). Aggregators already normalise them. Don't scrape directly.

## 4. Data schema (fixed — every feed emits this)

GeoJSON `FeatureCollection`; each `Feature` is a `Point` (ЖК, repairs) or `LineString` (roads) with `properties`:

| field | type | notes |
|---|---|---|
| `id` | string | stable, `krisha:<slug>`, `road:<hash>`, `gz:<lot_id>` |
| `category` | enum | `apartments` \| `road` \| `repair` \| `other` |
| `name` | string | "ЖК Каспий", "Абая (Достык – Розыбакиева)" |
| `builder` | string\|null | developer / contractor / customer |
| `address` | string\|null | |
| `status` | enum | `planned` \| `in_progress` \| `done` |
| `deadline` | ISO date\|null | quarter → last day of quarter (`Q3 2027` → `2027-09-30`) |
| `started_at` | ISO date\|null | usually null |
| `stage_note` | string\|null | e.g. "3-я очередь из 7" |
| `source_url` | string | |
| `updated_at` | ISO datetime | scrape time |

Coordinates in GeoJSON order `[lon, lat]`. Keep `done` items in the file; the UI hides them by default.

## 5. Map page requirements

- Yandex Maps JS API 3.0, centred on Almaty (`43.238, 76.889`), zoom 11.
- Pins coloured by `category`; **red outline/marker if `deadline < today` and status ≠ done** (overdue).
- Clustering for `apartments` at low zoom (there are ~700 points).
- Category filter checkboxes + "show completed" toggle + text "updated: <date>" from the file.
- Popup: name, builder, deadline (human format "Q3 2027" + exact date on hover), stage_note, link to `source_url`.
- Must work on phone width. Single `index.html`, no build step. API key read from one clearly marked constant.

## 6. Repo layout

```
almaty-build-map/
  index.html
  data/projects.geojson        # generated, committed
  data/roads.csv               # hand-maintained
  data/geocode_cache.json      # generated, committed
  scraper/
    common.py                  # schema dataclass, GeoJSON writer, polite HTTP session
    krisha.py                  # feed: apartments
    roads.py                   # feed: roads (csv + geocoder)
    goszakup.py                # feed: repairs/other (stub until token)
    build.py                   # runs feeds → merges → writes data/projects.geojson
    tests/                     # fixture HTML from one Krisha list page + one detail page
  .github/workflows/refresh.yml   # weekly cron, runs build.py, commits data/
  README.md
```

## 7. Scraping rules (non-negotiable)

- ≥ 1.5 s between requests, single thread, honest `User-Agent` with a contact email, respect `robots.txt`, retry with backoff on 429/5xx, stop the run after 5 consecutive failures.
- Cache raw HTML locally during development; never re-hit the site to re-parse.
- Never collect phone numbers, ИИН, or any personal data. Only ЖК/lot‑level facts.
- Never bypass CAPTCHAs, anti‑bot challenges, or login walls. If a source starts blocking, stop and switch to an alternative in §3.1, don't fight it.
- Republishing: the map shows facts + a link back to the source. Don't copy photos or descriptions.

## 8. Build order (tasks for Claude Code)

1. **Scaffold** repo per §6; `common.py` with the schema, GeoJSON writer, polite session.
2. **Krisha list parser**: fetch page 1 of `/complex/search/almaty/`, save fixture, parse slugs + status + district; paginate until empty. Unit test on fixture.
3. **Krisha detail parser**: fetch `/complex/show/almaty/kaspii/`, save fixture, extract all §3.1 fields + coordinates; convert очереди to `deadline`. Unit test on fixture.
4. **build.py** → `data/projects.geojson`; run a full crawl once (≈730 detail pages ≈ 20–25 min at 1.5 s).
5. **index.html** per §5; test locally with `python -m http.server`.
6. **roads.py** + seed `roads.csv` with the 2026 street list; geocode with cache.
7. **GitHub Actions** weekly cron + Pages deployment.
8. **goszakup.py** stub with the request logic written against the v3 docs, disabled until `GOSZAKUP_TOKEN` env var exists.

Definition of done for Phase 1: open the Pages URL on a phone, see ~700 ЖK pins + road lines, filter by category, overdue ones red, popup with deadline and source link, file refreshed by cron without manual steps.

## 9. Things Bekzat has to do himself

- Get a free **Yandex Maps API key** (developer cabinet) → put in `index.html` and as `YANDEX_GEOCODER_KEY` secret in GitHub.
- Send the **goszakup token letter** to the Ministry of Finance (template in §3.3).
- Fill `data/roads.csv` from the current year's akimat list.
- Optional: file a dataset request on data.egov.kz for the уведомления о начале СМР registry.

## 10. Reference links

- Krisha ЖК Almaty: https://krisha.kz/complex/search/almaty/ · example detail: https://krisha.kz/complex/show/almaty/kaspii/
- goszakup API: https://old.goszakup.gov.kz/ru/developer/ows · v3 docs: https://old.goszakup.gov.kz/ru/developer/ows_v3 · help: https://ows.goszakup.gov.kz/help/
- КЖК registry: https://homeportal.kz/shared-construction-objects
- Yandex Maps API tariffs / free limits: https://yandex.ru/maps-api/tariffs · https://yandex.ru/maps-api/docs/news/new-billing/free-usage.html
- 2GIS Places API (parked): https://docs.2gis.com/en/api/search/places/overview
- Akimat map (dead, re-check): http://building.uaig.kz/ → https://gis.uaig.kz/arcgis/...
- Roads 2026 lists: https://bes.media/news/tsentr-almati-nakroet-pilyu-iz-za-remonta-ulits-v-2026-godu-interaktivnaya-karta/ · https://www.zakon.kz/obshestvo/6528739-na-kakikh-ulitsakh-almaty-idet-remont-i-deystvuyut-ogranicheniya-dlya-avto--spisok.html
- Largest developers context: https://digitalbusiness.kz/2026-05-21/komu-prinadlezhat-krupneyshie-stroykompanii-kazahstana-vladeltsi-bi-group-bazis-a-rams-i-drugih/ · https://novostroyki-almaty.kz/developers
