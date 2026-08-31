import requests
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
import pandas as pd
import numpy as np

# =========================================================
# RATE-LIMITED FETCH
# =========================================================
# The province's ArcGIS org enforces a shared per-minute request quota
# across all these station endpoints (6000 request units/min). Hitting
# 6+ endpoints back-to-back can trip a 429 on whichever request lands
# last, which silently dropped that station's data for the whole run.
# A short delay between requests plus a retry-with-backoff on 429
# (honoring the API's own "retry after 60 sec" guidance) fixes that.

REQUEST_DELAY_SECONDS = 3
RATE_LIMIT_RETRY_SECONDS = 65
MAX_RETRIES = 3


def fetch_json(url, params, label):
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.get(url, params=params)
        try:
            data = r.json()
        except ValueError:
            print(f"FAILED (bad JSON): {label}")
            return None

        if isinstance(data, dict) and data.get("error", {}).get("code") == 429:
            print(f"Rate limited on {label} (attempt {attempt}/{MAX_RETRIES}); "
                  f"waiting {RATE_LIMIT_RETRY_SECONDS}s before retrying...")
            if attempt < MAX_RETRIES:
                time.sleep(RATE_LIMIT_RETRY_SECONDS)
                continue
            print(f"FAILED (still rate-limited after {MAX_RETRIES} attempts): {label}")
            return None

        return data
    return None


# =========================================================
# CURRENT STATION GEOMETRY
# =========================================================

CURRENT_API = "https://services3.arcgis.com/zcv98lgAl8xQ04cW/ArcGIS/rest/services/Hourly_Ambient_Air_Quality/FeatureServer/0/query"

# =========================================================
# HISTORICAL RAW TABLES
# =========================================================

RAW_APIS = {
    "Regina":
        "https://services3.arcgis.com/zcv98lgAl8xQ04cW/ArcGIS/rest/services/Regina_Ambient_Air_Quality_Raw/FeatureServer/0/query",

    "Saskatoon":
        "https://services3.arcgis.com/zcv98lgAl8xQ04cW/ArcGIS/rest/services/Saskatoon_Ambient_Air_Quality_Raw/FeatureServer/0/query",

    "Prince Albert":
        "https://services3.arcgis.com/zcv98lgAl8xQ04cW/ArcGIS/rest/services/Prince_Albert_Ambient_Air_Quality_Raw/FeatureServer/0/query",
    
    "Estevan":
        "https://services3.arcgis.com/zcv98lgAl8xQ04cW/ArcGIS/rest/services/Estevan_Historical_Ambient_Air_Quality_Raw/FeatureServer/0/query",

    "Swift Current":
        "https://services3.arcgis.com/zcv98lgAl8xQ04cW/ArcGIS/rest/services/Swift_Current_Ambient_Air_Quality_Raw/FeatureServer/0/query",

    "Buffalo Narrows":
        "https://services3.arcgis.com/zcv98lgAl8xQ04cW/ArcGIS/rest/services/Buffalo_Narrows_Ambient_Air_Quality_Raw/FeatureServer/0/query"
}

OUTPUT = Path("data/current_map.geojson")

# =========================================================
# TIME WINDOW
# =========================================================

now_utc = datetime.now(timezone.utc)
cutoff = now_utc - timedelta(hours=36)

start_ms = int(cutoff.timestamp() * 1000)

# =========================================================
# CLEANER
# =========================================================

def clean_val(x):
    try:
        x = float(x)

        if x <= -999:
            return None

        return round(x, 1)

    except:
        return None

# =========================================================
# AQHI
# =========================================================

def calc_aqhi(pm25, no2, o3):

    try:

        if None in [pm25, no2, o3]:
            return None

        aqhi = (10/10.4) * (
            100 * (
                math.exp(0.000871 * no2) +
                math.exp(0.000537 * o3) +
                math.exp(0.000487 * pm25) - 3
            )
        )

        aqhi = round(aqhi)

        return max(1, min(10, aqhi))

    except:
        return None

# =========================================================
# GET CURRENT GEOMETRY
# =========================================================

print("\n=== PULLING CURRENT STATIONS ===")

current_data = fetch_json(
    CURRENT_API,
    {
        "where": "1=1",
        "outFields": "COMMUNITY,DATETIME",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson"
    },
    "station geometry"
)

station_geometry = {}

if current_data:
    for f in current_data["features"]:

        p = f["properties"]

        station_geometry[p["COMMUNITY"]] = f["geometry"]

print("Current geometry stations:", len(station_geometry))

# =========================================================
# PULL RAW HISTORICAL DATA
# =========================================================

rows = []

for i, (station, api) in enumerate(RAW_APIS.items()):

    if i > 0:
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\n=== PULLING {station} ===")

    data = fetch_json(
        api,
        {
            "where": "1=1",
            "outFields": "*",
            "orderByFields": "DATETIME DESC",
            "resultRecordCount": 2000,
            "f": "json"
        },
        station
    )

    if not data or "features" not in data:
        print("FAILED:", station)
        if data:
            print(json.dumps(data, indent=2)[:3000])
        continue

    print("Rows returned:", len(data["features"]))

    for f in data["features"]:

        a = f["attributes"]

        pm25 = clean_val(a.get("PM2_5"))
        no2  = clean_val(a.get("NO2"))
        o3   = clean_val(a.get("O3"))

        rows.append({

            "station": station,

            "datetime": datetime.fromtimestamp(
                a["DATETIME"] / 1000,
                timezone.utc
            ),

            "PM25": pm25,
            "NO2": no2,
            "O3": o3,

            "WS": clean_val(a.get("WS")),
            "WD": clean_val(a.get("WD")),
            "TEMP": clean_val(a.get("TEMP")),
            "RH": clean_val(a.get("RH")),

            "AQHI": calc_aqhi(pm25, no2, o3),

            "geometry": station_geometry.get(station)
        })

# =========================================================
# DATAFRAME
# =========================================================

df = pd.DataFrame(rows)

if len(df) == 0:
    raise SystemExit("NO HISTORICAL DATA RETURNED")

df = df[df["datetime"] >= cutoff]
    
print("\n=== RAW DATAFRAME CHECK ===")
print("Total rows:", len(df))
print("Stations:", df["station"].nunique())

# =========================================================
# SORT
# =========================================================

df = df.sort_values(["station", "datetime"])

# =========================================================
# LAGS
# =========================================================

df["AQHI_lag1"]  = df.groupby("station")["AQHI"].shift(1)
df["AQHI_lag2"]  = df.groupby("station")["AQHI"].shift(2)
df["AQHI_lag3"]  = df.groupby("station")["AQHI"].shift(3)
df["AQHI_lag6"]  = df.groupby("station")["AQHI"].shift(6)
df["AQHI_lag12"] = df.groupby("station")["AQHI"].shift(12)
df["AQHI_lag24"] = df.groupby("station")["AQHI"].shift(24)

# =========================================================
# CHANGES
# =========================================================

df["AQHI_change_1h"] = df["AQHI"] - df["AQHI_lag1"]
df["AQHI_change_3h"] = df["AQHI"] - df["AQHI_lag3"]

# =========================================================
# TEMPORAL
# =========================================================

df["hour"] = df["datetime"].dt.hour
df["doy"]  = df["datetime"].dt.dayofyear

df["sin_hour"] = np.sin(2*np.pi*df["hour"]/24)
df["cos_hour"] = np.cos(2*np.pi*df["hour"]/24)

df["sin_doy"] = np.sin(2*np.pi*df["doy"]/365)
df["cos_doy"] = np.cos(2*np.pi*df["doy"]/365)

# =========================================================
# DROP INCOMPLETE
# =========================================================

stations_before_lag_filter = set(df["station"].unique())

df = df.dropna(subset=[
    "AQHI_lag1",
    "AQHI_lag2",
    "AQHI_lag3",
    "AQHI_lag6"
])

print("\nRows after lag filtering:", len(df))

dropped_stations = stations_before_lag_filter - set(df["station"].unique())
for station in sorted(dropped_stations):
    print(f"Skipping {station}; insufficient lag history within the cutoff window")

# =========================================================
# LATEST
# =========================================================

latest = df.groupby("station").tail(1)

print("\nLatest rows:", len(latest))

# =========================================================
# GUARD: DON'T PUBLISH AN EMPTY RESULT
# =========================================================
# Mirrors AB_AQHI_Forecast's validate_features(): if every station's data is
# too stale/incomplete to produce a feature row, fail loudly here rather
# than writing an empty geojson. run_sk_site_update.sh's `set -e` then stops
# before the git add/commit/push, so the last known-good current_map.geojson
# stays published instead of being overwritten with an empty FeatureCollection.
if len(latest) == 0:
    raise SystemExit(
        "No station has a complete current-conditions feature row "
        "(all stations dropped by lag filtering or upstream fetch failure). "
        "Refusing to overwrite the published output."
    )

# =========================================================
# SAVE FEATURES
# =========================================================

latest.to_csv("data/current_features.csv", index=False)

# =========================================================
# BUILD GEOJSON
# =========================================================

features = []

for _, row in latest.iterrows():

    feature = {

        "type": "Feature",

        "geometry": row["geometry"],

        "properties": {

            "station": row["station"],

            "AQHI": row["AQHI"],

            "PM25": row["PM25"],
            "NO2": row["NO2"],
            "O3": row["O3"],

            "WS": row["WS"],
            "WD": row["WD"],
            "TEMP": row["TEMP"],
            "RH": row["RH"],

            "AQHI_lag1": row["AQHI_lag1"],
            "AQHI_lag2": row["AQHI_lag2"],
            "AQHI_lag3": row["AQHI_lag3"],
            "AQHI_lag6": row["AQHI_lag6"],

            "AQHI_change_1h": row["AQHI_change_1h"],
            "AQHI_change_3h": row["AQHI_change_3h"],

            "sin_hour": row["sin_hour"],
            "cos_hour": row["cos_hour"],
            "sin_doy": row["sin_doy"],
            "cos_doy": row["cos_doy"],

            "updated": row["datetime"].isoformat()
        }
    }

    features.append(feature)

geojson = {
    "type": "FeatureCollection",
    "features": features
}

OUTPUT.parent.mkdir(parents=True, exist_ok=True)

with open(OUTPUT, "w") as f:
    json.dump(geojson, f)

print("\nMap features:", len(features))
