import pandas as pd
from sklearn.linear_model import LinearRegression


def predict_milk(df):

    if df.empty or len(df) < 5:
        return None

    df = df.sort_values("date")

    df["day_index"] = range(len(df))

    X = df[["day_index"]]
    y = df["quantity"]

    model = LinearRegression()
    model.fit(X, y)

    next_day = [[len(df)]]

    prediction = model.predict(next_day)[0]

    if prediction < 0:
        prediction = 0

    return round(prediction, 2)