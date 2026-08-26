"""
poller.py
----------
Background price-tracking script. Runs continuously, checking a fixed
list of routes (and now both Economy and Business class) on a timer,
independent of any user activity. Saves to Postgres (via price_history.py)
so history survives restarts.
"""
import asyncio
from datetime import datetime

from flights import search_direct_flight
from price_history import save_price, init_db

TRACKED_ROUTES = [
    ("DEL", "YYZ"),
    ("BOM", "YVR"),
    ("DEL", "YUL"),
    ("BOM", "YYZ"),
]

TRACKED_CLASSES = ["economy", "business"]

POLL_INTERVAL_SECONDS = 6 * 60 * 60
DAYS_AHEAD = 60


def get_target_date() -> str:
    from datetime import timedelta
    target = datetime.now() + timedelta(days=DAYS_AHEAD)
    return target.strftime("%Y-%m-%d")


async def poll_once():
    """One full pass: check every tracked route x class combo, save whatever comes back."""
    travel_date = get_target_date()
    print(f"[poller] Starting poll cycle for {travel_date} at {datetime.now().isoformat(timespec='seconds')}")

    for origin, destination in TRACKED_ROUTES:
        for seat_class in TRACKED_CLASSES:
            try:
                result = await asyncio.to_thread(search_direct_flight, origin, destination, travel_date, seat_class)

                if result:
                    save_price(origin, destination, result["price"], seat_class)
                    print(f"[poller] {origin}->{destination} ({seat_class}) on {travel_date}: {result['price']} (saved)")
                else:
                    print(f"[poller] {origin}->{destination} ({seat_class}) on {travel_date}: no results")

            except Exception as e:
                print(f"[poller] {origin}->{destination} ({seat_class}) failed: {e}")

            await asyncio.sleep(5)

    print(f"[poller] Poll cycle complete.")


async def run_forever():
    """Main loop: poll, sleep, repeat, forever."""
    init_db()
    print(f"[poller] Starting background price poller.")
    print(f"[poller] Tracking {len(TRACKED_ROUTES)} routes x {len(TRACKED_CLASSES)} classes, checking every {POLL_INTERVAL_SECONDS // 3600} hours.")

    while True:
        await poll_once()
        print(f"[poller] Sleeping for {POLL_INTERVAL_SECONDS // 3600} hours until next cycle...")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_forever())