"""
ai_summary.py
-------------
Takes the ALREADY-COMPUTED reroute data (real prices, real savings —
nothing here is invented by the AI) and asks Claude to turn it into a
short, human-readable summary.

Important design rule: the AI never sees raw prices without them
already being real numbers from fast-flights. It only narrates data
we've already validated. This keeps a hallucination from ever turning
into a fake price shown to a user.

Requires: pip install anthropic
Requires: an ANTHROPIC_API_KEY environment variable set.
    export ANTHROPIC_API_KEY="sk-ant-..."
Get a key at https://console.anthropic.com
"""

import os
from anthropic import Anthropic

client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment automatically

MODEL = "claude-haiku-4-5-20251001"  # cheap + fast, right fit for short narration


def summarize_reroute(reroute_data: dict) -> str:
    """
    Args:
        reroute_data: the exact dict returned by search_reroute_options()
                       i.e. {"direct": {...}, "alternatives": [...]}

    Returns:
        A short (1-3 sentence) plain-English summary string.
        Falls back to a simple template if the API call fails, so a
        flaky/missing API key never breaks the whole search endpoint.
    """
    direct = reroute_data.get("direct")
    alternatives = reroute_data.get("alternatives", [])

    if not alternatives:
        if direct:
            return f"No cheaper rerouting options found — the direct flight at ₹{direct['price']} is your best bet."
        return "No flights found for this route."

    best = alternatives[0]  # already sorted by savings, highest first

    # We hand Claude ONLY the numbers we've already computed — it is
    # explicitly told not to introduce any figure that isn't given to it.
    prompt = f"""Here is real flight data (already computed, do not alter any numbers):

Direct flight: {direct['origin']} -> {direct['destination']}, price ₹{direct['price']}, {direct['duration_minutes']} minutes, {direct['airlines']}

Best alternative: via {best['hub']}
  Leg 1: {best['leg1']['origin']} -> {best['leg1']['destination']}, ₹{best['leg1']['price']}, {best['leg1']['duration_minutes']} min
  Leg 2: {best['leg2']['origin']} -> {best['leg2']['destination']}, ₹{best['leg2']['price']}, {best['leg2']['duration_minutes']} min
  Total: ₹{best['combo_price']}, savings: ₹{best['savings']}

Total alternatives found: {len(alternatives)}

Write a 1-2 sentence, friendly, plain-English summary a traveler would read on a search results page. Only use the numbers given above — do not calculate or invent any new figures. Mention the savings and briefly note the tradeoff (extra stop, separate tickets)."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        # If the API call fails for any reason (bad key, rate limit, network),
        # fall back to a plain template instead of breaking the endpoint.
        print(f"[ai_summary error] {e}")
        return (
            f"Flying via {best['hub']} saves ₹{best['savings']} "
            f"compared to the direct flight (₹{best['combo_price']} vs ₹{direct['price']})."
        )
