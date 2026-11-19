"""Pull live AirPointer readings from Saskatchewan's regional airshed associations
(WYAMZ - Western Yellowhead Air Management Zone, SESAA - Southeast Saskatchewan
Airshed Association, GPAZ - Great Plains Air Zone) and push to Supabase + a
GeoJSON snapshot for SK_Air_Map.

Each station page only exposes a rolling last-24h table, server-rendered behind
a Cloudflare JS challenge - a plain HTTP GET gets a 403, so this uses a headless
browser. Both WYAMZ/SESAA publish "Crawl-delay: 10" in robots.txt; STATION_DELAY_SEC
below honors that between page loads. Data itself only updates hourly, so this
is meant to run once/hour, not more often.

Data is explicitly raw/unvalidated per the source sites themselves
("has not passed through a processed baseline adjustment or validation").

GPAZ note (2026-09-16): of GPAZ's 3 stations listed in Saskatchewan's own
gov station registry, only East Regina is actually live. Its URL differs from
the registry's stale `airquality.php?AP=EastRegina` link - the real, current
one uses `airqualityCB.php?CB=EastRegina`, found via GPAZ's own site nav.
Belle Plaine/Pense (`CB=BellePlaine`, `CB=Pense`) return no data table at all
(page renders with zero rows), and Yorkton (`CB=Yorkton`) returns a real table
frozen at 2023-05-03 - both confirmed dead, not a URL-naming issue, so only
East Regina is wired in below. GPAZ's H1 is just "GPAZ" (no per-station name
unlike WYAMZ/SESAA's own H1), and its column set adds SO2/H2S/PRE and drops
the "AQI" column WYAMZ/SESAA has (GPAZ publishes its own "AQHI" column
instead) - both handled via the `name`/`param_columns` overrides below. GPAZ's
own AQHI column is intentionally NOT ingested (same choice already made for
WYAMZ/SESAA's own "AQI" column) - AQHI_calc below computes it ourselves from
NO2/O3/PM2.5 for a consistent formula across every station on the map.
"""
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from supabase import create_client

STATION_DELAY_SEC = 10
STALE_HOURS = 6  # newest reading older than this = kept off the map + one ALERT
STALE_STATE = "/opt/airquality/logs/sk_airshed_stale_state.json"
ALERTS_LOG = "/opt/airquality/logs/sitrep_alerts.log"

# SK observes no DST (CST year-round, UTC-6)
SK_UTC_OFFSET = timedelta(hours=-6)

# name/coords are town-center references (no per-instrument marker data is
# exposed by the source sites); station_name is overwritten from each page's
# own H1 at scrape time to avoid the Kerrobert/Kindersley URL-vs-label mismatch.
STATIONS = [
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/meadow-lake-air-quality/", "lat": 54.1252, "lon": -108.4307},
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/clavet-air-quality/", "lat": 52.2667, "lon": -106.4000},
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/maidstone-air-quality/", "lat": 53.0961, "lon": -109.3106},
    # URL still says "kindersley" but WYAMZ's page H1 and menu both say
    # Kerrobert; until 2026-11-19 this dot sat at Kindersley (~50 km south).
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/kindersley-air-quality/", "lat": 51.9167, "lon": -109.1333},
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/lloydminster-west-air-quality/", "lat": 53.2783, "lon": -110.0011},
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/lloydminster-east-air-quality/", "lat": 53.2783, "lon": -110.0011},
    {"network": "SESAA", "province": "SK", "url": "https://sesaa.ca/esterhazy-air-quality/", "lat": 50.6497, "lon": -102.0839},
    {"network": "SESAA", "province": "SK", "url": "https://sesaa.ca/estevan-air-quality/", "lat": 49.1386, "lon": -102.9866},
    {"network": "SESAA", "province": "SK", "url": "https://sesaa.ca/glen-ewen-air-quality/", "lat": 49.1936, "lon": -101.7194},
    {"network": "SESAA", "province": "SK", "url": "https://sesaa.ca/oxbow-air-quality/", "lat": 49.2333, "lon": -102.1667},
    {"network": "SESAA", "province": "SK", "url": "https://sesaa.ca/stoughton-air-quality/", "lat": 49.6667, "lon": -103.0500},
    {"network": "SESAA", "province": "SK", "url": "https://sesaa.ca/torquay-air-quality/", "lat": 49.1333, "lon": -103.5333},
    {"network": "SESAA", "province": "SK", "url": "https://sesaa.ca/wauchope-air-quality/", "lat": 49.6667, "lon": -101.9667},
    {"network": "SESAA", "province": "SK", "url": "https://sesaa.ca/weyburn-air-quality/", "lat": 49.6608, "lon": -103.8500},
    {"network": "GPAZ", "province": "SK", "url": "https://www.gpaz.org/airquality/airqualityCB.php?CB=EastRegina",
     "lat": 50.453414, "lon": -104.50604, "name": "East Regina",
     "param_columns": ["NO", "NO2", "NOX", "SO2", "H2S", "O3", "PM2.5", "TEMP", "WS", "WD", "RH", "AP", "PRE"]},
]

# Fallback only - each page's own header row is what decides the column order
# (see header_columns). Fixed 2026-10-27: this list used to be applied to every
# page by position, but the stations don't share one layout - Kerrobert/Torquay/
# Wauchope have no NO/NO2/NOX/O3, Glen Ewen has no PM2.5, Oxbow/Stoughton/Weyburn
# add SO2/H2S - so every value after the first missing column landed under the
# wrong name (TEMP 109 deg C, RH 233 %), and AQHI_calc was built from H2S/SO2/TEMP/WS.
PARAM_COLUMNS = ["NO", "NO2", "NOX", "O3", "PM2.5", "TEMP", "WS", "WD", "RH", "AP", "AQI"]

# GPAZ's own AQHI column - not ingested (see module docstring). WYAMZ/SESAA's
# "AQI" is kept as before (SK_Air_Map shows it as "Site's own AQI").
SKIP_COLUMNS = {"AQHI"}

ROW_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


# Health Canada AQHI, same formula/colors/categories as AB_datapull/AQHI_idw.py
# and SK_datapull/web/SK_regina_blended_grid.py - kept in sync with those.
# WYAMZ/SESAA's own "AQI" column needs CO/H2S/SO2 the AirPointer units don't
# measure (their own pages admit it's "approximate"); AQHI only needs the three
# pollutants these units actually report, so it's a strictly better fit here.
def health_canada_aqhi(no2_ppb, o3_ppb, pm25_ugm3):
    import math
    return (1000.0 / 10.4) * (
        math.exp(0.000871 * no2_ppb) +
        math.exp(0.000537 * o3_ppb) +
        math.exp(0.000487 * pm25_ugm3) - 3.0
    )


def aqhi_color(v):
    if v is None:
        return "#D3D3D3"
    v = round(v)
    if v < 1:
        return "#D3D3D3"
    return {
        1: "#01cbff", 2: "#0099cb", 3: "#016797",
        4: "#fffe03", 5: "#ffcb00", 6: "#ff9835",
        7: "#fd6866", 8: "#fe0002", 9: "#cc0001", 10: "#9a0100",
    }.get(v, "#640100")


def aqhi_category(v):
    if v <= 3:
        return "Low"
    if v <= 6:
        return "Moderate"
    if v <= 10:
        return "High"
    return "Very High"


def header_columns(header_row):
    """Parameter name + unit for each data column, from the table's own header
    row ("NO2 ppb", "PM2.5 \u00b5g/m3", "WS  km/h", ...), skipping "Time"."""
    cols = []
    for cell in header_row.query_selector_all("th,td")[1:]:
        parts = cell.inner_text().split()
        cols.append((parts[0].upper() if parts else "", parts[1] if len(parts) > 1 else ""))
    return cols


def aqhi_3h(hours):
    """AQHI_calc per hour from 3-hour means of NO2/O3/PM2.5 (the hour plus the
    two before it, at least 2 of 3 present per pollutant) - the Health Canada
    definition, and the same rule AEPA's own published AQHI matches exactly.
    hours: {utc_dt: {param: value}}. Returns {utc_dt: raw AQHI}."""
    out = {}
    for t in hours:
        window = [hours.get(t - timedelta(hours=k), {}) for k in range(3)]
        means = {}
        for p in ("NO2", "O3", "PM2.5"):
            vals = [w[p] for w in window if p in w]
            if len(vals) < 2:
                break
            means[p] = sum(vals) / len(vals)
        else:
            out[t] = health_canada_aqhi(means["NO2"], means["O3"], means["PM2.5"])
    return out


def parse_station(page, station):
    # GPAZ's server takes ~40 s to return the HTML (Nov 2026) and its page
    # never reaches "networkidle", so the old 30 s networkidle wait timed out
    # every hour from 2026-11-02. Wait for the DOM + the data table instead.
    page.goto(station["url"], timeout=90000, wait_until="domcontentloaded")
    # Stop waiting as soon as either the data table or the source's own
    # "Real-time data is currently unavailable" notice is on the page
    # (SESAA/WYAMZ show that notice instead of a table during outages).
    try:
        page.wait_for_function(
            "() => document.querySelector('table tr td') || "
            "/currently unavailable/i.test(document.body ? document.body.innerText : '')",
            timeout=30000,
        )
    except Exception:
        pass  # handled below as "no table found"
    page.wait_for_timeout(1000)

    if "name" in station:
        # GPAZ's H1 is just "GPAZ" (no per-station name like WYAMZ/SESAA's
        # own H1s carry) - use the configured name directly instead.
        name = station["name"]
    else:
        h1 = page.query_selector("h1")
        raw_name = h1.inner_text().strip() if h1 else station["url"]
        name = re.sub(r"\s*AIR QUALITY\s*$", "", raw_name, flags=re.I).title().strip()

    table = page.query_selector("table")
    if not table:
        body = page.inner_text("body") if page.query_selector("body") else ""
        if re.search(r"currently unavailable", body, re.I):
            print(f"[SOURCE DOWN] {name}: site says real-time data is currently unavailable ({station['url']})")
        else:
            print(f"[WARN] no table found for {station['url']}")
        return name, []

    rows = table.query_selector_all("tr")
    columns = header_columns(rows[0]) if rows else []
    if not columns or not all(c for c, _ in columns):
        fallback = station.get("param_columns", PARAM_COLUMNS)
        print(f"[WARN] {name}: unreadable header {columns!r}, falling back to {fallback}")
        columns = [(c, "") for c in fallback]

    records = []
    hours = {}
    for row in rows[1:]:  # skip header
        cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
        if not cells or not ROW_RE.match(cells[0]):
            continue  # skips "Maximum Values" / "1 hour Standard" trailer rows
        if len(cells) - 1 != len(columns):
            print(f"[WARN] {name} {cells[0]}: {len(cells) - 1} values for {len(columns)} header columns, row skipped")
            continue
        local_dt = datetime.strptime(cells[0], "%Y-%m-%d %H:%M")
        utc_dt = (local_dt - SK_UTC_OFFSET).replace(tzinfo=timezone.utc)
        row_values = hours.setdefault(utc_dt, {})
        for (col_name, unit), raw_val in zip(columns, cells[1:]):
            if col_name in SKIP_COLUMNS or raw_val in ("", "NA"):
                continue
            try:
                value = float(raw_val)
            except ValueError:
                continue
            if col_name == "WS" and unit.lower() == "km/h":
                value = round(value / 3.6, 2)  # GPAZ publishes km/h, WYAMZ/SESAA m/s
            row_values[col_name] = value
            records.append({
                "station_name": name,
                "network": station["network"],
                "province": station["province"],
                "parameter_name": col_name,
                "reading_date": utc_dt.isoformat(),
                "value": value,
            })
    for utc_dt, aqhi_val in aqhi_3h(hours).items():
        records.append({
            "station_name": name,
            "network": station["network"],
            "province": station["province"],
            "parameter_name": "AQHI_calc",
            "reading_date": utc_dt.isoformat(),
            "value": round(aqhi_val, 2),
        })
    return name, records


def main():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    supabase = create_client(supabase_url, supabase_key)

    all_records = []
    latest_by_station = {}  # name -> {network, lat, lon, params: {name: (value, reading_date)}}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for i, station in enumerate(STATIONS):
            try:
                name, records = parse_station(page, station)
            except Exception as e:
                print(f"[ERROR] {station['url']} failed: {e}")
                records, name = [], None
            if records:
                all_records.extend(records)
                latest_ts = max(r["reading_date"] for r in records)
                latest_by_station[name] = {
                    "network": station["network"],
                    "province": station["province"],
                    "lat": station["lat"],
                    "lon": station["lon"],
                    "reading_date": latest_ts,
                    "source_url": station["url"],
                    "params": {
                        r["parameter_name"]: r["value"]
                        for r in records if r["reading_date"] == latest_ts
                    },
                }
                print(f"[OK] {name} ({station['network']}): {len(records)} readings, latest {latest_ts}")
            if i < len(STATIONS) - 1:
                time.sleep(STATION_DELAY_SEC)
        browser.close()

    print(f"Total records parsed: {len(all_records)}")

    if all_records:
        # upsert in chunks; PK is (station_name, parameter_name, reading_date)
        CHUNK = 500
        try:
            for i in range(0, len(all_records), CHUNK):
                chunk = all_records[i:i + CHUNK]
                supabase.table("airpointer_data").upsert(
                    chunk, on_conflict="station_name,parameter_name,reading_date"
                ).execute()
            print(f"Upserted {len(all_records)} records into airpointer_data")
        except Exception as e:
            print(f"[WARN] Supabase upsert failed (table may not exist yet): {e}")

    # Stale stations: still upserted above, but kept off the map (SK_Air_Map
    # shows whatever is in the snapshot, with no age check) and alerted once
    # on going stale / coming back. GPAZ East Regina stopped publishing
    # 2026-11-02 14:00Z while its page kept serving the old table.
    now = datetime.now(timezone.utc)
    stale = {
        n: info["reading_date"] for n, info in latest_by_station.items()
        if now - datetime.fromisoformat(info["reading_date"]) > timedelta(hours=STALE_HOURS)
    }
    for n, ts in stale.items():
        print(f"[STALE] {n}: newest reading {ts}, kept off the map")
        latest_by_station.pop(n)
    try:
        with open(STALE_STATE) as f:
            was_stale = set(json.load(f))
    except Exception:
        was_stale = set()
    stamp = now.replace(microsecond=0).isoformat()
    try:
        with open(ALERTS_LOG, "a") as f:
            for n in sorted(set(stale) - was_stale):
                f.write(f"{stamp} ALERT sk_airshed: '{n}' went STALE - newest reading "
                        f"{stale[n]} (> {STALE_HOURS} h old); scraped but kept off the SK map\n")
            for n in sorted(was_stale - set(stale)):
                if n in latest_by_station:  # only call it fresh if we actually saw new data
                    f.write(f"{stamp} ALERT sk_airshed: '{n}' back to FRESH - newest reading "
                            f"{latest_by_station[n]['reading_date']}; back on the SK map\n")
        still = set(stale) | {n for n in was_stale if n not in latest_by_station}
        with open(STALE_STATE, "w") as f:
            json.dump(sorted(still), f)
    except Exception as e:
        print(f"[WARN] stale-alert bookkeeping failed: {e}")

    # GeoJSON snapshot for SK_Air_Map
    features = []
    for name, info in latest_by_station.items():
        aqhi_raw = info["params"].get("AQHI_calc")
        # AQHI is reported on a 1-10+ scale by convention (Health Canada's
        # published categories start at 1, there's no "0" tier) even though
        # the raw formula can compute below 1 on very clean/noisy readings -
        # AQHI_calc keeps the true computed value, aqhi_display is the
        # floored value used for color/category/the on-map label.
        aqhi_display = max(1, round(aqhi_raw)) if aqhi_raw is not None else None
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [info["lon"], info["lat"]]},
            "properties": {
                "station_name": name,
                "network": info["network"],
                "province": info["province"],
                "reading_date": info["reading_date"],
                "source_url": info["source_url"],
                **info["params"],
                "aqhi_display": aqhi_display,
                "aqhi_color": aqhi_color(aqhi_display),
                "aqhi_category": aqhi_category(aqhi_display) if aqhi_display is not None else None,
            },
        })

    # Hold-over: a station whose page is down this hour (SESAA/WYAMZ
    # "currently unavailable" outages come and go) keeps its last map entry
    # from the previous snapshot while that reading is <= STALE_HOURS old,
    # marked held=true so SK_Air_Map can show it faded. Coordinates come from
    # STATIONS via source_url (entries written before source_url existed are
    # not held, so an old wrong position can't be carried forward).
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "sk_airshed_current.geojson")
    coords_by_url = {s["url"]: [s["lon"], s["lat"]] for s in STATIONS}
    fresh_urls = {info["source_url"] for info in latest_by_station.values()}
    try:
        with open(out_path) as f:
            previous = json.load(f).get("features", [])
    except Exception:
        previous = []
    for feat in previous:
        props = dict(feat.get("properties") or {})
        url = props.get("source_url")
        if url not in coords_by_url or url in fresh_urls or props.get("station_name") in stale:
            continue
        try:
            age = now - datetime.fromisoformat(props["reading_date"])
        except Exception:
            continue
        if age > timedelta(hours=STALE_HOURS):
            print(f"[DROPPED] {props.get('station_name')}: last reading {props['reading_date']} "
                  f"is over {STALE_HOURS} h old, removed from the map")
            continue
        props["held"] = True
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords_by_url[url]},
            "properties": props,
        })
        print(f"[HELD] {props.get('station_name')}: page down this run, showing last reading "
              f"{props['reading_date']} ({age.total_seconds() / 3600:.1f} h old)")

    geojson = {"type": "FeatureCollection", "features": features}
    with open(out_path, "w") as f:
        json.dump(geojson, f, indent=2)
    print(f"Wrote {len(features)} stations to {out_path}")


if __name__ == "__main__":
    main()
