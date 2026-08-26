import os
from datetime import datetime, date

from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from dotenv import load_dotenv

load_dotenv()

from flights import search_direct_flight, search_reroute_options
from cache import reroute_cache
from discounts import get_all_discounts, get_discounts_for_airline
from ai_summary import summarize_reroute
from nlp_intent import resolve_full_intent
from recommendations import predict_price_range, predict_price_trend, score_buy_now_vs_wait
from price_history import init_db
from notifications import init_notifications_db, create_tracker

# main.py
#
# This is the layer the frontend actually talks to -- every button click
# and search on the website ends up as a request to one of the routes in
# this file. Everything underneath (flights.py, recommendations.py,
# notifications.py) is plumbing; this file is where it all gets stitched
# together into something a browser can actually call.
#
# Run locally with: uvicorn main:app --reload
# Then visit http://127.0.0.1:8000/docs to try it in the browser.

app = FastAPI(title="Flight Reroute + Student Fares API")

# Both of these just make sure the right database tables exist before
# anything tries to use them. Safe to call on every startup -- they only
# create a table if it's genuinely missing, so this never wipes real data.
init_db()
init_notifications_db()

# Caps how many requests one person can make per minute, so a runaway
# frontend bug (or someone deliberately hammering the API) can't burn
# through our SearchApi quota or overwhelm the server.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Without this, a browser running the frontend on a different domain would
# get silently blocked from calling this API at all -- browsers refuse
# cross-origin requests by default unless the server explicitly says it's
# okay. allow_origins=["*"] is fine while building; a real production
# deploy would lock this down to just the actual frontend's domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The connecting airports we check as possible cheaper routes. Delhi and
# Mumbai cover India's own major hubs, Dubai and Doha cover the Middle
# East, Istanbul bridges into Europe/Asia, and Frankfurt gives us a proper
# European hub too rather than relying on Istanbul alone to represent
# that whole region.
DEFAULT_HUBS = ["DEL", "BOM", "DXB", "DOH", "IST", "FRA"]

VALID_SEAT_CLASSES = ["economy", "premium-economy", "business", "first"]

ENV = os.getenv("ENV", "production")
API_KEY = os.getenv("API_KEY")


def require_dev():
    """
    Gates the debug routes at the bottom of this file so they only work
    when ENV=dev. Raising a 404 rather than a 403 is deliberate -- a 404
    just makes the route look like it doesn't exist at all, which gives
    away less to anyone poking around than a 403 "forbidden" would.
    """
    if ENV != "dev":
        raise HTTPException(status_code=404, detail="Not found")


def require_api_key(x_api_key: str = Header(None)):
    """
    Every real route depends on this. If API_KEY was never set at all
    (e.g. someone forgot to configure it), we print a loud warning and let
    requests through anyway, rather than locking ourselves out entirely --
    but that's meant as a local-dev safety net, not something to rely on
    once this is actually live.
    """
    if API_KEY is None:
        print("[auth warning] API_KEY not set in .env -- all routes are unprotected")
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


def validate_travel_date(travel_date: str) -> str:
    """
    Checks the date is real and not in the past before it ever reaches the
    flight-search service -- catching a bad date here gives a clean,
    specific error message, instead of a confusing failure three layers
    deeper inside the scraper.
    """
    try:
        parsed = datetime.strptime(travel_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid travel_date '{travel_date}' -- must be YYYY-MM-DD")
    if parsed < date.today():
        raise HTTPException(status_code=400, detail=f"travel_date '{travel_date}' is in the past")
    return travel_date


def validate_seat_class(seat_class: str) -> str:
    seat_class = seat_class.lower()
    if seat_class not in VALID_SEAT_CLASSES:
        raise HTTPException(status_code=400, detail=f"Invalid seat_class '{seat_class}' -- must be one of {VALID_SEAT_CLASSES}")
    return seat_class


def _annotate_options(results):
    """
    Attaches student discount info and a discounted total to every option
    in a results list, whether it's a direct flight (no leg2) or a
    hub-routed one (leg1 + leg2). Pulled into its own function because
    both /search/reroute and /search/natural need to do this exact same
    step, and repeating it in two places would just be an invitation for
    them to quietly drift apart over time.
    """
    for option in results:
        option["leg1"]["student_discounts"] = get_discounts_for_airline(option["leg1"]["airlines"])
        if option.get("leg2"):
            option["leg2"]["student_discounts"] = get_discounts_for_airline(option["leg2"]["airlines"])
            option["discounted_total_price"] = (
                option["leg1"]["discounted_price"] + option["leg2"]["discounted_price"]
            )
        else:
            option["discounted_total_price"] = option["leg1"]["discounted_price"]
    return results


@app.get("/")
def root():
    return {"status": "ok", "message": "Flight API is running"}


@app.get("/search/direct", dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
def search_direct(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3, description="e.g. BOM"),
    destination: str = Query(..., min_length=3, max_length=3, description="e.g. YYZ"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
    seat_class: str = Query("economy", description="economy, premium-economy, business, or first"),
):
    """The simplest search: just the cheapest nonstop flight for a route,
    date, and cabin class -- no reroute comparison, no forecasting."""
    origin = origin.upper()
    destination = destination.upper()
    travel_date = validate_travel_date(travel_date)
    seat_class = validate_seat_class(seat_class)

    result = search_direct_flight(origin, destination, travel_date, seat_class)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No flights found for {origin} -> {destination} on {travel_date} ({seat_class})")

    result["student_discounts"] = get_discounts_for_airline(result["airlines"])
    return result


@app.get("/search/reroute", dependencies=[Depends(require_api_key)])
@limiter.limit("5/minute")
async def search_reroute(
    request: Request,
    origin: str = Query(..., min_length=3, max_length=3, description="e.g. BOM"),
    destination: str = Query(..., min_length=3, max_length=3, description="e.g. YYZ"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
    seat_class: str = Query("economy", description="economy, premium-economy, business, or first"),
    include_summary: bool = Query(False, description="If true, adds an AI-generated plain-English summary"),
):
    """
    The main comparison endpoint: checks the direct flight and every
    candidate hub together, so a genuinely good direct flight can win on
    its own merits instead of being hidden in a separate lookup.
    """
    origin = origin.upper()
    destination = destination.upper()
    travel_date = validate_travel_date(travel_date)
    seat_class = validate_seat_class(seat_class)

    results, failures = await search_reroute_options(origin, destination, travel_date, DEFAULT_HUBS, seat_class)

    if not results:
        raise HTTPException(status_code=404, detail=f"No flights found for {origin} -> {destination} on {travel_date} ({seat_class})")

    results.sort(key=lambda r: r["total_price"])
    results = _annotate_options(results)

    response = {
        "origin": origin,
        "destination": destination,
        "date": travel_date,
        "seat_class": seat_class,
        "options": results,
        "failed_hubs": [f["hub"] for f in failures],
    }

    if failures:
        response["warning"] = (
            f"{len(failures)} of {len(DEFAULT_HUBS)} hubs could not be checked "
            f"({', '.join(f['hub'] for f in failures)}); results may not be complete"
        )

    if include_summary:
        response["ai_summary"] = summarize_reroute(origin, destination, travel_date, results)

    return response


@app.get("/search/natural", dependencies=[Depends(require_api_key)])
@limiter.limit("5/minute")
async def search_natural(
    request: Request,
    query: str = Query(..., description="Free-text travel request, e.g. 'student flying Gujarat to Toronto 31 aug to 2 sep'"),
):
    """
    The actual front door for most searches: takes one plain sentence,
    has Groq pull the structured pieces out of it, and runs the same
    direct-vs-hub comparison as /search/reroute on whatever it understood.
    """
    intent = resolve_full_intent(query)

    if not intent["success"]:
        if "errors" in intent:
            raise HTTPException(status_code=400, detail=intent["errors"])
        raise HTTPException(
            status_code=422,
            detail={
                "message": intent.get("clarification_needed", "Could you provide more details?"),
                "missing_fields": intent.get("missing_fields", []),
                "understood_so_far": {
                    "origin": intent.get("origin"),
                    "destination": intent.get("destination"),
                    "start_date": intent.get("start_date"),
                },
            },
        )

    origin = intent["origin"]
    destination = intent["destination"]
    travel_date = validate_travel_date(intent["start_date"])

    results, failures = await search_reroute_options(origin, destination, travel_date, DEFAULT_HUBS)

    if not results:
        raise HTTPException(status_code=404, detail=f"No flights found for {origin} -> {destination} on {travel_date}")

    results.sort(key=lambda r: r["total_price"])
    results = _annotate_options(results)

    return {
        "understood_query": {
            "origin": origin,
            "destination": destination,
            "date": travel_date,
            "is_student": intent.get("is_student"),
            "budget": intent.get("budget"),
            "other_notes": intent.get("other_notes"),
        },
        "options": results,
        "failed_hubs": [f["hub"] for f in failures],
    }


@app.get("/search/recommend", dependencies=[Depends(require_api_key)])
def search_recommend(
    airline: str = Query(..., description="Airline name, e.g. Vistara"),
    source_city: str = Query(..., description="Source city, e.g. Delhi"),
    departure_time: str = Query(..., description="e.g. Morning, Evening"),
    stops: str = Query(..., description="e.g. zero, one"),
    arrival_time: str = Query(..., description="e.g. Morning, Evening"),
    destination_city: str = Query(..., description="Destination city, e.g. Mumbai"),
    travel_class: str = Query(..., description="Economy or Business (model only supports these two)"),
    duration: float = Query(..., description="Flight duration in hours"),
    days_left: int = Query(..., description="Days before departure"),
    current_price: int = Query(None, description="Optional: today's actual observed price, for a buy-now-vs-wait signal"),
):
    """
    The forecasting endpoint. Predicts a price range for this flight based
    on how many days out from departure it's being booked, using a model
    trained on real Indian domestic fare history.

    Worth being upfront about the limitation: the training data only has
    Economy and Business cabin classes, and it's domestic routes only --
    for international routes like the ones Stufly is really built for,
    treat this as a general approximation of how prices typically move,
    not a precise prediction for that specific route, until the model's
    retrained on Stufly's own collected data.
    """
    if travel_class not in ("Economy", "Business"):
        raise HTTPException(
            status_code=400,
            detail="The forecasting model only supports travel_class 'Economy' or 'Business' -- the training data didn't include Premium Economy or First.",
        )

    predicted_range = predict_price_range(airline, source_city, departure_time, stops, arrival_time, destination_city, travel_class, duration, days_left)
    trend = predict_price_trend(airline, source_city, departure_time, stops, arrival_time, destination_city, travel_class, duration)

    response = {
        "predicted_range": predicted_range,
        "price_trend_by_days_left": trend,
        "caveat": "Model trained on Indian domestic flight data (Economy/Business only); treat as a general approximation for international routes.",
    }

    if current_price is not None:
        response["buy_now_vs_wait"] = score_buy_now_vs_wait(current_price, predicted_range)

    return response


@app.get("/discounts/student", dependencies=[Depends(require_api_key)])
def list_student_discounts():
    """The full catalog of known student discount programs -- useful for a
    standalone 'student deals' page on the frontend, independent of any search."""
    return {"programs": get_all_discounts()}


@app.post("/tracker/create", dependencies=[Depends(require_api_key)])
def create_price_tracker(
    email: str = Query(..., description="Where to send the final report"),
    origin: str = Query(..., min_length=3, max_length=3),
    destination: str = Query(..., min_length=3, max_length=3),
    airlines: str = Query(..., description="Comma-separated airline names, e.g. 'Air Canada,Emirates,Lufthansa'"),
    duration_days: int = Query(7, ge=1, le=30, description="How many days to track, 1-30"),
    seat_class: str = Query("economy"),
):
    """
    Sets up a manual price tracker. This is for someone who'd rather just
    tell us "watch these 2-3 airlines for a week" than keep checking
    prices themselves -- we do the daily checking quietly in the
    background (see poller.py) and email a proper comparison once the
    window's up. This endpoint's only job is registering that request;
    the actual watching happens elsewhere.
    """
    origin = origin.upper()
    destination = destination.upper()
    seat_class = validate_seat_class(seat_class)
    airline_list = [a.strip() for a in airlines.split(",") if a.strip()]

    if not airline_list:
        raise HTTPException(status_code=400, detail="At least one airline is required")
    if len(airline_list) > 5:
        raise HTTPException(status_code=400, detail="Please track 5 airlines or fewer at a time")
    if "@" not in email:
        raise HTTPException(status_code=400, detail="That doesn't look like a valid email address")

    tracker_id = create_tracker(email, origin, destination, airline_list, duration_days, seat_class)

    return {
        "tracker_id": tracker_id,
        "message": f"Tracking {origin} -> {destination} on {', '.join(airline_list)} for {duration_days} days. You'll get an email at {email} once it's done.",
    }


@app.get("/debug/cache")
async def debug_cache():
    require_dev()
    return reroute_cache.stats()


@app.delete("/debug/cache")
async def clear_cache():
    require_dev()
    entries_cleared = len(reroute_cache._store)
    reroute_cache._store.clear()
    return {"cleared": entries_cleared}