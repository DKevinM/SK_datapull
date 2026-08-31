import pandas as pd
import numpy as np
import glob
from pathlib import Path

DATA_DIR = Path("SK_csv")

files = glob.glob(str(DATA_DIR / "*.csv"))

print("Files found:")
print(files)


dfs = []

for f in files:

    df = pd.read_csv(f, sep=None, engine="python")

    print(df.columns)
    
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.replace(-9999, np.nan, inplace=True)
    dfs.append(df)


# ----------------------------------
# Load station metadata
# ----------------------------------
stations = pd.read_csv("data/stations.csv")


# ----------------------------
# combine stations
# ----------------------------
data = pd.concat(dfs)


# ----------------------------------
# Merge station coordinates
# ----------------------------------
data = data.merge(stations, on="station", how="left")

data["station"] = data["station"].astype(int)

data = data.sort_values(["station","datetime"])

print(data["station"].unique())
print(data[["station","name"]].drop_duplicates())

# ------------------------------------
# Fill small gaps (max 3 hours)
# ------------------------------------
cols = ["PM25","NO2","O3","WS","WD","TEMP","RH"]
for c in cols:
    data[c] = data.groupby("station")[c].transform(
        lambda x: x.interpolate(limit=3, limit_direction="both")
    )



# ------------------------------------------------
# WIND VECTOR FEATURES
# ------------------------------------------------
rad = np.deg2rad(data["WD"])

data["U"] = -data["WS"] * np.sin(rad)
data["V"] = -data["WS"] * np.cos(rad)


# ----------------------------------
# Wind transport proxy
# ----------------------------------
# difference from network centre
data["dlat"] = data["lat"] - data["lat"].mean()
data["dlon"] = data["lon"] - data["lon"].mean()

# transport index
data["transport_index"] = data["U"] * data["dlat"] + data["V"] * data["dlon"]


# ------------------------------------------------
# Normalize lat and lon
# ------------------------------------------------
data["lat_norm"] = (data["lat"] - data["lat"].mean()) / data["lat"].std()
data["lon_norm"] = (data["lon"] - data["lon"].mean()) / data["lon"].std()

data["dist_center"] = np.sqrt(
    (data["lat"] - data["lat"].mean())**2 +
    (data["lon"] - data["lon"].mean())**2
)



# ------------------------------------------------
# TIME FEATURES (seasonality)
# ------------------------------------------------
# day of year (seasonality)
data["doy"] = data["datetime"].dt.dayofyear
data["sin_doy"] = np.sin(2*np.pi*data["doy"]/365)
data["cos_doy"] = np.cos(2*np.pi*data["doy"]/365)

# hour of day (diurnal cycle)
data["hour"] = data["datetime"].dt.hour
data["sin_hour"] = np.sin(2*np.pi*data["hour"]/24)
data["cos_hour"] = np.cos(2*np.pi*data["hour"]/24)


# ----------------------------
# sort data before lagging
# ----------------------------
data = data.sort_values(["station", "datetime"])



# ------------------------------------------------
# 3-hour rolling averages
# ------------------------------------------------
data = data.set_index("datetime")

data["PM25_3hr"] = (
    data.groupby("station")["PM25"]
    .rolling("3h", min_periods=2)
    .mean()
    .reset_index(level=0, drop=True)
)

data["NO2_3hr"] = (
    data.groupby("station")["NO2"]
    .rolling("3h", min_periods=2)
    .mean()
    .reset_index(level=0, drop=True)
)

data["O3_3hr"] = (
    data.groupby("station")["O3"]
    .rolling("3h", min_periods=2)
    .mean()
    .reset_index(level=0, drop=True)
)



# ------------------------------------------------
# AQHI calculation
# ------------------------------------------------
data["AQHI"] = (
    (1000 / 10.4) *
    (
        (np.exp(0.000537 * data["O3_3hr"]) - 1) +
        (np.exp(0.000871 * data["NO2_3hr"]) - 1) +
        (np.exp(0.000487 * data["PM25_3hr"]) - 1)
    )
)

data["AQHI"] = data["AQHI"].clip(lower=1, upper=11)
data["AQHI"] = data["AQHI"].round(1)


for h in [1,2,3,6]:

    data[f"AQHI_future_{h}h"] = (
        data.groupby("station")["AQHI"]
        .shift(-h)
    )


for lag in [1,2,3,6,12,24]:

    data[f"AQHI_lag{lag}"] = (
        data.groupby("station")["AQHI"]
        .shift(lag)
    )


data["AQHI_change_1h"] = (
    data["AQHI"] - data["AQHI_lag1"]
)

data["AQHI_change_3h"] = (
    data["AQHI"] - data["AQHI_lag3"]
)




data = data.reset_index()

# ------------------------------------------------
# Remove incomplete rows
# ------------------------------------------------
data = data.dropna()


# ------------------------------------------------
# Save dataset
# ------------------------------------------------
data.to_csv(
    "data/training_dataset.csv.gz",
    index=False,
    compression="gzip"
)
print("Training dataset built")


