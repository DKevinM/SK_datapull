library(Cubist)

dir.create("models", showWarnings = FALSE)

# -----------------------------------
# Load dataset
# -----------------------------------
data <- read.csv(
  gzfile("data/training_dataset.csv.gz")
)

data <- na.omit(data)

cat("Rows:", nrow(data), "\n")
cat("Columns:\n")
print(names(data))

# -----------------------------------
# Feature columns
# -----------------------------------
feature_cols <- c(

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
)

# -----------------------------------
# Train function
# -----------------------------------
train_model <- function(target, name){

  y <- data[[target]]

  X <- data[, feature_cols]

  split_index <- floor(nrow(X) * 0.8)

  X_train <- X[1:split_index, ]
  X_test  <- X[(split_index+1):nrow(X), ]

  y_train <- y[1:split_index]
  y_test  <- y[(split_index+1):length(y)]

  model <- cubist(
    x = X_train,
    y = y_train,
    committees = 25,
    neighbors = 5
  )

  pred <- predict(model, X_test)

  rmse <- sqrt(mean((pred - y_test)^2))
  mae <- mean(abs(pred - y_test))
  r2 <- cor(
    pred,
    y_test,
    use = "complete.obs"
  )^2

  cat("\n====================\n")
  cat(name, "\n")
  cat("====================\n")
  cat("RMSE:", rmse, "\n")
  cat("MAE :", mae, "\n")
  cat("R²  :", r2, "\n")


  writeLines(
    c(
      paste("Model:", name),
      paste("RMSE:", rmse),
      paste("MAE:", mae),
      paste("R2:", r2)
    ),
    paste0("models/", name, "_cubist_metrics.txt")
  )
  
  capture.output(
    summary(model),
    file = paste0("models/", name, "_cubist_summary.txt")
  )  

  saveRDS(
    model,
    paste0("models/", name, "_cubist.rds")
  )
}

# -----------------------------------
# Train models
# -----------------------------------
train_model("AQHI_future_1h", "aqhi_1h")
train_model("AQHI_future_2h", "aqhi_2h")
train_model("AQHI_future_3h", "aqhi_3h")
train_model("AQHI_future_6h", "aqhi_6h")
