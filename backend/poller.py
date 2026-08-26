import asyncio
from datetime import datetime, timedelta

from flights import search_direct_flight, search_prices_by_airline
from price_history import save_price, init_db
from notifications import (
    init_notifications_db,
    get_due_trackers,
    get_trackers_ready_to_close,
    has_checked_today,
    save_tracker_day,
    send_tracker_report,
    close_tracker,
)

# poller.py
#
# This is the one process in the whole app that runs completely on its
# own, with nobody watching -- it wakes up, checks prices, saves them,
# goes back to sleep, and repeats forever. It's what actually makes
# Stufly's "should you book now" feature honest: without something
# checking prices in the background regardless of whether anyone's using
# the site right now, we'd have no real history to compare today's price
# against.
#
# Deployed as its own separate Railway service, deliberately not folded
# into the main API -- if this got stuck or crashed, we don't want that
# taking the actual website down with it, and vice versa.

TRACKED_ROUTES = [
    ("DEL", "YYZ"),
    ("BOM", "YVR"),
    ("DEL", "YUL"),
    ("BOM", "YYZ"),
]

TRACKED_CLASSES = ["economy", "business"]

# Every 6 hours, not more often. We tried 3 hours first, but once we added
# both economy and business to the routine, that would have meant burning
# through our SearchApi quota within a couple of days -- 6 hours still
# catches same-day price swings without paying for checks nobody needs
# that granularly.
POLL_INTERVAL_SECONDS = 6 * 60 * 60

# We check prices 60 days out from today rather than, say, tomorrow --
# that keeps every poll comparing apples to apples. If we checked
# "today's date" on every run, the booking window would keep shrinking
# with each poll, and we'd never be able to tell whether a price change
# was real or just an artifact of asking a different question each time.
DAYS_AHEAD = 60


def get_target_date() -> str:
    target = datetime.now() + timedelta(days=DAYS_AHEAD)
    return target.strftime("%Y-%m-%d")


async def poll_routes_once():
    """
    One full pass over the fixed list of routes we watch continuously, in
    both cabin classes. This is the part that builds up Stufly's own
    price history over time, independent of the tracker feature below.
    """
    travel_date = get_target_date()
    print(f"[poller] Starting route poll cycle for {travel_date} at {datetime.now().isoformat(timespec='seconds')}")

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

    print("[poller] Route poll cycle complete.")


async def check_trackers_once():
    """
    Separate from the fixed-route polling above -- this handles the
    manual trackers people have actually set up through /tracker/create.

    Two different things happen here, on every run:
    1. Any tracker that's still inside its window and hasn't been checked
       yet today gets a fresh price pull for its specific airlines.
    2. Any tracker whose window has fully finished gets its report built
       and emailed, then gets closed so it doesn't send twice.

    Doing both in the same pass, rather than two separate loops on
    different schedules, keeps this simple -- one function, one clear job
    every time the poller wakes up.
    """
    due = get_due_trackers()
    if due:
        print(f"[poller] {len(due)} tracker(s) due for a check today")

    for tracker in due:
        if has_checked_today(tracker["id"]):
            continue

        airlines = tracker["airlines"].split(",")
        travel_date = get_target_date()

        try:
            prices = await asyncio.to_thread(
                search_prices_by_airline, tracker["origin"], tracker["destination"], travel_date, airlines, tracker["seat_class"]
            )
            save_tracker_day(tracker["id"], prices)
            print(f"[poller] tracker {tracker['id']} ({tracker['origin']}->{tracker['destination']}): {prices}")
        except Exception as e:
            print(f"[poller] tracker {tracker['id']} check failed: {e}")

    ready = get_trackers_ready_to_close()
    for tracker in ready:
        print(f"[poller] tracker {tracker['id']} window finished -- sending report")
        sent = send_tracker_report(tracker)
        if sent:
            close_tracker(tracker["id"])


async def run_forever():
    """
    The main loop: check the fixed routes, check any active trackers,
    sleep, repeat -- forever, until the process is stopped or redeployed.
    """
    init_db()
    init_notifications_db()
    print("[poller] Starting background price poller.")
    print(f"[poller] Tracking {len(TRACKED_ROUTES)} routes x {len(TRACKED_CLASSES)} classes, checking every {POLL_INTERVAL_SECONDS // 3600} hours.")

    while True:
        await poll_routes_once()
        await check_trackers_once()
        print(f"[poller] Sleeping for {POLL_INTERVAL_SECONDS // 3600} hours until next cycle...")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_forever())