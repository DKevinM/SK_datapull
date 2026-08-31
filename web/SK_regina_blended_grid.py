"""
SK_regina_blended_grid.py

Builds four AQHI surface products for SK_datapull:

1. data/sk_current_blend.geojson
2. data/sk_forecast_3h_blend.geojson
3. data/regina_current_blend.geojson
4. data/regina_forecast_3h_blend.geojson

Current grids:
    observed AQHI station values + PurpleAir eAQHI

Forecast grids:
    RF 3-hour station forecast + PurpleAir persistence eAQHI

Weights:
    stations   = 1.0
    PurpleAir  = 0.5

Notes:
    - This first operational version uses Random Forest only.
    - Cubist files are .rds.gz and require an R prediction step; add that later as a second stage.
"""

from __future__ import annotations

import ast
import gzip
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from shapely.geometry import Point, Polygon, shape, mapping
from shapely.ops import unary_union

try:
    import geopandas as gpd
except Exception:  # pragma: no cover
    gpd = None


# =========================================================
# CONFIG
# =========================================================

ROOT = Path(".")
DATA_DIR = ROOT / "data"
DATA_SK_DIR = ROOT / "data"
MODELS_DIR = ROOT / "models"

CURRENT_FEATURES = DATA_DIR / "current_features.csv"
CURRENT_MAP = DATA_DIR / "current_map.geojson"
STATIONS_CSV = DATA_DIR / "stations.csv"
PURPLE_LOCAL_1 = DATA_DIR / "SK_PM25_map.json"
PURPLE_LOCAL_2 = DATA_SK_DIR / "SK_PM25_map.json"
AIRPOINTER_LOCAL = DATA_DIR / "sk_airshed_current.geojson"
SK_SHP = DATA_SK_DIR / "SK.shp"

RF_MODEL_PATHS = [
    MODELS_DIR / "aqhi_3h_model.pkl.gz",
    MODELS_DIR / "aqhi_3h_model.pkl",
]

OUT_SK_CURRENT = DATA_DIR / "sk_current_blend.geojson"
OUT_SK_FORECAST = DATA_DIR / "sk_forecast_3h_blend.geojson"
OUT_REGINA_CURRENT = DATA_DIR / "regina_current_blend.geojson"
OUT_REGINA_FORECAST = DATA_DIR / "regina_forecast_3h_blend.geojson"

STATION_WEIGHT = 1.0
AIRPOINTER_WEIGHT = 0.75
PURPLE_WEIGHT = 0.5
IDW_POWER = 2

# Sanity bounds on the *computed* AQHI value before it's allowed into the
# interpolation - a point marker tolerates one bad raw reading sitting on
# its own dot, but IDW smears an outlier across every cell within
# MAX_IDW_DIST_KM, so a single broken sensor (e.g. Kerrobert reporting a
# 157.5C temp) can visibly warp the grid over a wide area if let through.
AIRPOINTER_AQHI_MIN = -1.0
AIRPOINTER_AQHI_MAX = 50.0
MAX_IDW_DIST_KM = 150.
MIN_POINTS_SK = 1
MIN_POINTS_REGINA = 1

# Regina domain
REGINA_LAT = 50.4452
REGINA_LON = -104.6189
REGINA_RADIUS_KM = 100.0

# Grid size. 0.05 degrees is roughly 4–6 km around Regina.
REGINA_GRID_STEP_DEG = 0.02

# Provincial grid should stay coarser for browser performance.
SK_GRID_STEP_DEG = 0.10

# PurpleAir persistence age limit.
# If you are troubleshooting stale files, temporarily increase this or set to None.
MAX_PA_AGE_HOURS = None

FEATURE_COLS = [
    "AQHI",
    "AQHI_lag1",
    "AQHI_lag2",
    "AQHI_lag3",
    "AQHI_lag6",
    "AQHI_lag12",
    "AQHI_lag24",
    "AQHI_change_1h",
    "AQHI_change_3h",
    "PM25",
    "O3",
    "NO2",
    "WS",
    "U",
    "V",
    "TEMP",
    "RH",
    "sin_hour",
    "cos_hour",
    "sin_doy",
    "cos_doy",
    "lat_norm",
    "lon_norm",
    "dist_center",
]


# =========================================================
# GENERAL HELPERS
# =========================================================

def clean_num(x) -> Optional[float]:
    if x is None or x == "" or str(x).lower() in {"nan", "none", "null"}:
        return None
    try:
        v = float(x)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


def aqhi_color(v: float) -> str:
    if v is None or not math.isfinite(v):
        return "#D3D3D3"
    try:
        v = round(float(v))
    except Exception:
        return "#D3D3D3"
    if v < 1:
        return "#D3D3D3"
    if v == 1:
        return "#01cbff"
    if v == 2:
        return "#0099cb"
    if v == 3:
        return "#016797"
    if v == 4:
        return "#fffe03"
    if v == 5:
        return "#ffcb00"
    if v == 6:
        return "#ff9835"
    if v == 7:
        return "#fd6866"
    if v == 8:
        return "#fe0002"
    if v == 9:
        return "#cc0001"
    if v == 10:
        return "#9a0100"
    return "#640100"


def aqhi_category(v: float) -> str:
    if v <= 3:
        return "Low"
    if v <= 6:
        return "Moderate"
    if v <= 10:
        return "High"
    return "Very High"


def pm25_to_eaqhi(pm25: float) -> Optional[int]:
    pm25 = clean_num(pm25)
    if pm25 is None:
        return None
    if pm25 <= 10:
        return 1
    if pm25 <= 20:
        return 2
    if pm25 <= 30:
        return 3
    if pm25 <= 40:
        return 4
    if pm25 <= 50:
        return 5
    if pm25 <= 60:
        return 6
    if pm25 <= 70:
        return 7
    if pm25 <= 80:
        return 8
    if pm25 <= 90:
        return 9
    if pm25 <= 100:
        return 10
    return 11


def haversine_km(lat1, lon1, lat2, lon2):
    """Vectorized Haversine distance in km."""
    r = 6371.0
    lat1 = np.radians(lat1)
    lon1 = np.radians(lon1)
    lat2 = np.radians(lat2)
    lon2 = np.radians(lon2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def load_joblib_model(paths: Iterable[Path]):
    for path in paths:
        if not path.exists():
            continue
        print(f"Loading RF model: {path}")
        try:
            return joblib.load(path)
        except Exception:
            # Handles manually gzip-compressed joblib files if needed.
            if path.suffix == ".gz":
                with gzip.open(path, "rb") as f:
                    return joblib.load(f)
            raise
    raise FileNotFoundError(f"No RF model found in: {[str(p) for p in paths]}")


def parse_geometry_to_lonlat(geom_value) -> Tuple[Optional[float], Optional[float]]:
    """Accept dict, JSON string, or Python-literal string geometry."""
    if geom_value is None or pd.isna(geom_value):
        return None, None
    geom = geom_value
    if isinstance(geom_value, str):
        try:
            geom = json.loads(geom_value)
        except Exception:
            try:
                geom = ast.literal_eval(geom_value)
            except Exception:
                return None, None
    try:
        coords = geom.get("coordinates")
        return float(coords[0]), float(coords[1])
    except Exception:
        return None, None


# =========================================================
# LOAD CURRENT STATIONS / FEATURES
# =========================================================

def load_current_station_features() -> pd.DataFrame:
    """Load current station features and make them model-ready."""
    if not CURRENT_FEATURES.exists():
        raise FileNotFoundError(
            f"Missing {CURRENT_FEATURES}. Run scripts/build_current.py before this grid script."
        )

    df = pd.read_csv(CURRENT_FEATURES)
    
    if df.empty:
        print(f"{CURRENT_FEATURES} is empty. Falling back to {CURRENT_MAP}.")
    
        if not CURRENT_MAP.exists():
            raise ValueError(
                f"{CURRENT_FEATURES} is empty and {CURRENT_MAP} does not exist."
            )
    
        with open(CURRENT_MAP, "r", encoding="utf-8") as f:
            current_geo = json.load(f)
    
        rows = []
    
        for feat in current_geo.get("features", []):
    
            props = feat.get("properties", {}) or {}
            geom = feat.get("geometry", {}) or {}
            coords = geom.get("coordinates", [None, None])
    
            lon = clean_num(coords[0])
            lat = clean_num(coords[1])
    
            aqhi = clean_num(props.get("AQHI") or props.get("aqhi"))
    
            if lon is None or lat is None or aqhi is None:
                continue
    
            rows.append({
                "station": props.get("station") or props.get("COMMUNITY") or props.get("name"),
                "AQHI": aqhi,
                "PM25": clean_num(props.get("PM25") or props.get("PM2_5")),
                "O3": clean_num(props.get("O3")),
                "NO2": clean_num(props.get("NO2")),
                "WS": clean_num(props.get("WS")),
                "WD": clean_num(props.get("WD")),
                "TEMP": clean_num(props.get("TEMP")),
                "RH": clean_num(props.get("RH")),
                "geometry": geom,
                "lat": lat,
                "lon": lon,
            })
    
        df = pd.DataFrame(rows)
    
        if df.empty:
            raise ValueError(
                f"{CURRENT_MAP} exists but produced no usable station rows."
            )
    
        # Operational fallback: use current AQHI as lag persistence.
        for lag in ["AQHI_lag1", "AQHI_lag2", "AQHI_lag3", "AQHI_lag6", "AQHI_lag12", "AQHI_lag24"]:
            df[lag] = df["AQHI"]
    
        df["AQHI_change_1h"] = 0
        df["AQHI_change_3h"] = 0
    
        now = datetime.now(timezone.utc)
        df["hour"] = now.hour
        df["doy"] = now.timetuple().tm_yday
    
        df["sin_hour"] = np.sin(2 * np.pi * df["hour"] / 24)
        df["cos_hour"] = np.cos(2 * np.pi * df["hour"] / 24)
    
        df["sin_doy"] = np.sin(2 * np.pi * df["doy"] / 365)
        df["cos_doy"] = np.cos(2 * np.pi * df["doy"] / 365)

    
    stations_meta = pd.read_csv(STATIONS_CSV) if STATIONS_CSV.exists() else pd.DataFrame()

    # Normalize station matching fields.
    if not stations_meta.empty:
        stations_meta["station_str"] = stations_meta["station"].astype(str)
        stations_meta["name_lower"] = stations_meta["name"].astype(str).str.lower()
        df["station_str"] = df["station"].astype(str)
        df["station_lower"] = df["station"].astype(str).str.lower()

        # Merge by station id if possible, otherwise by name.
        df = df.merge(
            stations_meta[["station_str", "lat", "lon"]],
            on="station_str",
            how="left",
            suffixes=("", "_meta_id"),
        )

        missing = df["lat"].isna() | df["lon"].isna()
        if missing.any():
            by_name = stations_meta[["name_lower", "lat", "lon"]].rename(
                columns={"lat": "lat_name", "lon": "lon_name"}
            )
            df = df.merge(by_name, left_on="station_lower", right_on="name_lower", how="left")
            df.loc[df["lat"].isna(), "lat"] = df.loc[df["lat"].isna(), "lat_name"]
            df.loc[df["lon"].isna(), "lon"] = df.loc[df["lon"].isna(), "lon_name"]

    # Fallback to geometry column if lat/lon still absent.
    if "lat" not in df.columns or "lon" not in df.columns or df["lat"].isna().any():
        lons, lats = [], []
        for g in df.get("geometry", [None] * len(df)):
            lon, lat = parse_geometry_to_lonlat(g)
            lons.append(lon)
            lats.append(lat)
        if "lon" not in df.columns:
            df["lon"] = lons
        else:
            df["lon"] = df["lon"].fillna(pd.Series(lons))
        if "lat" not in df.columns:
            df["lat"] = lats
        else:
            df["lat"] = df["lat"].fillna(pd.Series(lats))

    # Wind vectors.
    if "U" not in df.columns or "V" not in df.columns:
        wd = pd.to_numeric(df.get("WD"), errors="coerce")
        ws = pd.to_numeric(df.get("WS"), errors="coerce")
        rad = np.deg2rad(wd)
        df["U"] = -ws * np.sin(rad)
        df["V"] = -ws * np.cos(rad)

    # Spatial normalization should match the training script approach.
    # With only current stations, use the current network centre. This is operationally acceptable
    # as long as the station list is stable.
    if "lat_norm" not in df.columns:
        df["lat_norm"] = (df["lat"] - df["lat"].mean()) / df["lat"].std(ddof=0)
    if "lon_norm" not in df.columns:
        df["lon_norm"] = (df["lon"] - df["lon"].mean()) / df["lon"].std(ddof=0)
    if "dist_center" not in df.columns:
        df["dist_center"] = np.sqrt((df["lat"] - df["lat"].mean()) ** 2 + (df["lon"] - df["lon"].mean()) ** 2)

    # Replace zero std NaNs for tiny station sets.
    df[["lat_norm", "lon_norm", "dist_center"]] = df[["lat_norm", "lon_norm", "dist_center"]].replace(
        [np.inf, -np.inf], np.nan
    ).fillna(0)

    # Ensure numeric model columns.
    for c in FEATURE_COLS:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Conservative fallback: fill missing lag/change/pollutant/met columns from current AQHI or medians.
    # This prevents operational failure if one live parameter is missing.
    for c in FEATURE_COLS:
        if df[c].isna().all():
            if c.startswith("AQHI_lag"):
                df[c] = df["AQHI"]
            elif c.startswith("AQHI_change"):
                df[c] = 0
            else:
                df[c] = 0
        else:
            df[c] = df[c].fillna(df[c].median())

    df = df.dropna(subset=["lat", "lon", "AQHI"])
    df["source"] = "station"
    df["weight"] = STATION_WEIGHT
    df["aqhi_current"] = pd.to_numeric(df["AQHI"], errors="coerce")

    return df


# =========================================================
# LOAD PURPLEAIR
# =========================================================

def load_purpleair() -> pd.DataFrame:
    path = PURPLE_LOCAL_1 if PURPLE_LOCAL_1.exists() else PURPLE_LOCAL_2
    if not path.exists():
        print("PurpleAir file not found; continuing with stations only.")
        return pd.DataFrame(columns=["lat", "lon", "aqhi_current", "weight", "source"])

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    records = []
    features = raw.get("features", []) if isinstance(raw, dict) else raw

    now = datetime.now(timezone.utc)

    for feat in features:
        if isinstance(feat, dict) and feat.get("type") == "Feature":
            props = feat.get("properties", {})
            coords = feat.get("geometry", {}).get("coordinates", [None, None])
            lon, lat = coords[0], coords[1]
        else:
            props = feat
            lat = props.get("lat") or props.get("latitude")
            lon = props.get("lon") or props.get("longitude")

        if props.get("use_for_map") is False:
            continue

        last_seen = props.get("last_seen") or props.get("last_modified") or props.get("time_stamp")
        if MAX_PA_AGE_HOURS is not None and last_seen:
            try:
                ts = pd.to_datetime(last_seen, utc=True).to_pydatetime()
                age_h = (now - ts).total_seconds() / 3600
                if age_h > MAX_PA_AGE_HOURS:
                    continue
            except Exception:
                pass

 
        aqhi = clean_num(
            props.get("eAQHI")
            or props.get("eaqhi")
        )
        
        if aqhi is None:
        
            pm_val = clean_num(
                props.get("pm25")
                or props.get("pm_corr")
                or props.get("pm_corrected_clean")
                or props.get("pm_corrected_original")
            )
        
            if pm_val is None:
                continue
        
            aqhi = pm25_to_eaqhi(pm_val)
                

        lat = clean_num(lat)
        lon = clean_num(lon)
        aqhi = clean_num(aqhi)
        if lat is None or lon is None or aqhi is None:
            continue

        records.append(
            {
                "lat": lat,
                "lon": lon,
                "aqhi_current": float(aqhi),
                "weight": PURPLE_WEIGHT,
                "source": "purpleair",
                "name": props.get("name"),
                "sensor_index": props.get("sensor_index"),
            }
        )

    df = pd.DataFrame(records)
    print(f"Loaded PurpleAir sensors for blending: {len(df)}")
    if not df.empty:
        print(df["aqhi_current"].value_counts().sort_index())
    return df


# =========================================================
# LOAD AIRPOINTER (WYAMZ / SESAA)
# =========================================================

def load_airpointer() -> pd.DataFrame:
    cols = ["lat", "lon", "aqhi_current", "weight", "source"]
    if not AIRPOINTER_LOCAL.exists():
        print("AirPointer file not found; continuing without it.")
        return pd.DataFrame(columns=cols)

    with open(AIRPOINTER_LOCAL, "r", encoding="utf-8") as f:
        raw = json.load(f)

    records = []
    skipped_oob = 0
    for feat in raw.get("features", []):
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates", [None, None])
        lon, lat = clean_num(coords[0]), clean_num(coords[1])
        aqhi = clean_num(props.get("AQHI_calc"))

        if lat is None or lon is None or aqhi is None:
            continue
        if not (AIRPOINTER_AQHI_MIN <= aqhi <= AIRPOINTER_AQHI_MAX):
            skipped_oob += 1
            continue

        records.append({
            "lat": lat,
            "lon": lon,
            "aqhi_current": aqhi,
            "weight": AIRPOINTER_WEIGHT,
            "source": "airpointer",
            "name": props.get("station_name"),
        })

    df = pd.DataFrame(records, columns=cols + ["name"])
    print(f"Loaded AirPointer stations for blending: {len(df)} ({skipped_oob} skipped as out-of-bounds AQHI)")
    if not df.empty:
        print(df[["name", "aqhi_current"]])
    return df


# =========================================================
# FORECAST STATIONS
# =========================================================

def forecast_station_aqhi(stations: pd.DataFrame) -> pd.DataFrame:
    model = load_joblib_model(RF_MODEL_PATHS)
    X = stations[FEATURE_COLS].copy()
    preds = model.predict(X)

    out = stations.copy()
    out["aqhi_forecast"] = np.clip(preds.astype(float), 1, 11)
    out["forecast_method"] = "rf_3h"
    return out


# =========================================================
# REGION GEOMETRY / GRID
# =========================================================

def load_sk_polygon():
    if gpd is None or not SK_SHP.exists():
        print("SK shapefile/geopandas unavailable; using bbox only.")
        return None
    sk = gpd.read_file(SK_SHP).to_crs("EPSG:4326")
    return unary_union(sk.geometry)


def make_cell(lon: float, lat: float, step: float) -> Polygon:
    h = step / 2
    return Polygon(
        [
            (lon - h, lat - h),
            (lon + h, lat - h),
            (lon + h, lat + h),
            (lon - h, lat + h),
            (lon - h, lat - h),
        ]
    )


def generate_grid_points(domain: str, step: float):
    if domain == "regina":
        xmin, xmax = REGINA_LON - 1.45, REGINA_LON + 1.45
        ymin, ymax = REGINA_LAT - 0.95, REGINA_LAT + 0.95
    elif domain == "sk":
        xmin, xmax = -110.1, -101.3
        ymin, ymax = 48.9, 60.1
    else:
        raise ValueError(domain)

    xs = np.arange(xmin, xmax + step, step)
    ys = np.arange(ymin, ymax + step, step)
    for lon in xs:
        for lat in ys:
            if domain == "regina":
                d = haversine_km(REGINA_LAT, REGINA_LON, lat, lon)
                if d > REGINA_RADIUS_KM:
                    continue
            yield lon, lat


# =========================================================
# IDW GRID BUILDER
# =========================================================

def idw_value(
    lon,
    lat,
    pts: pd.DataFrame,
    value_col: str,
    min_points: int
) -> Tuple[Optional[float], int, Optional[float]]:
    if pts.empty:
        return None, 0, None, {}

    distances = haversine_km(lat, lon, pts["lat"].values, pts["lon"].values)
    mask = distances <= MAX_IDW_DIST_KM

    if mask.sum() < min_points:
        return None, int(mask.sum()), None, {}

    d = distances[mask]
    v = pts.loc[mask, value_col].astype(float).values
    base_w = pts.loc[mask, "weight"].astype(float).values

    d = np.where(d == 0, 0.001, d)
    w = base_w / (d ** IDW_POWER)


    z = np.sum(w * v) / np.sum(w)
    src = pts.loc[mask, "source"].astype(str).value_counts().to_dict()
    return float(z), int(mask.sum()), float(d.min()), src


def build_geojson_grid(
    points: pd.DataFrame,
    value_col: str,
    domain: str,
    step: float,
    output_path: Path,
    sk_polygon=None,
    product_name: str = "grid",
    min_points: int = 1,
):
    features = []
    
    # Match Alberta approach: subset points to the active grid domain first
    if domain == "regina":
        margin = 0.25
        xmin, xmax = REGINA_LON - 1.45 - margin, REGINA_LON + 1.45 + margin
        ymin, ymax = REGINA_LAT - 0.95 - margin, REGINA_LAT + 0.95 + margin
    
        points = points[
            points["lat"].between(ymin, ymax) &
            points["lon"].between(xmin, xmax)
        ].copy()
    
        print(f"Regina {product_name} interpolation points:")
        print(points["source"].value_counts())
        print(points.groupby("source")[value_col].describe())
    
    elif domain == "sk":
        print(f"SK {product_name} interpolation points:")
        print(points["source"].value_counts())
    
    for lon, lat in generate_grid_points(domain, step):
        cell = make_cell(lon, lat, step)

        if domain == "sk" and sk_polygon is not None:
            if not cell.intersects(sk_polygon):
                continue

        z, n, nearest, src_counts = idw_value(
            lon,
            lat,
            points,
            value_col,
            min_points
        )
        if z is None:
            continue

        if "forecast" in product_name:
            z_round = round(z, 1)
        else:
            z_round = round(z)
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(cell),
                "properties": {
                    "AQHI": min(z_round, 10),
                    "AQHI_raw": z_round,
                    "aqhi_display": "10+" if z_round > 10 else round(z_round),
                    "color": aqhi_color(z_round),
                    "category": aqhi_category(z_round),
                    "n_points": n,
                    "source_counts": src_counts,
                    "nearest_km": round(nearest, 1) if nearest is not None else None,
                    "product": product_name,
                    "domain": domain,
                    "generated_utc": datetime.now(timezone.utc).isoformat(),                    
                },
            }
        )

    output = {"type": "FeatureCollection", "features": features}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, separators=(",", ":"))

    print(f"Saved {output_path}: {len(features)} cells")


# =========================================================
# MAIN
# =========================================================

def main():
    print("=== Building SK blended AQHI grids ===")

    stations = load_current_station_features()
    print(f"Loaded stations: {len(stations)}")

    sensors = load_purpleair()
    airpointer = load_airpointer()

    print("PurpleAir sensors loaded:", len(sensors))
    print("AirPointer stations loaded:", len(airpointer))

    if not sensors.empty:
        print(sensors.head())
        print(sensors["aqhi_current"].describe())

    forecast_stations = forecast_station_aqhi(stations)

    # Current points: observed station AQHI + current PurpleAir eAQHI + AirPointer AQHI.
    current_station_pts = stations[["lat", "lon", "aqhi_current", "weight", "source"]].copy()
    current_sensor_pts = sensors[["lat", "lon", "aqhi_current", "weight", "source"]].copy() if not sensors.empty else sensors
    current_airpointer_pts = airpointer[["lat", "lon", "aqhi_current", "weight", "source"]].copy() if not airpointer.empty else airpointer
    current_pts = pd.concat([current_station_pts, current_sensor_pts, current_airpointer_pts], ignore_index=True)

    print("Current blend source counts:")
    print(current_pts["source"].value_counts())

    print("Current blend AQHI by source:")
    print(current_pts.groupby("source")["aqhi_current"].describe())

    # Forecast points: RF forecast station AQHI + PurpleAir/AirPointer persistence
    # (neither has its own forecast model, so current value stands in).
    forecast_station_pts = forecast_stations[["lat", "lon", "aqhi_forecast", "weight", "source"]].copy()
    forecast_extra_parts = [forecast_station_pts]
    if not sensors.empty:
        forecast_extra_parts.append(
            sensors[["lat", "lon", "aqhi_current", "weight", "source"]].rename(columns={"aqhi_current": "aqhi_forecast"})
        )
    if not airpointer.empty:
        forecast_extra_parts.append(
            airpointer[["lat", "lon", "aqhi_current", "weight", "source"]].rename(columns={"aqhi_current": "aqhi_forecast"})
        )
    forecast_pts = pd.concat(forecast_extra_parts, ignore_index=True) if len(forecast_extra_parts) > 1 else forecast_station_pts

    sk_polygon = load_sk_polygon()

    # Province-wide overlays.
    build_geojson_grid(
        points=current_pts,
        min_points=MIN_POINTS_SK,
        value_col="aqhi_current",
        domain="sk",
        step=SK_GRID_STEP_DEG,
        output_path=OUT_SK_CURRENT,
        sk_polygon=sk_polygon,
        product_name="current_blended_aqhi",
    )

    build_geojson_grid(
        points=forecast_pts,
        min_points=MIN_POINTS_SK,
        value_col="aqhi_forecast",
        domain="sk",
        step=SK_GRID_STEP_DEG,
        output_path=OUT_SK_FORECAST,
        sk_polygon=sk_polygon,
        product_name="forecast_3h_blended_aqhi_rf_plus_pa_persistence",
    )

    # Regina 100 km overlays.
    build_geojson_grid(
        points=current_pts,
        min_points=MIN_POINTS_REGINA,
        value_col="aqhi_current",
        domain="regina",
        step=REGINA_GRID_STEP_DEG,
        output_path=OUT_REGINA_CURRENT,
        product_name="regina_current_blended_aqhi",
    )

    build_geojson_grid(
        points=forecast_pts,
        min_points=MIN_POINTS_REGINA,
        value_col="aqhi_forecast",
        domain="regina",
        step=REGINA_GRID_STEP_DEG,
        output_path=OUT_REGINA_FORECAST,
        product_name="regina_forecast_3h_blended_aqhi_rf_plus_pa_persistence",
    )

    # Save station forecast table for debugging and transparency.
    debug_cols = [
        "station",
        "lat",
        "lon",
        "AQHI",
        "aqhi_forecast",
        "forecast_method",
    ]
    forecast_stations[[c for c in debug_cols if c in forecast_stations.columns]].to_csv(
        DATA_DIR / "station_forecast_3h_debug.csv", index=False
    )

    print("=== Done ===")


if __name__ == "__main__":
    main()
