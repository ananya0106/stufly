"""
price_history.py
-----------------
Storage interface for tracked flight prices over time.

TEMPORARY: uses an in-memory list right now. Swap this class's
internals for real Postgres queries once Railway's database is set up.
Nothing outside this file needs to change when that swap happens --
everything else calls save_price() / get_recent_prices() only.
"""
from datetime import datetime
from typing import Optional

_price_log: list[dict] = []


def save_price(origin: str, destination: str, price: int):
    """Records one observed price point for a route, right now."""
    _price_log.append({
        "origin": origin,
        "destination": destination,
        "price": price,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    })


def get_recent_prices(origin: str, destination: str, limit: int = 30) -> list[dict]:
    """Returns the most recent recorded price points for a route."""
    matches = [
        p for p in _price_log
        if p["origin"] == origin and p["destination"] == destination
    ]
    return matches[-limit:]


def price_history_size(origin: str, destination: str) -> int:
    """How many price points we've collected for this route so far."""
    return len(get_recent_prices(origin, destination, limit=10_000))