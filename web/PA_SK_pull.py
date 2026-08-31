# PA_SK_pull.py
# Pull all PurpleAir sensors in Saskatchewan and save as CSV

import os
import requests
import pandas as pd
import geopandas as gpd

from supabase import create_client


# 1) Load Saskatchewan boundary
sk = gpd.read_file("data/SK.shp")

# Make sure it’s in WGS84 (lat/lon) for the API bbox
if sk.crs is None or sk.crs.to_epsg() != 4326:
    sk = sk.to_crs(epsg=4326)

# 2) Get bounding box [minx, miny, maxx, maxy]
minx, miny, maxx, maxy = sk.total_bounds

# 3) Call PurpleAir /v1/sensors endpoint
url = "https://api.purpleair.com/v1/sensors"
api_key = os.getenv("PURPLEAIR_API_KEY")
if not api_key:
    raise RuntimeError("PURPLEAIR_API_KEY environment variable is not set")

headers = {"X-API-Key": api_key}

params = {
    # what fields you want – add more if needed later
    "fields": "sensor_index,name,latitude,longitude,location_type,last_seen",

    # optional: 0 = outside only, 1 = inside only
    "location_type": 0,

    # bounding box (NW and SE corners)
    "nwlng": minx,
    "nwlat": maxy,
    "selng": maxx,
    "selat": miny,
}

resp = requests.get(url, headers=headers, params=params, timeout=30)
resp.raise_for_status()
data = resp.json()

fields = data.get("fields", [])
rows   = data.get("data", [])

if not rows:
    raise RuntimeError("No sensors returned from PurpleAir – check bbox or API key.")

# 4) Convert to DataFrame
df = pd.DataFrame(rows, columns=fields)

required_cols = ["sensor_index", "name", "latitude", "longitude", "location_type", "last_seen"]
missing_cols = [c for c in required_cols if c not in df.columns]
if missing_cols:
    raise RuntimeError(f"PurpleAir response missing expected columns: {missing_cols}")

# force numeric fields
df["sensor_index"] = pd.to_numeric(df["sensor_index"], errors="coerce")
df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
df["location_type"] = pd.to_numeric(df["location_type"], errors="coerce")
df["last_seen"] = pd.to_numeric(df["last_seen"], errors="coerce")

df = df.dropna(subset=["sensor_index", "latitude", "longitude"])
df["sensor_index"] = df["sensor_index"].astype("int64")

# Convert last_seen to datetime and add active flags
df["last_seen_utc"] = pd.to_datetime(df["last_seen"], unit="s", utc=True)

now_utc = pd.Timestamp.now(tz="UTC")
df["age_days"] = (now_utc - df["last_seen_utc"]).dt.total_seconds() / 86400
df["active_7d"] = df["age_days"] <= 7
df["active_30d"] = df["age_days"] <= 30

print(df[["latitude", "longitude"]].head())
print("LAT range:", df["latitude"].min(), df["latitude"].max())
print("LON range:", df["longitude"].min(), df["longitude"].max())


# 5) GeoDataFrame for spatial filtering
gdf = gpd.GeoDataFrame(
    df,
    geometry=gpd.points_from_xy(df.longitude, df.latitude),
    crs="EPSG:4326"
)

print(gdf.crs)
print(sk.crs)



# 6) Clip to Sask polygon
try:
    sk_union = sk.geometry.union_all()
except AttributeError:
    sk_union = sk.unary_union

inside = gdf[gdf.geometry.intersects(sk_union)].copy()
inside["province"] = "SK"

# Drop geometry before CSV/Supabase
inside_no_geom = pd.DataFrame(inside.drop(columns="geometry"))

# Save only recently active sensors for downstream live PurpleAir pulls
inside_live = inside_no_geom[inside_no_geom["active_30d"] == True].copy()
inside_live.to_csv("data/SK_PA_sensors.csv", index=False)

print(f"Total sensors from API: {len(gdf)}")
print(f"Sensors inside Saskatchewan: {len(inside_no_geom)}")
print(f"Active sensors written to CSV: {len(inside_live)}")



# 8) Push sensor metadata into Supabase
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY")
)

payload = inside_no_geom[[
    "sensor_index",
    "name",
    "latitude",
    "longitude",
    "location_type",
    "last_seen",
    "last_seen_utc",
    "age_days",
    "active_7d",
    "active_30d",
    "province"
]].copy()

payload["last_seen_utc"] = payload["last_seen_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
payload["age_days"] = payload["age_days"].round(2)

# Optional: compute network label now or leave it null
def infer_network(name):
    n = name.upper()
    return "OTHER"

payload["network"] = payload["name"].apply(infer_network)

response = supabase.table("purpleair_sensors_meta") \
    .upsert(payload.to_dict("records"), on_conflict="sensor_index") \
    .execute()

print("Supabase response:", response)
print(f"Attempted to upsert {len(payload)} sensors.")


