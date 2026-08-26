from notifications import init_notifications_db, create_tracker, save_tracker_day, send_tracker_report

init_notifications_db()

tracker_id = create_tracker(
    "ishikasharma01.ca@gmail.com",
    "BOM", "YYZ",
    ["Air Canada", "Emirates", "Lufthansa"],
    7,
    "economy",
)
print("Created tracker", tracker_id)

save_tracker_day(tracker_id, {"Air Canada": 58200, "Emirates": 56900, "Lufthansa": 59800})

tracker = {
    "id": tracker_id,
    "email": "ishikasharma01.ca@gmail.com",
    "origin": "BOM",
    "destination": "YYZ",
    "seat_class": "economy",
    "airlines": "Air Canada,Emirates,Lufthansa",
    "duration_days": 7,
}
sent = send_tracker_report(tracker)
print("Email sent:", sent)