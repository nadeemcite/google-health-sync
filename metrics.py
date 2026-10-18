"""Read-side helpers for the assistant and the web app: daily metrics, weight history, profile and signals.

Weight comes from two places: weigh-ins synced from Google Health (data_points) and ones logged by hand
in the app (weight_log). Both are merged here, so the rest of the code never cares where a number came from.
"""

import json
import os
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

from report import DEVICE, local, query


def load(data_type: str, start: date, end: date, any_device: bool = False) -> list[dict]:
    """Data points whose civil date is in [start, end), from one source so phones and watches don't double count."""
    rows = query("SELECT payload FROM data_points WHERE data_type = ? AND civil_date >= ? AND civil_date < ?",
                 data_type, start.isoformat(), end.isoformat())
    points = [json.loads(r[0]) for r in rows]
    if any_device:
        return points
    if DEVICE:
        return [p for p in points if p["dataSource"].get("device", {}).get("displayName") == DEVICE]
    return [p for p in points if p["dataSource"].get("platform") == "FITBIT"]


# ---------- profile ----------

def get_profile(conn: sqlite3.Connection) -> dict:
    stored = dict(conn.execute("SELECT key, value FROM profile").fetchall())
    height = float(stored.get("height_cm") or os.environ.get("HEIGHT_CM") or 0) or None
    goal = float(stored["goal_weight_kg"]) if stored.get("goal_weight_kg") else None
    kcal = float(stored["kcal_target"]) if stored.get("kcal_target") else None
    return {"height_cm": height, "goal_weight_kg": goal, "kcal_target": kcal,
            "diet_style": stored.get("diet_style") or "", "diet_avoid": stored.get("diet_avoid") or ""}


def set_profile(conn: sqlite3.Connection, **values: float | str) -> None:
    with conn:
        for key, value in values.items():
            conn.execute("INSERT OR REPLACE INTO profile (key, value) VALUES (?, ?)", (key, str(value)))


# ---------- food ----------

def log_food(conn: sqlite3.Connection, text: str, kcal: int | None = None, when: datetime | None = None) -> None:
    with conn:
        conn.execute("INSERT INTO food_log (eaten_at, text, kcal) VALUES (?, ?, ?)",
                     ((when or datetime.now()).isoformat(timespec="minutes"), text, kcal))


def food_log(conn: sqlite3.Connection, days: int = 1) -> list[dict]:
    """Meals from the last `days` days (1 = today), oldest first."""
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = conn.execute("SELECT id, eaten_at, text, kcal FROM food_log WHERE eaten_at >= ? ORDER BY eaten_at", (since,)).fetchall()
    return [{"id": i, "at": at, "text": t, "kcal": k} for i, at, t, k in rows]


# ---------- weight ----------

def log_weight(conn: sqlite3.Connection, kg: float, when: datetime | None = None, note: str = "", source: str = "manual") -> None:
    when = when or datetime.now()
    with conn:
        conn.execute("INSERT INTO weight_log (measured_at, kg, source, note) VALUES (?, ?, ?, ?)",
                     (when.isoformat(timespec="minutes"), kg, source, note))


def weight_series(conn: sqlite3.Connection, days: int = 365) -> list[dict]:
    """Every weigh-in from the last `days` days, oldest first: {at, kg, source}."""
    since = date.today() - timedelta(days=days)
    series = []
    for p in load("weight", since, date.today() + timedelta(days=2), any_device=True):
        st = p["weight"]["sampleTime"]
        series.append({"at": local(st["physicalTime"], st.get("utcOffset", "0s")).isoformat(timespec="minutes"),
                       "kg": p["weight"]["weightGrams"] / 1000, "source": "google_health"})
    for at, kg, source in conn.execute("SELECT measured_at, kg, source FROM weight_log WHERE measured_at >= ?",
                                       (since.isoformat(),)):
        series.append({"at": at, "kg": kg, "source": source})
    return sorted(series, key=lambda w: w["at"])


def current_weight(conn: sqlite3.Connection) -> dict | None:
    series = weight_series(conn)
    if series:
        return series[-1]
    if os.environ.get("WEIGHT_KG"):  # the .env value is only a starting point until the first real weigh-in
        return {"at": None, "kg": float(os.environ["WEIGHT_KG"]), "source": "config"}
    return None


def weekly_slope(series: list[dict]) -> float | None:
    """Least-squares trend in kg per week, or None if there isn't enough spread to say."""
    if len(series) < 3:
        return None
    t0 = datetime.fromisoformat(series[0]["at"])
    xs = [(datetime.fromisoformat(w["at"]) - t0).total_seconds() / 86400 for w in series]
    if max(xs) < 7:
        return None
    ys = [w["kg"] for w in series]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    return 7 * sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else None


def bmi(kg: float, height_cm: float | None) -> float | None:
    return kg / (height_cm / 100) ** 2 if height_cm else None


def routine() -> dict:
    """The person's configured day as hour ranges (24h local), e.g. work [18, 3]. None where not configured."""
    def hours(name: str) -> list[int] | None:
        value = os.environ.get(name)
        return [int(h) for h in value.split("-")] if value else None
    return {"work": hours("WORK_HOURS"), "bed": hours("TARGET_BEDTIME"), "wake": hours("TARGET_WAKE"),
            "days_off": os.environ.get("DAYS_OFF", "")}


def day_state(now: datetime | None = None) -> dict:
    """Where the person is in their day: 'sleep' (should be asleep), 'work' (on shift) or 'free', plus minutes to target bedtime."""
    r, now = routine(), now or datetime.now()
    h = now.hour + now.minute / 60
    inside = lambda a, b: a <= h < b if a <= b else h >= a or h < b  # hour ranges may wrap past midnight
    if r["bed"] and r["wake"] and inside(r["bed"][1], r["wake"][0]):
        state = "sleep"
    elif r["work"] and inside(*r["work"]):
        state = "work"
    else:
        state = "free"
    to_bed = None
    if r["bed"]:
        to_bed = 0 if inside(*r["bed"]) else round(((r["bed"][0] - h) % 24) * 60)  # 0 = it is already bedtime
    return {"state": state, "to_bed_min": to_bed}


def diet_target(weight_kg: float, height_cm: float, goal_kg: float | None = None) -> dict:
    """What the diet should aim for. Without a goal weight it is derived from BMI: bring it into 18.5 to 24.9."""
    h2 = (height_cm / 100) ** 2
    b = weight_kg / h2
    category = "underweight" if b < 18.5 else "healthy" if b < 25 else "overweight" if b < 30 else "obese"
    if goal_kg and abs(goal_kg - weight_kg) > 1:
        direction, goal, auto = ("lose" if goal_kg < weight_kg else "gain"), goal_kg, False
    elif category == "underweight":
        direction, goal, auto = "gain", round(19.5 * h2, 1), True
    elif category == "healthy":
        direction, goal, auto = "maintain", round(weight_kg, 1), True
    else:
        direction, goal, auto = "lose", round(24.9 * h2, 1), True
    pace = 0.25 if direction == "gain" else 0.5 if direction == "lose" else 0.0
    return {"bmi": round(b, 1), "category": category, "direction": direction, "goal_kg": goal, "auto": auto,
            "kg_per_week": pace, "weeks": round(abs(goal - weight_kg) / pace) if pace else None}


# ---------- daily metrics ----------

def daily(start: date, end: date) -> dict[date, dict]:
    """Per civil day in [start, end): steps, sleep (keyed by the day you woke), workouts, resting HR and HRV."""
    days: dict[date, dict] = {}
    day = lambda d: days.setdefault(d, {"steps": None, "sleep_min": None, "bed": None, "wake": None,
                                        "workouts": [], "resting_hr": None, "hrv_ms": None})
    for p in load("steps", start, end):
        iv = p["steps"]["interval"]
        d = day(local(iv["startTime"], iv["startUtcOffset"]).date())
        d["steps"] = (d["steps"] or 0) + int(p["steps"]["count"])
    for p in load("sleep", start, end):
        s = p["sleep"]
        if s.get("metadata", {}).get("nap"):
            continue
        iv = s["interval"]
        bed, wake = local(iv["startTime"], iv["startUtcOffset"]), local(iv["endTime"], iv["endUtcOffset"])
        d = day(wake.date())
        d["sleep_min"] = (d["sleep_min"] or 0) + int(s["summary"]["minutesAsleep"])
        if d["bed"] is None or bed < datetime.fromisoformat(d["bed"]):
            d["bed"] = bed.isoformat(timespec="minutes")  # earliest session start of the night
        if d["wake"] is None or wake > datetime.fromisoformat(d["wake"]):
            d["wake"] = wake.isoformat(timespec="minutes")
    for p in load("exercise", start, end):
        e = p["exercise"]
        minutes = float(e["activeDuration"].rstrip("s")) / 60
        if minutes < 2:
            continue
        iv = e["interval"]
        m = e["metricsSummary"]
        day(local(iv["startTime"], iv["startUtcOffset"]).date())["workouts"].append({
            "type": e["exerciseType"], "minutes": round(minutes), "kcal": round(m.get("caloriesKcal", 0)),
            "km": round(m.get("distanceMillimeters", 0) / 1e6, 1), "avg_hr": int(m.get("averageHeartRateBeatsPerMinute", 0))})
    for p in load("daily-resting-heart-rate", start, end, any_device=True):
        b = p["dailyRestingHeartRate"]
        day(date(**b["date"]))["resting_hr"] = int(b["beatsPerMinute"])
    for p in load("daily-heart-rate-variability", start, end, any_device=True):
        b = p["dailyHeartRateVariability"]
        day(date(**b["date"]))["hrv_ms"] = round(b["averageHeartRateVariabilityMilliseconds"])
    return dict(sorted(days.items()))


def daily_rows(start: date, end: date) -> list[dict]:
    """`daily` as a flat list for the model, with weekday names and None for 'the band recorded nothing'."""
    return [{"date": d.isoformat(), "weekday": f"{d:%a}", **v} for d, v in daily(start, end).items()]


# ---------- signals ----------

def signals(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    """Things worth bringing up when the app opens, most important first: {id, text}.

    These are plain facts plus the question they raise; the assistant decides how to word them.
    """
    today = today or date.today()
    out: list[dict] = []
    recent = daily(today - timedelta(days=15), today)  # excludes today: it isn't finished
    yesterday = today - timedelta(days=1)
    prior = [v["steps"] for d, v in recent.items() if d < yesterday and v["steps"]]
    typical = statistics.median(prior) if prior else None

    y = recent.get(yesterday)
    if not y or y["steps"] is None:
        out.append({"id": "no_steps_yesterday",
                    "text": f"No step data for yesterday ({yesterday:%a %d %b}). The band was off, not synced yet, or the sync is stale."})
    elif typical and y["steps"] < max(2000, 0.4 * typical):
        out.append({"id": "low_activity_yesterday",
                    "text": f"Yesterday ({yesterday:%a}) only {y['steps']:,} steps against a typical {typical:,.0f}. Ask why: day off, "
                            f"tired, ill, or the band not worn?"})

    last_sync = conn.execute("SELECT value FROM sync_state WHERE name = 'last_catchup'").fetchone()
    if last_sync and datetime.now(timezone.utc) - datetime.fromisoformat(last_sync[0]) > timedelta(hours=36):
        out.append({"id": "sync_stale", "text": f"Data last synced {last_sync[0][:16]} UTC, over 36 hours ago, so recent numbers may be missing."})

    profile = get_profile(conn)
    series = weight_series(conn, 60)
    if not series:
        out.append({"id": "no_weight", "text": "There is no weigh-in on record. Ask for today's weight."})
    else:
        age = (datetime.now() - datetime.fromisoformat(series[-1]["at"])).days
        if age >= 7:
            out.append({"id": "weight_stale", "text": f"Last weigh-in was {age} days ago ({series[-1]['kg']:.1f} kg). Ask for a fresh one."})
        window = [w for w in series if (datetime.now() - datetime.fromisoformat(w["at"])).days <= 28]
        slope = weekly_slope(window)
        goal = profile["goal_weight_kg"] or (24.9 * (profile["height_cm"] / 100) ** 2 if profile["height_cm"] else None)
        if slope is not None and goal and window[-1]["kg"] > goal + 0.5 and slope > -0.1:
            workouts = sum(len(v["workouts"]) for v in recent.values())
            steps = [v["steps"] for v in recent.values() if v["steps"]]
            out.append({"id": "weight_plateau",
                        "text": f"Weight trend is {slope:+.2f} kg/week over the last 4 weeks ({window[0]['kg']:.1f} to {window[-1]['kg']:.1f} kg) "
                                f"with the goal at {goal:.0f} kg. Activity in the last two weeks: {workouts} workouts, "
                                f"{statistics.mean(steps) if steps else 0:,.0f} steps/day. Ask about food and the exercise-food balance."})

    nights = [v["sleep_min"] for d, v in recent.items() if v["sleep_min"]][-3:]
    if len(nights) == 3 and statistics.mean(nights) < 360:
        out.append({"id": "short_sleep", "text": f"Last 3 nights averaged {statistics.mean(nights) / 60:.1f}h of sleep."})
    return out
