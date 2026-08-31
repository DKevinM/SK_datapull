import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
import joblib
import os
os.makedirs("models", exist_ok=True)

from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    r2_score
)

# ---------------------------
# Load dataset
# ---------------------------
data = pd.read_csv("data/training_dataset.csv.gz")
print("Rows:", len(data))
print("Columns:", data.columns)

data = data.dropna()



# ---------------------------
# Feature columns
# ---------------------------
feature_cols = [

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
    "dist_center"
]

X = data[feature_cols]


# ---------------------------
# Train function
# ---------------------------
def train_model(target, name):

    y = data[target]

    split_index = int(len(X) * 0.8)

    X_train = X.iloc[:split_index]
    X_test  = X.iloc[split_index:]

    y_train = y.iloc[:split_index]
    y_test  = y.iloc[split_index:]

    model = RandomForestRegressor(
        n_estimators=150,
        max_depth=10,
        min_samples_leaf=10,
        max_features="sqrt",
        oob_score=True,
        n_jobs=-1,
        random_state=42
    )

    model.fit(X_train, y_train)

    pred = model.predict(X_test)

    score = model.score(X_test, y_test)

    print(f"{name} model R²:", score)
    print("OOB:", model.oob_score_)

    joblib.dump(model, f"models/{name}_model.pkl")

    importance = pd.Series(
        model.feature_importances_,
        index=feature_cols
    ).sort_values(ascending=False)

    importance.to_csv(f"models/{name}_importance.csv")

    print(f"\nTop features for {name}:")
    print(importance.head(10))

    rmse = np.sqrt(mean_squared_error(y_test, pred))
    mae = mean_absolute_error(y_test, pred)
    r2 = r2_score(y_test, pred)

    print(f"\n{name}")
    print("RMSE:", rmse)
    print("MAE:", mae)
    print("R²:", r2)

    with open(f"models/{name}_metrics.txt", "w") as f:

        f.write(f"Model: {name}\n")
        f.write(f"RMSE: {rmse}\n")
        f.write(f"MAE: {mae}\n")
        f.write(f"R2: {r2}\n")
        f.write(f"OOB: {model.oob_score_}\n\n")

        f.write("Top Features:\n")
        f.write(str(importance.head(15)))


# ---------------------------
# Train models
# ---------------------------
train_model("AQHI_future_1h", "aqhi_1h")
train_model("AQHI_future_2h", "aqhi_2h")
train_model("AQHI_future_3h", "aqhi_3h")
train_model("AQHI_future_6h", "aqhi_6h")





