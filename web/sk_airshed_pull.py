"""Pull live AirPointer readings from Saskatchewan's regional airshed associations
(WYAMZ - Western Yellowhead Air Management Zone, SESAA - Southeast Saskatchewan
Airshed Association) and push to Supabase + a GeoJSON snapshot for SK_Air_Map.

Each station page only exposes a rolling last-24h table, server-rendered behind
a Cloudflare JS challenge - a plain HTTP GET gets a 403, so this uses a headless
browser. Both sites publish "Crawl-delay: 10" in robots.txt; STATION_DELAY_SEC
below honors that between page loads. Data itself only updates hourly, so this
is meant to run once/hour, not more often.

Data is explicitly raw/unvalidated per the source sites themselves
("has not passed through a processed baseline adjustment or validation").
"""
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from supabase import create_client

STATION_DELAY_SEC = 10

# SK observes no DST (CST year-round, UTC-6)
SK_UTC_OFFSET = timedelta(hours=-6)

# name/coords are town-center references (no per-instrument marker data is
# exposed by the source sites); station_name is overwritten from each page's
# own H1 at scrape time to avoid the Kerrobert/Kindersley URL-vs-label mismatch.
STATIONS = [
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/meadow-lake-air-quality/", "lat": 54.1252, "lon": -108.4307},
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/clavet-air-quality/", "lat": 52.2667, "lon": -106.4000},
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/maidstone-air-quality/", "lat": 53.0961, "lon": -109.3106},
    {"network": "WYAMZ", "province": "SK", "url": "https://wyamz.ca/kindersley-air-quality/", "lat": 51.4667, "lon": -109.1667},
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
]

PARAM_COLUMNS = ["NO", "NO2", "NOX", "O3", "PM2.5", "TEMP", "WS", "WD", "RH", "AP", "AQI"]

ROW_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


# Health Canada AQHI, same formula/colors/categories as AB_datapull/AQHI_idw.py
# and SK_datapull/web/SK_regina_blended_grid.py - kept in sync with those.
# WYAMZ/SESAA's own "AQI" column needs CO/H2S/SO2 the AirPointer units don't
# measure (their own pages admit it's "approximate"); AQHI only needs the three
# pollutants these units actually report, so it's a strictly better fit here.
def health_canada_aqhi(no2_ppb, o3_ppb, pm25_ugm3):
    import math
    return (1000.0 / 10.4) * (
        math.exp(0.000537 * no2_ppb) +
        math.exp(0.000871 * o3_ppb) +
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


def parse_station(page, station):
    page.goto(station["url"], timeout=30000, wait_until="networkidle")
    page.wait_for_timeout(3000)

    h1 = page.query_selector("h1")
    raw_name = h1.inner_text().strip() if h1 else station["url"]
    name = re.sub(r"\s*AIR QUALITY\s*$", "", raw_name, flags=re.I).title().strip()

    table = page.query_selector("table")
    if not table:
        print(f"[WARN] no table found for {station['url']}")
        return name, []

    rows = table.query_selector_all("tr")
    records = []
    for row in rows[1:]:  # skip header
        cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
        if not cells or not ROW_RE.match(cells[0]):
            continue  # skips "Maximum Values" / "1 hour Standard" trailer rows
        local_dt = datetime.strptime(cells[0], "%Y-%m-%d %H:%M")
        utc_dt = (local_dt - SK_UTC_OFFSET).replace(tzinfo=timezone.utc)
        row_values = {}
        for col_name, raw_val in zip(PARAM_COLUMNS, cells[1:]):
            if raw_val in ("", "NA"):
                continue
            try:
                value = float(raw_val)
            except ValueError:
                continue
            row_values[col_name] = value
            records.append({
                "station_name": name,
                "network": station["network"],
                "province": station["province"],
                "parameter_name": col_name,
                "reading_date": utc_dt.isoformat(),
                "value": value,
            })
        if all(k in row_values for k in ("NO2", "O3", "PM2.5")):
            aqhi_val = health_canada_aqhi(row_values["NO2"], row_values["O3"], row_values["PM2.5"])
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
                **info["params"],
                "aqhi_display": aqhi_display,
                "aqhi_color": aqhi_color(aqhi_display),
                "aqhi_category": aqhi_category(aqhi_display) if aqhi_display is not None else None,
            },
        })
    geojson = {"type": "FeatureCollection", "features": features}
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "sk_airshed_current.geojson")
    with open(out_path, "w") as f:
        json.dump(geojson, f, indent=2)
    print(f"Wrote {len(features)} stations to {out_path}")


if __name__ == "__main__":
    main()
