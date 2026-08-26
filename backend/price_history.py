"""
price_history.py
-----------------
Storage interface for tracked flight prices over time, backed by
Postgres (Railway-managed). Persists across restarts, unlike the
earlier in-memory version -- this is what lets the poller actually
build real history over days/weeks instead of resetting constantly.
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

_DATABASE_URL = os.getenv("DATABASE_URL")


def _get_connection():
    return psycopg2.connect(_DATABASE_URL)


def init_db():
    """Creates the price_history table if it doesn't exist yet. Call once on startup."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id SERIAL PRIMARY KEY,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    timestamp TIMESTAMPTZ NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_route
                ON price_history (origin, destination)
            """)
        conn.commit()
    finally:
        conn.close()


def save_price(origin: str, destination: str, price: int):
    """Records one observed price point for a route, right now."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO price_history (origin, destination, price, timestamp) VALUES (%s, %s, %s, %s)",
                (origin, destination, price, datetime.now()),
            )
        conn.commit()
    finally:
        conn.close()


def get_recent_prices(origin: str, destination: str, limit: int = 30) -> list[dict]:
    """Returns the most recent recorded price points for a route, oldest to newest."""
    conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT origin, destination, price, timestamp
                FROM price_history
                WHERE origin = %s AND destination = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (origin, destination, limit),
            )
            rows = cur.fetchall()
        rows.reverse()  # oldest to newest
        return [
            {
                "origin": r["origin"],
                "destination": r["destination"],
                "price": r["price"],
                "timestamp": r["timestamp"].isoformat(timespec="seconds"),
            }
            for r in rows
        ]
    finally:
        conn.close()


def price_history_size(origin: str, destination: str) -> int:
    """How many price points we've collected for this route so far."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM price_history WHERE origin = %s AND destination = %s",
                (origin, destination),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()