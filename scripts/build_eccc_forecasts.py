from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from io import StringIO

import pandas as pd
import requests


URL = "https://weather.gc.ca/airquality/pages/provincial_summary/sk_e.html"

ROOT = Path(".")
DATA_DIR = ROOT / "data"
OUT = DATA_DIR / "aqhi_forecasts.geojson"
STATIONS_CSV = DATA_DIR / "stations.csv"


def clean_text(x) -> str:
    if x is None:
        return ""
    return re.sub(r"\s+", " ", str(x)).strip()


def extract_aqhi(x):
    txt = clean_text(x)
    m = re.search(r"\b(10\+|\d{1,2})\b", txt)
    if not m:
        return None
    return 11 if m.group(1) == "10+" else int(m.group(1))


def risk_category(v):
    if v is None:
        return None
    if v <= 3:
        return "Low Risk"
    if v <= 6:
        return "Moderate Risk"
    if v <= 10:
        return "High Risk"
    return "Very High Risk"


def norm_name(x):
    return clean_text(x).lower()


def load_station_lookup():
    if not STATIONS_CSV.exists():
        return {}

    st = pd.read_csv(STATIONS_CSV)
    lookup = {}

    name_col = "name" if "name" in st.columns else "station"
    lat_col = "lat" if "lat" in st.columns else "Latitude"
    lon_col = "lon" if "lon" in st.columns else "Longitude"

    for _, r in st.iterrows():
        name = r.get(name_col)
        lat = r.get(lat_col)
        lon = r.get(lon_col)

        try:
            lat = float(lat)
            lon = float(lon)
        except Exception:
            continue

        lookup[norm_name(name)] = {
            "lat": lat,
            "lon": lon,
            "station_meta_name": clean_text(name),
        }

    return lookup


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching ECCC AQHI summary: {URL}")

    response = requests.get(
        URL,
        timeout=30,
        headers={"User-Agent": "SK_datapull AQHI forecast builder"},
    )
    response.raise_for_status()
    
    if "Air Quality Health Index" not in response.text:
        raise RuntimeError("Unexpected ECCC response page")

    tables = pd.read_html(StringIO(response.text))

    
    
    
    if not tables:
        raise RuntimeError("No tables found on ECCC AQHI summary page.")

    df = tables[1]

    # Flatten possible multi-index columns.
    df.columns = [
        " ".join([str(x) for x in c if str(x) != "nan"]).strip()
        if isinstance(c, tuple)
        else str(c)
        for c in df.columns
    ]

    print("Detected columns:")
    for c in df.columns:
        print(f"  - {c}")

    # First column is location.
    location_col = df.columns[0]

    # Forecast columns are normally the last four columns.
    forecast_cols = [
        c for c in df.columns
        if any(x in str(c).lower() for x in [
            "today",
            "tonight",
            "tomorrow"
        ])
    ]
    
    print("Forecast columns detected:")
    for c in forecast_cols:
        print("  ", c)

    station_lookup = load_station_lookup()

    features = []
    issued_at = None

    m = re.search(r"Forecast issued at:\s*([^<\n]+)", response.text)
    if m:
        issued_at = clean_text(m.group(1))

    for _, row in df.iterrows():
        name = clean_text(row.get(location_col))

        if not name or name.lower() in {"nan", "location and sub-locations"}:
            continue

        meta = station_lookup.get(norm_name(name), {})

        props = {
            "name": name,
            "source": "ECCC AQHI provincial summary",
            "source_url": URL,
            "issued_at": issued_at,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }

        forecast_values = row.tolist()[1:5]
        
        forecast_labels = [
            "Today",
            "Tonight",
            "Tomorrow",
            "Tomorrow Night"
        ]
        
        for i, raw in enumerate(forecast_values, start=1):
        
            raw = clean_text(raw)
            aqhi = extract_aqhi(raw)
        
            props[f"p{i}_label"] = forecast_labels[i - 1]
            props[f"p{i}_raw"] = raw
            props[f"p{i}_aqhi"] = aqhi
            props[f"p{i}_category"] = risk_category(aqhi)

        geom = None
        if "lat" in meta and "lon" in meta:
            geom = {
                "type": "Point",
                "coordinates": [meta["lon"], meta["lat"]],
            }
            props["lat"] = meta["lat"]
            props["lon"] = meta["lon"]

        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": props,
            }
        )

    out = {
        "type": "FeatureCollection",
        "name": "ECCC_SK_AQHI_Forecasts",
        "features": features,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Saved {OUT}: {len(features)} forecast locations")


if __name__ == "__main__":
    main()
