"""Fill demo/health.db with what the web app needs for screenshots and demos: weight, a chat, a diet plan and "right now" advice.

Run after scripts/demo_data.py. Nothing here calls a model; it writes the cached results the app would have produced.

    HEALTH_DB=demo/health.db MEMORY_FILE=demo/MEMORY.md HEIGHT_CM=175 WORK_HOURS=22-06 TARGET_BEDTIME=07-08 TARGET_WAKE=15-16 \\
        uv run scripts/seed_demo_app.py
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import assistant  # noqa: E402
import healthsync  # noqa: E402
import metrics  # noqa: E402

conn = healthsync.db()
now = datetime.now()
for days_ago, kg in ((21, 80.6), (14, 80.1), (7, 79.8), (1, 79.4)):
    metrics.log_weight(conn, kg, now - timedelta(days=days_ago), source="manual")

for role, text in (
    ("assistant", "Morning. Steps dropped to 3,100 yesterday against your usual ~9k, and you slept 5h 40m. Rough day, or was the band off?"),
    ("user", "Rough day, had a cold. Band was on."),
    ("assistant", "Noted, I'll treat yesterday as a sick day, not a trend. Rest today. Are you eating normally, and is your appetite okay?"),
):
    conn.execute("INSERT INTO chat_messages (ts, role, content, session) VALUES (?, ?, ?, 1)", (now.isoformat(timespec="seconds"), role, text))
conn.commit()

assistant.MEMORY_FILE.parent.mkdir(exist_ok=True)
assistant.MEMORY_FILE.write_text("""# Health memory

_Last updated: demo_

## Profile & goals
- Wants to lose about 4 kg gradually. (demo)

## Routine & lifestyle
- Works 09:00-17:00 weekdays, sleeps 23:00-07:00. (demo)

## Patterns noticed
- Usually ~9k steps on work days, drops on weekends.

## Open questions (ask when the moment is right)
- Appetite while recovering from the cold?
""")
plan = {"summary": "At 79 kg (BMI 25.8) a gentle deficit of about 0.4 kg/week fits your 22:00-06:00 night shift. About 2,000 kcal and 110 g protein is an estimate, not a measured burn.",
        "daily_kcal": 2000, "protein_g": 110, "water_l": 2.5,
        "meals": [{"name": "Wake-up brunch", "time": "15:30", "what": "3-egg omelette, 2 rotis, a banana", "kcal": 550},
                  {"name": "Pre-shift dinner", "time": "20:00", "what": "Chicken or paneer, 1 cup rice, dal, large salad", "kcal": 650},
                  {"name": "Mid-shift meal", "time": "00:30", "what": "Wrap with paneer and veg, small curd", "kcal": 450},
                  {"name": "Mid-shift snack", "time": "03:30", "what": "Roasted chana and a fruit", "kcal": 250},
                  {"name": "Post-shift wind-down", "time": "06:30", "what": "Warm milk, only if hungry", "kcal": 100}],
        "timing_notes": ["First meal within an hour of waking.", "Nothing heavy in the 3 hours before your 07:00 bedtime."],
        "avoid": ["Caffeine after 02:00", "Sugary drinks on shift"]}
st = metrics.day_state()
advice = {"action": {"sleep": "avoid", "work": "light", "free": "eat"}[st["state"]],
          "headline": {"sleep": "Avoid eating. Have water.", "work": "Small snack, then water", "free": "Time for dinner"}[st["state"]],
          "foods": [] if st["state"] == "sleep" else ["Greek yogurt with a handful of nuts (~250 kcal)", "A glass of water"],
          "why": "Demo data. Dinner is behind you and the next planned meal is a few hours away, so keep it small.",
          "at": now.isoformat(timespec="minutes"), "state": st["state"]}
with conn:
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('diet_plan', ?)", (json.dumps({"at": now.isoformat(timespec="minutes"), "fingerprint": assistant.plan_fingerprint(conn), "plan": plan}),))
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('diet_now', ?)", (json.dumps(advice),))
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('last_diet_refresh', ?)", (healthsync.now_utc().isoformat(),))
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('last_catchup', ?)", (healthsync.now_utc().isoformat(),))
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('briefing_date', ?)", (now.date().isoformat(),))
metrics.log_food(conn, "Chicken, rice, dal, salad", 640, now.replace(hour=20, minute=10) - timedelta(days=0 if now.hour >= 20 else 1))
print("seeded", st)
