"""Generate a synthetic week of data so you can try the report without a Google account.

Usage:
    uv run scripts/demo_data.py                      # writes demo/health.db
    HEALTH_DB=demo/health.db uv run report.py --weight 78 --height 175
"""

import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import healthsync  # noqa: E402

DEVICE = "Fitbit Charge 6"
OFFSET = timedelta(hours=1)  # demo user lives at UTC+1
SOURCE = {"device": {"displayName": DEVICE}, "platform": "FITBIT", "recordingMethod": "PASSIVELY_MEASURED"}
rng = random.Random(42)


def utc(local: datetime) -> str:
    return (local - OFFSET).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def interval(start: datetime, end: datetime) -> dict:
    off = f"{int(OFFSET.total_seconds())}s"
    return {"startTime": utc(start), "startUtcOffset": off, "endTime": utc(end), "endUtcOffset": off}


def night(wake_day: date, weekend: bool) -> tuple[dict, list[tuple[datetime, datetime, bool]]]:
    """One main sleep with realistic stage cycles. Returns the data point and (start, end, asleep) spans."""
    bed = datetime.combine(wake_day - timedelta(days=1), datetime.min.time()) + timedelta(
        hours=23.5 + (1.2 if weekend else 0) + rng.uniform(-0.6, 0.8))
    t, stages, spans = bed, [], []
    target = timedelta(hours=rng.uniform(6.0, 7.8) + (0.8 if weekend else 0))
    while t - bed < target:
        for kind, minutes in (("LIGHT", rng.randint(20, 45)), ("DEEP", rng.randint(10, 35)),
                              ("LIGHT", rng.randint(10, 25)), ("REM", rng.randint(10, 30))):
            end = t + timedelta(minutes=minutes)
            stages.append({**interval(t, end), "type": kind})
            t = end
        if rng.random() < 0.3:
            end = t + timedelta(minutes=rng.randint(3, 15))
            stages.append({**interval(t, end), "type": "AWAKE"})
            t = end
    totals = {}
    for s in stages:
        mins = (datetime.fromisoformat(s["endTime"][:-1]) - datetime.fromisoformat(s["startTime"][:-1])).seconds // 60
        totals[s["type"]] = totals.get(s["type"], 0) + mins
    asleep = sum(v for k, v in totals.items() if k != "AWAKE")
    point = {
        "name": f"users/demo/dataTypes/sleep/dataPoints/{wake_day:%Y%m%d}",
        "dataSource": {**SOURCE, "recordingMethod": "DERIVED"},
        "sleep": {
            "interval": interval(bed, t), "type": "STAGES", "stages": stages, "metadata": {"mainSleep": True},
            "summary": {"minutesAsleep": str(asleep), "minutesAwake": str(totals.get("AWAKE", 0)),
                        "stagesSummary": [{"type": k, "minutes": str(v)} for k, v in totals.items()]},
        },
    }
    return point, [(bed, t, True)]


def main() -> None:
    out = Path("demo/health.db")
    out.parent.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    healthsync.DB_FILE = out
    conn = healthsync.db()
    today = date.today()
    days = [today - timedelta(days=i) for i in range(8, 0, -1)]
    points: list[tuple[str, dict]] = []
    hr_rows = []
    for d in days:
        weekend = d.weekday() >= 5
        sleep_point, _ = night(d, weekend)
        points.append(("sleep", sleep_point))
        bed = datetime.fromisoformat(sleep_point["sleep"]["interval"]["startTime"][:-1]) + OFFSET
        wake = datetime.fromisoformat(sleep_point["sleep"]["interval"]["endTime"][:-1]) + OFFSET

        # Steps: a commute walk, an office day, an evening walk; lazier at weekends.
        walk_at = 18.0 + rng.uniform(-0.5, 1.0)
        for minute in range(24 * 60):
            t = datetime.combine(d, datetime.min.time()) + timedelta(minutes=minute)
            h = minute / 60
            if t < wake or h >= 23.4:
                continue
            base = 4 if weekend else 5
            if not weekend and (8.2 <= h < 8.7 or 17.3 <= h < 17.8):
                base = 80
            elif walk_at <= h < walk_at + (0.8 if weekend else 0.5):
                base = 100
            elif weekend and 11 <= h < 13:
                base = 30
            count = max(0, int(rng.gauss(base, base * 0.35)))
            if count:
                points.append(("steps", {"dataSource": SOURCE, "steps": {"interval": interval(t, t + timedelta(minutes=1)), "count": str(count)}}))

        # Evening walk as a logged exercise.
        start = datetime.combine(d, datetime.min.time()) + timedelta(hours=walk_at)
        minutes = rng.randint(60, 85) if weekend else rng.randint(20, 40)
        kind = "CARDIO_WORKOUT" if d.weekday() == 2 else "WALKING"
        kcal = minutes * rng.randint(7, 10)
        points.append(("exercise", {
            "name": f"users/demo/dataTypes/exercise/dataPoints/{d:%Y%m%d}",
            "dataSource": SOURCE,
            "exercise": {"interval": interval(start, start + timedelta(minutes=minutes)), "exerciseType": kind,
                         "activeDuration": f"{minutes * 60}s",
                         "metricsSummary": {"caloriesKcal": kcal, "distanceMillimeters": minutes * 95_000, "steps": str(minutes * 105),
                                            "averageHeartRateBeatsPerMinute": str(rng.randint(125, 138) if kind != "WALKING" else rng.randint(100, 124)),
                                            "activeZoneMinutes": str(int(minutes * rng.uniform(0.6, 1.0)))}},
        }))

        # Heart rate in 5-minute windows: low while asleep, higher during walks.
        for k in range(288):
            t = datetime.combine(d, datetime.min.time()) + timedelta(minutes=5 * k)
            h = k / 12
            asleep = t < wake or t >= bed + timedelta(days=1)
            avg = rng.gauss(54, 2.5) if asleep else rng.gauss(72, 6)
            if walk_at <= h < walk_at + minutes / 60:
                avg = rng.gauss(118, 6)
            hr_rows.append((utc(t), utc(t + timedelta(minutes=5)), round(avg, 1), int(avg - 6), int(avg + 9)))

    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO data_points (key, data_type, civil_date, payload, fetched_at) VALUES (?, ?, ?, ?, ?)",
            [(healthsync.point_key(dt, p), dt, healthsync.civil_date(dt, p), json.dumps(p), healthsync.now_utc().isoformat())
             for dt, p in points])
        conn.executemany("INSERT OR REPLACE INTO heart_rate_5min VALUES (?, ?, ?, ?, ?)", hr_rows)
    print(f"Wrote {out}: {len(points)} data points, {len(hr_rows)} heart-rate windows")


if __name__ == "__main__":
    main()
