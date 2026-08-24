"""
nlp_intent.py
--------------
Extracts structured travel intent from ONE free-form user sentence,
using Groq to do entity extraction (dates, locations, budget, student
status, anything else mentioned) rather than requiring separate fields.

Falls back gracefully: if Groq fails or returns something unusable, the
caller can fall back to nlp_parser's rule-based origin/destination/date
fields as a backup, or ask the user to clarify.
"""
import json
import os
from groq import Groq
from nlp_parser import resolve_airport, parse_date_range

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


MODEL = "llama-3.1-8b-instant"

EXTRACTION_PROMPT = """You extract structured travel search information from a user's message.
Return ONLY valid JSON, no other text, no markdown formatting, in exactly this shape:

{{
  "origin": "<city, state, or airport name mentioned as departure point, or null if not mentioned>",
  "destination": "<city, state, or airport name mentioned as arrival point, or null if not mentioned>",
  "date_text": "<the exact date or date range phrase as the user wrote it, or null if not mentioned>",
  "is_student": <true if user mentions being a student/student discount, false otherwise>,
  "budget": <a number if the user mentions a budget/max price, or null>,
  "flexible_dates": <true if user indicates date flexibility, e.g. "around", "sometime", "or nearby dates">,
  "other_notes": "<anything else relevant the user mentioned that doesn't fit above fields, e.g. 'prefers direct flights', or null>"
}}

Do not invent information the user didn't provide. If something isn't mentioned, use null (or false for booleans).

User message: "{user_text}"

JSON:"""


def extract_travel_intent(user_text: str) -> dict:
    """
    Args:
        user_text: raw free-form user input, e.g.
            "student flying Gujarat to Toronto 31 aug to 2 sep, budget 60k"

    Returns a dict with the extracted fields (see EXTRACTION_PROMPT shape),
    plus a "parse_success" bool. On any failure, returns parse_success=False
    with an "error" field -- caller should fall back to asking the user for
    structured fields directly rather than guessing.
    """
    try:
        response = _get_client().chat.completions.create(
            model=MODEL,
            max_tokens=300,
            temperature=0,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT.format(user_text=user_text)}],
        )
        raw = response.choices[0].message.content.strip()

        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        extracted = json.loads(raw)
        extracted["parse_success"] = True
        return extracted

    except Exception as e:
        print(f"[nlp_intent error] {e}")
        return {"parse_success": False, "error": str(e)}


def resolve_full_intent(user_text: str) -> dict:
    """
    Full pipeline: extract intent via Groq, then resolve the extracted
    origin/destination text into real airport codes and the date_text
    into real dates, using the existing rule-based resolvers.
    """
    intent = extract_travel_intent(user_text)

    if not intent.get("parse_success"):
        return {
            "success": False,
            "errors": ["Couldn't understand the message -- please specify your origin, destination, and travel dates directly."],
        }

    origin_code = resolve_airport(intent.get("origin")) if intent.get("origin") else None
    destination_code = resolve_airport(intent.get("destination")) if intent.get("destination") else None
    date_range = parse_date_range(intent.get("date_text")) if intent.get("date_text") else None

    missing = []
    if origin_code is None:
        missing.append("origin")
    if destination_code is None:
        missing.append("destination")
    if date_range is None:
        missing.append("travel dates")

    result = {
        "success": len(missing) == 0,
        "origin": origin_code,
        "destination": destination_code,
        "start_date": date_range[0] if date_range else None,
        "end_date": date_range[1] if date_range else None,
        "is_student": intent.get("is_student", False),
        "budget": intent.get("budget"),
        "flexible_dates": intent.get("flexible_dates", False),
        "other_notes": intent.get("other_notes"),
    }

    if missing:
        result["missing_fields"] = missing
        result["clarification_needed"] = f"Could you clarify your {', '.join(missing)}?"

    return result