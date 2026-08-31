import requests
import xarray as xr
import numpy as np
import json
from PIL import Image
import matplotlib.colors as mcolors
from pathlib import Path
import urllib3
from datetime import datetime, timedelta


urllib3.disable_warnings()

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# region of interest (Prairies / Saskatchewan focus) - must match the fixed
# smokeBounds used by SK_Air_Map's Leaflet imageOverlay ([[42,-130],[65,-90]])
LAT_MIN = 42
LAT_MAX = 65
LON_MIN = -130
LON_MAX = -90

url = "https://services.firesmoke.ca/forecasts/current/dispersion.nc"
nc_file = DATA_DIR / "firesmoke.nc"

print("Downloading FireSmoke forecast...")
r = requests.get(url, verify=False, timeout=120)
with open(nc_file, "wb") as f:
    f.write(r.content)
print("Saved:", nc_file)

ds = xr.open_dataset(nc_file)
print("VARS:", list(ds.data_vars))

if "PM25" not in ds.data_vars:
    print(f"ERROR: PM25 not found in dataset. Available: {list(ds.data_vars)}")
    exit(1)

tflag = ds["TFLAG"].values
date = int(tflag[0, 0, 0])
time = int(tflag[0, 0, 1])
year = date // 1000
day = date % 1000
hour = time // 10000
minute = (time % 10000) // 100
second = time % 100
smoke_time = datetime(year, 1, 1) + timedelta(days=day - 1)
smoke_time = smoke_time.replace(hour=hour, minute=minute, second=second)
print("FireSmoke timestamp:", smoke_time)

pm = ds["PM25"]
print("PM25 dims:", pm.dims)
print("PM25 shape:", pm.shape)

if "LAY" in pm.dims:
    rows = pm.shape[2]
    cols = pm.shape[3]
else:
    rows = pm.shape[1]
    cols = pm.shape[2]

# approximate geographic bounds of the native model grid
lon_min, lon_max = -145, -85
lat_min, lat_max = 35, 75

lon_step = (lon_max - lon_min) / cols
lat_step = (lat_max - lat_min) / rows

lat_vals = lat_min + np.arange(rows) * lat_step
lon_vals = lon_min + np.arange(cols) * lon_step

region_mask = (
    (lat_vals[:, None] >= LAT_MIN) &
    (lat_vals[:, None] <= LAT_MAX) &
    (lon_vals[None, :] >= LON_MIN) &
    (lon_vals[None, :] <= LON_MAX)
)

# Fixed crop window - identical rows/cols on every run, matching
# SK_Air_Map's fixed smokeBounds regardless of where any smoke happens to
# be this run. Auto-cropping to wherever smoke is (as the source AB_datapull
# version does) would silently misalign the image against the map's fixed
# overlay bounds whenever the plume shifts or is absent.
region_rows = np.where(region_mask.any(axis=1))[0]
region_cols = np.where(region_mask.any(axis=0))[0]
rmin, rmax = region_rows[0], region_rows[-1]
cmin, cmax = region_cols[0], region_cols[-1]

crop_lat_min = lat_vals[rmin]
crop_lat_max = lat_vals[rmax] + lat_step
crop_lon_min = lon_vals[cmin]
crop_lon_max = lon_vals[cmax] + lon_step
print(f"Fixed crop bounds: {crop_lat_min:.4f},{crop_lon_min:.4f} -> {crop_lat_max:.4f},{crop_lon_max:.4f}")

colors = [
    (210 / 255, 255 / 255, 210 / 255, 0.70),
    (180 / 255, 255 / 255, 180 / 255, 0.78),
    (255 / 255, 255 / 255, 120 / 255, 0.84),
    (255 / 255, 200 / 255, 80 / 255, 0.88),
    (255 / 255, 120 / 255, 60 / 255, 0.92),
    (220 / 255, 60 / 255, 40 / 255, 0.96),
    (160 / 255, 0, 0, 1.00),
]
cmap = mcolors.LinearSegmentedColormap.from_list("smoke", colors)
# Expands the colour range for low concentrations while retaining
# differentiation at higher smoke concentrations
norm = mcolors.PowerNorm(gamma=0.30, vmin=0.1, vmax=80)

forecast_hours = {"now": 0, "6h": 6, "12h": 12, "24h": 24}

for name, t in forecast_hours.items():
    forecast_time = smoke_time + timedelta(hours=t)
    print("Processing:", name, forecast_time)

    if t >= pm.shape[0]:
        print(f"Skipping {name} — not available in forecast range")
        continue

    if "LAY" in pm.dims:
        grid = pm.isel(TSTEP=t, LAY=0).values.copy()
    else:
        grid = pm.isel(TSTEP=t).values.copy()

    grid[grid < 0.1] = np.nan
    print(f"Grid min: {np.nanmin(grid)}, max: {np.nanmax(grid)}")

    # geojson output (not currently used by SK_Air_Map, kept for other consumers)
    valid_mask = (~np.isnan(grid)) & region_mask
    valid_rc = np.argwhere(valid_mask)
    features = []
    for r_idx, c_idx in valid_rc:
        raw_val = float(grid[r_idx, c_idx])
        lat = lat_vals[r_idx]
        lon = lon_vals[c_idx]
        poly = [
            [lon, lat],
            [lon + lon_step, lat],
            [lon + lon_step, lat + lat_step],
            [lon, lat + lat_step],
            [lon, lat],
        ]
        features.append({
            "type": "Feature",
            "properties": {"pm25": raw_val, "forecast": name, "timestamp": forecast_time.isoformat()},
            "geometry": {"type": "Polygon", "coordinates": [poly]},
        })
    geojson = {"type": "FeatureCollection", "features": features}
    outfile = DATA_DIR / f"firesmoke_{name}.geojson"
    with open(outfile, "w") as f:
        json.dump(geojson, f)
    print("Saved:", outfile, "features:", len(features))

    # PNG image overlay, cropped to the fixed region above so every frame
    # aligns with SK_Air_Map's constant smokeBounds - written even when
    # there's no smoke at all, so the overlay correctly goes blank rather
    # than silently keeping a stale image from a previous run.
    cropped = grid[rmin:rmax + 1, cmin:cmax + 1]
    masked = np.where(region_mask[rmin:rmax + 1, cmin:cmax + 1], cropped, np.nan)

    alpha_mask = ~np.isnan(masked)
    safe_grid = np.nan_to_num(masked, nan=0.0)
    rgba = cmap(norm(safe_grid))
    rgba[..., 3] = np.where(alpha_mask, rgba[..., 3], 0)

    img = (rgba * 255).astype(np.uint8)
    img = np.flipud(img)
    image = Image.fromarray(img, mode="RGBA")

    UPSCALE = 3
    image = image.resize((image.width * UPSCALE, image.height * UPSCALE), resample=Image.BICUBIC)

    png_out = DATA_DIR / f"firesmoke_{name}.png"
    image.save(png_out)
    print("Saved PNG:", png_out)
