import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, date, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from flights import search_prices_by_airline
from discounts import get_discounts_for_airline

# notifications.py
#
# This is the "manual tracker" feature: instead of just searching once,
# a student can ask us to watch a route on a few specific airlines for a
# handful of days, and we'll email them a proper comparison once that
# window's up -- basically doing the tedious job of checking prices every
# day yourself, which is what a lot of people already do by hand in a
# spreadsheet.
#
# Two tables live in the same Postgres database as price_history:
#   price_trackers        -- one row per subscription someone set up
#   tracker_daily_prices   -- one row per (tracker, day, airline) price we recorded
#
# The actual "check today's prices and see if any tracker is due" logic
# lives in poller.py, since that's the process that's already running
# continuously in the background -- this file just provides the building
# blocks it calls.

_DATABASE_URL = os.getenv("DATABASE_URL")
_GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
_GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# Colours pulled straight from the frontend's palette, so a tracker report
# and the website itself feel like the same product rather than a plain
# spreadsheet bolted on the side.
NAVY = "14213D"
GOLD = "C98F1F"
CREAM = "FDF8F0"
CREAM_DIM = "F3ECDC"
SAGE = "D9F0E6"
WHITE = "FFFFFF"


def _get_connection():
    return psycopg2.connect(_DATABASE_URL)


def init_notifications_db():
    """
    Creates both tracker tables if they don't already exist. Safe to call
    every time the app starts -- CREATE TABLE IF NOT EXISTS just does
    nothing on the second and every later run, so there's no harm in
    calling this on every boot rather than trying to remember whether it's
    already been done.
    """
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_trackers (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    seat_class TEXT NOT NULL DEFAULT 'economy',
                    airlines TEXT NOT NULL,
                    start_date DATE NOT NULL,
                    duration_days INTEGER NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tracker_daily_prices (
                    id SERIAL PRIMARY KEY,
                    tracker_id INTEGER NOT NULL REFERENCES price_trackers(id),
                    checked_date DATE NOT NULL,
                    airline TEXT NOT NULL,
                    price INTEGER,
                    UNIQUE (tracker_id, checked_date, airline)
                )
            """)
        conn.commit()
    finally:
        conn.close()


def create_tracker(email: str, origin: str, destination: str, airlines: list[str], duration_days: int, seat_class: str = "economy") -> int:
    """
    Registers a new tracker. airlines gets stored as a comma-joined string
    rather than its own table, mainly because there are only ever 2-3 of
    them per tracker -- a full join table would be over-engineering for
    something this small.

    Returns the new tracker's id, so the endpoint that calls this can hand
    it back to the person as a confirmation ("your tracker id is 42").
    """
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO price_trackers (email, origin, destination, seat_class, airlines, start_date, duration_days, active, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                RETURNING id
                """,
                (email, origin, destination, seat_class, ",".join(airlines), date.today(), duration_days, datetime.now()),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_due_trackers() -> list[dict]:
    """
    A tracker is "due" for a check today if it's still active and today's
    date falls somewhere inside its tracking window. We check whether
    today's row already exists for this tracker further down (in
    check_trackers_today), rather than here -- this function's only job is
    finding which trackers are relevant today at all.
    """
    conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, email, origin, destination, seat_class, airlines, start_date, duration_days
                FROM price_trackers
                WHERE active = TRUE
                  AND CURRENT_DATE >= start_date
                  AND CURRENT_DATE < start_date + duration_days
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_trackers_ready_to_close() -> list[dict]:
    """
    Once a tracker's window has fully passed, it's time to send the final
    report and mark it done. Kept separate from get_due_trackers() above
    because "still collecting data" and "time to send the report" are
    genuinely different moments in a tracker's life, even though they're
    both just date comparisons against the same window.
    """
    conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, email, origin, destination, seat_class, airlines, start_date, duration_days
                FROM price_trackers
                WHERE active = TRUE
                  AND CURRENT_DATE >= start_date + duration_days
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def has_checked_today(tracker_id: int) -> bool:
    """
    The poller runs every 6 hours, but a tracker only needs one price
    point per day -- without this check we'd end up with 4 rows for the
    same day instead of 1, which would throw off the "cheapest day" logic
    later. This is what stops that from happening.
    """
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM tracker_daily_prices WHERE tracker_id = %s AND checked_date = CURRENT_DATE",
                (tracker_id,),
            )
            return cur.fetchone()[0] > 0
    finally:
        conn.close()


def save_tracker_day(tracker_id: int, prices_by_airline: dict):
    """
    Saves one day's worth of prices for a tracker -- one row per airline.
    ON CONFLICT DO NOTHING means if this somehow got called twice for the
    same tracker/day/airline, the second call just silently does nothing
    rather than erroring out or overwriting real data with a possibly
    worse second reading.
    """
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            for airline, price in prices_by_airline.items():
                cur.execute(
                    """
                    INSERT INTO tracker_daily_prices (tracker_id, checked_date, airline, price)
                    VALUES (%s, CURRENT_DATE, %s, %s)
                    ON CONFLICT (tracker_id, checked_date, airline) DO NOTHING
                    """,
                    (tracker_id, airline, price),
                )
        conn.commit()
    finally:
        conn.close()


def get_tracker_history(tracker_id: int) -> list[dict]:
    """Every price point recorded for a tracker, oldest day first -- this is
    exactly the data the Excel report gets built from."""
    conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT checked_date, airline, price FROM tracker_daily_prices WHERE tracker_id = %s ORDER BY checked_date ASC",
                (tracker_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def close_tracker(tracker_id: int):
    """Marks a tracker inactive once its report's been sent, so it doesn't
    get picked up again by get_due_trackers() or get_trackers_ready_to_close()."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE price_trackers SET active = FALSE WHERE id = %s", (tracker_id,))
        conn.commit()
    finally:
        conn.close()


def _build_report_excel(tracker: dict, history: list[dict], filepath: str):
    """
    Builds the actual .xlsx report -- two sheets, styled to match Stufly's
    own colours so this doesn't feel like a generic spreadsheet that
    happens to have flight numbers in it.

    This whole function is really just following the same layout we
    mocked up and approved by hand earlier: a Summary sheet with the
    headline numbers, and a Daily Prices sheet with the full day-by-day
    breakdown, a student-discount column, and a couple of booking links
    at the bottom.
    """
    airlines = tracker["airlines"].split(",")
    origin, destination = tracker["origin"], tracker["destination"]

    # Reshape the flat list of (date, airline, price) rows into a
    # date -> {airline: price} lookup, since that's a much easier shape
    # to build a day-by-day table from than the raw rows.
    by_date = {}
    for row in history:
        d = row["checked_date"]
        by_date.setdefault(d, {})[row["airline"]] = row["price"]
    sorted_dates = sorted(by_date.keys())

    # Work out the overall cheapest price/airline/day across the whole
    # window -- this is the headline number the Summary sheet leads with.
    best_price, best_airline, best_date = None, None, None
    for d in sorted_dates:
        for airline, price in by_date[d].items():
            if price is not None and (best_price is None or price < best_price):
                best_price, best_airline, best_date = price, airline, d

    wb = openpyxl.Workbook()

    # ---- Summary sheet ----
    ws1 = wb.active
    ws1.title = "Summary"
    ws1.sheet_properties.tabColor = NAVY

    for row in ws1.iter_rows(min_row=1, max_row=20, min_col=1, max_col=6):
        for cell in row:
            cell.fill = PatternFill("solid", fgColor=CREAM)

    ws1["B2"] = "Stufly Price Tracker Report"
    ws1["B2"].font = Font(name="Arial", size=16, bold=True, color=NAVY)
    ws1["B3"] = f"{origin} -> {destination}  |  Tracked {sorted_dates[0].strftime('%d %b %Y') if sorted_dates else '-'} - {sorted_dates[-1].strftime('%d %b %Y') if sorted_dates else '-'}"
    ws1["B3"].font = Font(name="Arial", size=11, color="7A7362")

    thin = Side(style="thin", color="D8D2C2")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    label_fill = PatternFill("solid", fgColor=CREAM_DIM)

    rows = [
        ("Airlines tracked", ", ".join(airlines)),
        ("Cheapest price found", f"Rs {best_price:,}" if best_price else "No prices recorded"),
        ("Cheapest airline", best_airline or "-"),
        ("Cheapest day", best_date.strftime("%d %b %Y") if best_date else "-"),
    ]
    for i, (label, value) in enumerate(rows):
        r = 6 + i
        ws1[f"B{r}"] = label
        ws1[f"B{r}"].fill = label_fill
        ws1[f"B{r}"].font = Font(name="Arial", size=10, bold=True, color="5C5646")
        ws1[f"B{r}"].border = border
        ws1[f"D{r}"] = value
        ws1[f"D{r}"].border = border
        ws1[f"D{r}"].font = Font(name="Arial", size=12, bold=True)
        if i == 1:
            ws1[f"D{r}"].fill = PatternFill("solid", fgColor=SAGE)

    ws1.column_dimensions["B"].width = 26
    ws1.column_dimensions["D"].width = 30

    # ---- Daily Prices sheet ----
    ws2 = wb.create_sheet("Daily Prices")
    ws2.sheet_properties.tabColor = GOLD
    for row in ws2.iter_rows(min_row=1, max_row=30, min_col=1, max_col=8):
        for cell in row:
            cell.fill = PatternFill("solid", fgColor=CREAM)

    ws2["B2"] = "Daily Price Tracking"
    ws2["B2"].font = Font(name="Arial", size=16, bold=True, color=NAVY)

    headers = ["Date"] + airlines
    for i, h in enumerate(headers):
        c = ws2.cell(row=4, column=2 + i, value=h)
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.font = Font(name="Arial", size=10, bold=True, color=WHITE)
        c.alignment = Alignment(horizontal="center")
        c.border = border

    for i, d in enumerate(sorted_dates):
        r = 5 + i
        ws2.cell(row=r, column=2, value=d.strftime("%d %b %Y")).font = Font(name="Arial", size=10, bold=True, color=NAVY)
        for j, airline in enumerate(airlines):
            price = by_date[d].get(airline)
            cell = ws2.cell(row=r, column=3 + j, value=price if price is not None else "-")
            if price is not None:
                cell.number_format = '"Rs"#,##0'
            cell.border = border
            if price is not None and price == best_price:
                cell.fill = PatternFill("solid", fgColor=SAGE)
                cell.font = Font(name="Arial", size=10, bold=True, color="2F6B54")

    # A short, honest note on which airlines have a known student discount --
    # sourced from the same discounts.py the rest of the app uses, so this
    # never drifts out of sync with what the website itself would show.
    discount_row = 6 + len(sorted_dates)
    ws2[f"B{discount_row}"] = "Student discounts"
    ws2[f"B{discount_row}"].font = Font(name="Arial", size=11, bold=True, color=NAVY)
    for i, airline in enumerate(airlines):
        programs = get_discounts_for_airline(airline)
        pct_programs = [p for p in programs if p["discount_type"] == "percentage"]
        if pct_programs:
            best_pct = max(p["discount_value"] for p in pct_programs)
            note = f"{airline}: up to {best_pct}% off"
        elif programs:
            note = f"{airline}: baggage/other perk, no fare discount"
        else:
            note = f"{airline}: no known student program"
        ws2[f"B{discount_row + 1 + i}"] = note
        ws2[f"B{discount_row + 1 + i}"].font = Font(name="Arial", size=9, color="5C5646")

    link_row = discount_row + len(airlines) + 2
    ws2[f"B{link_row}"] = "Verify current price & book"
    ws2[f"B{link_row}"].font = Font(name="Arial", size=11, bold=True, color=NAVY)
    ws2[f"B{link_row + 1}"] = f"Search {origin} to {destination} on Stufly"
    ws2[f"B{link_row + 1}"].hyperlink = f"https://stufly.app/search?origin={origin}&destination={destination}"
    ws2[f"B{link_row + 1}"].font = Font(name="Arial", size=10, underline="single", color="1155CC")

    for col, w in zip("BCDEF", [16, 15, 15, 15, 15]):
        ws2.column_dimensions[col].width = w

    wb.save(filepath)


def send_tracker_report(tracker: dict):
    """
    Puts together the finished report and emails it. This is the one place
    where a tracker's whole story comes together -- pull the recorded
    history, build the spreadsheet, write a short human-readable summary
    in the email body itself (so the headline is visible without even
    opening the attachment), and send it.

    If Gmail isn't configured, we log and quietly skip rather than
    crashing the poller -- a missing email credential shouldn't take down
    the whole background price-tracking process.
    """
    if not _GMAIL_ADDRESS or not _GMAIL_APP_PASSWORD:
        print(f"[notifications] Gmail not configured -- skipping report for tracker {tracker['id']}")
        return False

    history = get_tracker_history(tracker["id"])
    filepath = f"/tmp/stufly_tracker_{tracker['id']}.xlsx"
    _build_report_excel(tracker, history, filepath)

    best_price, best_airline, best_date = None, None, None
    for row in history:
        if row["price"] is not None and (best_price is None or row["price"] < best_price):
            best_price, best_airline, best_date = row["price"], row["airline"], row["checked_date"]

    subject = f"Your {tracker['origin']} to {tracker['destination']} tracker: results are in"
    if best_price:
        body = (
            f"Hi,\n\n"
            f"Your {tracker['duration_days']}-day tracker for {tracker['origin']} to {tracker['destination']} "
            f"is complete. The cheapest price we saw was {best_airline} at Rs {best_price:,}, on "
            f"{best_date.strftime('%d %b %Y')}.\n\n"
            f"The attached spreadsheet has the full day-by-day breakdown for every airline you asked us "
            f"to track, plus student discount info and a couple of links to verify the price and book.\n\n"
            f"-- Stufly"
        )
    else:
        body = (
            f"Hi,\n\n"
            f"Your tracker for {tracker['origin']} to {tracker['destination']} has finished, but we "
            f"weren't able to find prices for the airlines you asked about during this window. "
            f"You might want to try a fresh search on Stufly, or track again with a wider set of airlines.\n\n"
            f"-- Stufly"
        )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = _GMAIL_ADDRESS
    msg["To"] = tracker["email"]
    msg.attach(MIMEText(body, "plain"))

    with open(filepath, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f"attachment; filename=stufly_tracker_{tracker['origin']}_{tracker['destination']}.xlsx")
    msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(_GMAIL_ADDRESS, _GMAIL_APP_PASSWORD)
            server.sendmail(_GMAIL_ADDRESS, [tracker["email"]], msg.as_string())
        print(f"[notifications] Sent tracker report to {tracker['email']} for tracker {tracker['id']}")
        return True
    except Exception as e:
        print(f"[notifications] Failed to send tracker report {tracker['id']}: {e}")
        return False