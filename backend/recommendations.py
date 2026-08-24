"""
recommendations.py
-------------------
Uses the trained quantile regression models (forecast_model_q10/q50/q90.pkl)
to predict a price RANGE for a route at a given days_left value, and to
compare a real observed price against that range for a buy-now-vs-wait
signal.

IMPORTANT CAVEAT: these models are trained on Indian DOMESTIC flight data
(Kaggle, Shubham Bathwal dataset) -- there is no public dataset for
international routes like India-Canada. This validates the forecasting
METHODOLOGY on real data. Predictions for international routes should be
treated as a rough approximation of general booking-window behavior, not
a precise forecast for that specific route -- until retrained on our own
scraped India-Canada price history (see poller.py / price_history.py).
"""
import joblib
import numpy as np
import os

_MODEL_DIR = os.path.dirname(os.path.abspath(__file__))

_model_q10 = None
_model_q50 = None
_model_q90 = None
_encoder = None
_feature_config = None


def _load_models():
    """Lazy-load models once, on first use, not at import time."""
    global _model_q10, _model_q50, _model_q90, _encoder, _feature_config
    if _model_q10 is None:
        _model_q10 = joblib.load(os.path.join(_MODEL_DIR, "forecast_model_q10.pkl"))
        _model_q50 = joblib.load(os.path.join(_MODEL_DIR, "forecast_model_q50.pkl"))
        _model_q90 = joblib.load(os.path.join(_MODEL_DIR, "forecast_model_q90.pkl"))
        _encoder = joblib.load(os.path.join(_MODEL_DIR, "forecast_encoder.pkl"))
        _feature_config = joblib.load(os.path.join(_MODEL_DIR, "forecast_feature_config.pkl"))


def predict_price_range(
    airline: str,
    source_city: str,
    departure_time: str,
    stops: str,
    arrival_time: str,
    destination_city: str,
    travel_class: str,
    duration: float,
    days_left: int,
):
    """
    Predicts a price range (10th, 50th/median, 90th percentile) for a
    flight matching these features. All string args should match the
    categories the model was trained on (Indian domestic airlines/cities) --
    for international routes, pass the closest reasonable domestic analog
    (e.g. class="Economy") since exact city/airline matches won't exist.

    Returns dict: {low, median, high}
    """
    _load_models()

    categorical_input = [[airline, source_city, departure_time, stops, arrival_time, destination_city, travel_class]]
    encoded = _encoder.transform(categorical_input)
    numeric_input = np.array([[duration, days_left]])
    X = np.hstack([encoded, numeric_input])

    low = _model_q10.predict(X)[0]
    median = _model_q50.predict(X)[0]
    high = _model_q90.predict(X)[0]

    return {
        "low": round(float(low)),
        "median": round(float(median)),
        "high": round(float(high)),
    }


def predict_price_trend(
    airline: str,
    source_city: str,
    departure_time: str,
    stops: str,
    arrival_time: str,
    destination_city: str,
    travel_class: str,
    duration: float,
    days_left_points: list = None,
):
    """
    Predicts the median price at several different days_left values, so
    the caller can see how price is expected to move as the booking
    window shrinks -- e.g. "book at 60 days vs 30 days vs 10 days."

    Returns a list of {days_left, predicted_price}, ordered furthest-out
    to closest-to-departure.
    """
    if days_left_points is None:
        days_left_points = [60, 45, 30, 21, 14, 7, 3, 1]

    _load_models()

    trend = []
    for d in days_left_points:
        result = predict_price_range(
            airline, source_city, departure_time, stops,
            arrival_time, destination_city, travel_class, duration, d
        )
        trend.append({"days_left": d, "predicted_price": result["median"]})

    return trend


def score_buy_now_vs_wait(current_price: int, predicted_range: dict) -> dict:
    """
    Compares a real observed price against the model's predicted range
    for the same days_left, and gives a plain recommendation.
    """
    low, median, high = predicted_range["low"], predicted_range["median"], predicted_range["high"]

    if current_price <= low:
        recommendation = "book_now"
        reason = f"Current price ({current_price}) is at or below the typical low end ({low}) for this booking window -- a strong time to book."
    elif current_price <= median:
        recommendation = "book_now"
        reason = f"Current price ({current_price}) is below the typical median ({median}) for this booking window -- a good time to book."
    elif current_price <= high:
        recommendation = "borderline"
        reason = f"Current price ({current_price}) is above the typical median ({median}) but within the normal range -- not unusual, but not a great deal either."
    else:
        recommendation = "wait"
        reason = f"Current price ({current_price}) is above the typical high end ({high}) for this booking window -- may be worth waiting or checking alternate dates/routes."

    return {
        "recommendation": recommendation,
        "reason": reason,
        "predicted_range": predicted_range,
    }