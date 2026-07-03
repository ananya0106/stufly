"""
main.py
-------
FastAPI app. This is the layer the frontend actually talks to.

Run locally with:
    uvicorn main:app --reload

Then visit http://127.0.0.1:8000/docs to test it in the browser
(FastAPI auto-generates an interactive test page — use this constantly
while building, it's faster than curl).
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from flights import search_direct_flight

app = FastAPI(title="Flight Reroute + Student Fares API")

# CORS: without this, your React frontend (running on a different port,
# e.g. localhost:5173) will be blocked by the browser from calling this API.
# In production you'd restrict allow_origins to your actual frontend domain
# instead of "*", but "*" is fine while developing locally.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "ok", "message": "Flight API is running"}


@app.get("/search/direct")
def search_direct(
    origin: str = Query(..., min_length=3, max_length=3, description="e.g. BOM"),
    destination: str = Query(..., min_length=3, max_length=3, description="e.g. YYZ"),
    travel_date: str = Query(..., description="YYYY-MM-DD"),
):
    """
    Returns the cheapest direct flight for a given route + date.
    This is the simplest possible endpoint — no reroute logic yet,
    just: does fast-flights give us real data back.
    """
    origin = origin.upper()
    destination = destination.upper()

    result = search_direct_flight(origin, destination, travel_date)

    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No flights found for {origin} -> {destination} on {travel_date}",
        )

    return result
