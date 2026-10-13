"""Build a one-page PDF health report from the data synced into data/health.db.

Usage:
    uv run report.py --weight 80 --height 170               # the 7 days before today
    uv run report.py --weight 80 --height 170 --end 2026-10-13
"""

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env")  # routine settings below are read at import time

DB_FILE = Path(os.environ.get("HEALTH_DB", "data/health.db"))
OUT_DIR = Path(os.environ.get("REPORT_DIR", "reports"))
CHROMES = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chromium", "chromium-browser", "google-chrome"]
REPORT_DAYS = 7
window: tuple[date, date] = (date.min, date.max)  # [start, end) of civil dates, set by generate()



def hour_range(value: str) -> tuple[int, int]:
    """"23-00" -> (23, 0): a window of whole hours on the 24h clock."""
    start, end = value.split("-")
    return int(start), int(end)


# Your routine (set in .env; see .env.example).
TARGET_BED = hour_range(os.environ.get("TARGET_BEDTIME", "23-00"))   # fall asleep within this hour range
TARGET_WAKE = hour_range(os.environ.get("TARGET_WAKE", "07-08"))     # wake within this hour range
SHIFT = hour_range(os.environ["WORK_HOURS"]) if os.environ.get("WORK_HOURS") else None  # e.g. "18-03" for a night shift
DAYS_OFF = {d.strip()[:3].title() for d in os.environ.get("DAYS_OFF", "Sat,Sun").split(",") if d.strip()}
DEVICE = os.environ.get("DEVICE_NAME")  # only use data from this device; default: any Google/Fitbit wearable

# Reference categorical palette (validated: scripts/validate_palette.js, light mode).
BLUE, AQUA, VIOLET, ORANGE = "#2a78d6", "#1baf7a", "#6250d6", "#eb6834"
GOOD, WARN, WARN_TEXT = "#0d8a4f", "#d9631e", "#b04e12"
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0"
STAGE_COLORS = {"DEEP": BLUE, "LIGHT": AQUA, "REM": VIOLET, "AWAKE": ORANGE}
STAGE_LABELS = {"DEEP": "Deep", "LIGHT": "Light", "REM": "REM", "AWAKE": "Awake"}


def local(ts: str, offset: str) -> datetime:
    utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (utc + timedelta(seconds=float(offset.rstrip("s")))).replace(tzinfo=None)


def query(sql: str, *args) -> list[tuple]:
    with sqlite3.connect(DB_FILE) as conn:
        return conn.execute(sql, args).fetchall()


def load(name: str) -> list[dict]:
    rows = query("SELECT payload FROM data_points WHERE data_type = ? AND civil_date >= ? AND civil_date < ?",
                 name, window[0].isoformat(), window[1].isoformat())
    points = [json.loads(r[0]) for r in rows]
    # Keep one source so phones and third-party watches don't double count.
    if DEVICE:
        return [p for p in points if p["dataSource"].get("device", {}).get("displayName") == DEVICE]
    return [p for p in points if p["dataSource"].get("platform") == "FITBIT"]


def day_off(d: date) -> bool:
    return f"{d:%a}" in DAYS_OFF


def axis_hour(h: float) -> float:
    """Hour on the report's night axis, where 18:00 = -6 and 13:00 = 13."""
    return h - 24 if h >= 18 else h


def clock_spans(start: float, end: float) -> list[tuple[float, float]]:
    """A clock window as spans on a 0–24 axis, split in two if it wraps past midnight."""
    end = 24 if end == 0 and start > 0 else end
    return [(start, end)] if start < end else [(start, 24), (0, end)]


def night_hours(t: datetime) -> float:
    return axis_hour(t.hour + t.minute / 60)


def analyse(weight: float, height_cm: float) -> dict:
    daily_steps, hourly_steps = defaultdict(int), defaultdict(int)
    for p in load("steps"):
        iv = p["steps"]["interval"]
        t = local(iv["startTime"], iv["startUtcOffset"])
        daily_steps[t.date()] += int(p["steps"]["count"])
        hourly_steps[t.hour] += int(p["steps"]["count"])
    days = sorted(daily_steps)
    device_counts = defaultdict(int)
    for p in load("steps"):
        device_counts[p["dataSource"].get("device", {}).get("displayName", "your wearable")] += 1

    # Group sleep by the day you woke up; naps are kept separately.
    nights = defaultdict(lambda: {"sessions": [], "asleep": 0, "awake": 0, "stages": defaultdict(int), "naps": []})
    for p in load("sleep"):
        s = p["sleep"]
        iv = s["interval"]
        start, end = local(iv["startTime"], iv["startUtcOffset"]), local(iv["endTime"], iv["endUtcOffset"])
        segs = [(local(x["startTime"], x["startUtcOffset"]), local(x["endTime"], x["endUtcOffset"]), x["type"])
                for x in s.get("stages", [])]
        n = nights[end.date()]
        if s.get("metadata", {}).get("nap"):
            n["naps"].append({"start": start, "end": end, "segs": segs, "asleep": int(s["summary"]["minutesAsleep"])})
            continue
        n["sessions"].append({"start": start, "end": end, "segs": segs})
        n["asleep"] += int(s["summary"]["minutesAsleep"])
        n["awake"] += int(s["summary"].get("minutesAwake", 0))
        for st in s["summary"].get("stagesSummary", []):
            n["stages"][st["type"]] += int(st["minutes"])
    for n in nights.values():
        if n["sessions"]:
            n["bed"] = min(x["start"] for x in n["sessions"])
            n["wake"] = max(x["end"] for x in n["sessions"])
            # The evening this night belongs to (a 01:00 bedtime belongs to the previous evening).
            n["evening"] = (n["bed"] - timedelta(hours=12)).date()
            n["holiday"] = day_off(n["evening"])

    workouts = []
    for p in load("exercise"):
        e = p["exercise"]
        minutes = float(e["activeDuration"].rstrip("s")) / 60
        if minutes < 2:  # accidental start/stop
            continue
        m = e["metricsSummary"]
        iv = e["interval"]
        workouts.append({
            "type": e["exerciseType"],
            "start": local(iv["startTime"], iv["startUtcOffset"]),
            "minutes": minutes,
            "hr": int(m.get("averageHeartRateBeatsPerMinute", 0)),
            "kcal": m.get("caloriesKcal", 0),
            "azm": int(m.get("activeZoneMinutes", 0)),
            "km": m.get("distanceMillimeters", 0) / 1e6,
        })

    slept = [nights[d] for d in days if nights.get(d, {}).get("sessions")]
    if not days or not slept:
        raise SystemExit("Not enough data for a report yet: need at least one day of steps and one night of sleep.")
    work_nights = [n for n in slept if not n["holiday"]]
    holiday_nights = [n for n in slept if n["holiday"]]
    total_stages = defaultdict(int)
    for n in slept:
        for k, v in n["stages"].items():
            total_stages[k] += v

    height_m = height_cm / 100
    avg = lambda xs: sum(xs) / len(xs) if xs else 0
    return {
        "days": days,
        "daily_steps": daily_steps,
        "hourly_avg": {h: hourly_steps[h] / len(days) for h in range(24)},
        "nights": nights,
        "slept": slept,
        "workouts": sorted(workouts, key=lambda w: w["start"]),
        "total_stages": total_stages,
        "avg_steps": avg(list(daily_steps.values())),
        "goal_days": sum(v >= 10_000 for v in daily_steps.values()),
        "avg_sleep": avg([n["asleep"] for n in slept]),
        "avg_sleep_work": avg([n["asleep"] for n in work_nights]),
        "avg_sleep_holiday": avg([n["asleep"] for n in holiday_nights]),
        "nights_7h": sum(n["asleep"] >= 420 for n in slept),
        "avg_bed": avg([night_hours(n["bed"]) for n in work_nights]),
        "avg_wake": avg([night_hours(n["wake"]) for n in work_nights]),
        "early_bed_in_shift": [n for n in work_nights
                               if SHIFT and axis_hour(SHIFT[0]) <= night_hours(n["bed"]) < axis_hour(SHIFT[1])],
        "worst_awake": max(slept, key=lambda n: n["awake"]),
        "holiday_beds": [n["bed"] for n in holiday_nights],
        "window_steps": sum(hourly_steps[h] for a0, a1 in clock_spans(TARGET_BED[1], TARGET_WAKE[0])
                            for h in range(int(a0), int(a1))) / len(days),
        "device": max(device_counts, key=device_counts.get, default="your wearable"),
        "azm": sum(w["azm"] for w in workouts),
        "km": sum(w["km"] for w in workouts),
        "kcal": sum(w["kcal"] for w in workouts),
        "hr": analyse_heart_rate(nights),
        "bmi": weight / height_m**2,
        "healthy_lo": 18.5 * height_m**2,
        "healthy_hi": 24.9 * height_m**2,
        "weight": weight,
        "height": height_cm,
    }


def analyse_heart_rate(nights: dict) -> dict:
    """Heart rate for one day: the last mostly-complete day in the window, from 5-minute rollups."""
    latest_steps = query("SELECT payload FROM data_points WHERE data_type = 'steps' ORDER BY civil_date DESC LIMIT 1")
    offset = json.loads(latest_steps[0][0])["steps"]["interval"]["startUtcOffset"]
    windows = []  # (local start, avg, max)
    for start, avg, peak in query("SELECT start_utc, bpm_avg, bpm_max FROM heart_rate_5min ORDER BY start_utc"):
        t = local(start, offset)
        if window[0] <= t.date() < window[1] and avg is not None:
            windows.append((t, avg, peak))
    per_day = defaultdict(int)
    for t, _, _ in windows:
        per_day[t.date()] += 1
    full_days = [d for d, n in per_day.items() if n >= 200]  # 288 windows = full day
    if not full_days:
        return None
    day = max(full_days)
    windows = [w for w in windows if w[0].date() == day]
    samples = [(t, avg) for t, avg, _ in windows]
    series = {(t.hour * 60 + t.minute) // 5: avg for t, avg in samples}

    asleep_ranges = [(s["start"], s["end"]) for n in nights.values() for s in n["sessions"] + n["naps"]]
    is_asleep = lambda t: any(a <= t < b for a, b in asleep_ranges)
    sleep_bpm = [b for t, b in samples if is_asleep(t)]
    awake_bpm = [b for t, b in samples if not is_asleep(t)]
    # Lowest sustained rate: the minimum of 30-minute rolling means while asleep.
    sleep_series = [v for k, v in series.items() if is_asleep(datetime.combine(day, time()) + timedelta(minutes=k * 5))]
    sleeping_low = min(statistics.mean(sleep_series[i:i + 6]) for i in range(max(len(sleep_series) - 5, 1)))
    peak_t, _, peak_b = max(windows, key=lambda w: w[2])
    return {
        "day": day,
        "series": series,
        "until": samples[-1][0] + timedelta(minutes=5),
        "sleeping_low": sleeping_low,
        "sleep_avg": statistics.mean(sleep_bpm),
        "awake_avg": statistics.mean(awake_bpm),
        "peak": (peak_t, peak_b),
        "asleep_ranges": [(a, b) for a, b in asleep_ranges if a.date() == day or b.date() == day],
    }


def clock(h: float) -> str:
    h %= 24
    m = round((h % 1) * 60)
    return f"{int(h) + m // 60:02d}:{m % 60:02d}"


def hm(minutes: float) -> str:
    return f"{int(minutes // 60)}h {round(minutes % 60):02d}m"


# ---------- SVG charts ----------

def sleep_timeline(a: dict) -> str:
    """Each night on a clock axis from 18:00 to 16:00, stages drawn where they really happened."""
    w, h, l, r, t, b = 740, 196, 62, 112, 16, 18
    days = a["days"]
    rh = (h - t - b) / len(days)
    x0, x1 = -6, 16
    x = lambda hrs: l + (w - l - r) * (hrs - x0) / (x1 - x0)
    clamp = lambda hrs: min(max(hrs, x0), x1)
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart">']
    if SHIFT:
        s0, s1 = clamp(axis_hour(SHIFT[0])), clamp(axis_hour(SHIFT[1]) if SHIFT[1] else 24)
        out.append(f'<rect x="{x(s0)}" y="{t}" width="{x(s1) - x(s0)}" height="{h - t - b}" fill="{ORANGE}" opacity="0.07"/>'
                   f'<text x="{(x(s0) + x(s1)) / 2}" y="{t - 5}" class="anno" fill="{WARN_TEXT}" text-anchor="middle">work {SHIFT[0]:02d}:00–{SHIFT[1]:02d}:00</text>')
    w0, w1 = axis_hour(TARGET_BED[0]), axis_hour(TARGET_WAKE[1])
    out.append(f'<rect x="{x(w0)}" y="{t}" width="{x(w1) - x(w0)}" height="{h - t - b}" fill="{GOOD}" opacity="0.07"/>'
               f'<text x="{(x(w0) + x(w1)) / 2}" y="{t - 5}" class="anno" fill="{GOOD}" text-anchor="middle">target sleep window '
               f'{TARGET_BED[0]:02d}–{TARGET_BED[1]:02d} → {TARGET_WAKE[0]:02d}–{TARGET_WAKE[1]:02d}</text>')
    for edge in (*TARGET_BED, *TARGET_WAKE):
        out.append(f'<line x1="{x(axis_hour(edge))}" x2="{x(axis_hour(edge))}" y1="{t}" y2="{h - b}" stroke="{GOOD}" stroke-width="0.8" stroke-dasharray="3 3"/>')
    for hr in range(-6, 17, 3):
        out.append(f'<text x="{x(hr)}" y="{h - 5}" class="tick" text-anchor="middle">{hr % 24:02d}:00</text>')

    def draw(start: datetime, end: datetime, segs: list, cy: float, opacity: float = 1) -> None:
        parts = segs or [(start, end, "LIGHT")]
        for s0, s1, kind in parts:
            a0, a1 = night_hours(s0), night_hours(s1)
            if a1 < a0:
                a1 += 24
            out.append(f'<rect x="{x(a0):.1f}" y="{cy + rh * 0.2:.1f}" width="{max(x(a1) - x(a0), 0.6):.1f}" height="{rh * 0.6:.1f}" '
                       f'fill="{STAGE_COLORS.get(kind, AQUA)}" opacity="{opacity}"/>')

    for i, d in enumerate(days):
        cy = t + i * rh
        n = a["nights"].get(d)
        tag = ' <tspan class="hol">day off</tspan>' if n and n.get("holiday") else ""
        out.append(f'<text x="{l - 6}" y="{cy + rh / 2 + 3:.1f}" class="tick" text-anchor="end">{d:%a %d}{tag}</text>')
        if not n or not n["sessions"]:
            out.append(f'<rect x="{x(x0)}" y="{cy + rh * 0.2:.1f}" width="{x(x1) - x(x0):.1f}" height="{rh * 0.6:.1f}" rx="3" '
                       f'fill="none" stroke="{MUTED}" stroke-dasharray="3 3" stroke-width="0.8"/>'
                       f'<text x="{x(5)}" y="{cy + rh / 2 + 3:.1f}" class="tick" text-anchor="middle">no sleep recorded — band not worn?</text>')
            continue
        for s in n["sessions"]:
            draw(s["start"], s["end"], s["segs"], cy)
        for nap in n["naps"]:
            draw(nap["start"], nap["end"], nap["segs"], cy, 0.45)
        late = axis_hour(TARGET_WAKE[0]) - night_hours(n["wake"])
        verdict = f' · <tspan fill="{WARN_TEXT}">up {hm(late * 60)} early</tspan>' if late > 0.5 else ""
        out.append(f'<text x="{w - r + 6}" y="{cy + rh / 2 + 3:.1f}" class="val">{hm(n["asleep"])}{verdict}</text>')
    out.append("</svg>")
    return "".join(out)


def steps_chart(a: dict) -> str:
    w, h, l, r, t, b = 360, 176, 30, 6, 16, 24
    days = a["days"]
    vmax = 20_000
    bw = (w - l - r) / len(days)
    y = lambda v: t + (h - t - b) * (1 - v / vmax)
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart">',
           '<defs><linearGradient id="gBar" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="#86b6ef"/></linearGradient></defs>']
    for i, d in enumerate(days):
        if day_off(d):
            out.append(f'<rect x="{l + i * bw:.1f}" y="{t - 12}" width="{bw:.1f}" height="{h - t - b + 12}" fill="{VIOLET}" opacity="0.06"/>'
                       f'<text x="{l + i * bw + bw / 2:.1f}" y="{t - 4}" class="hol-svg" text-anchor="middle">day off</text>')
    for v in (0, 5_000, 10_000, 15_000, 20_000):
        out.append(f'<line x1="{l}" x2="{w - r}" y1="{y(v)}" y2="{y(v)}" stroke="{GRID}" stroke-width="0.6"/>'
                   f'<text x="{l - 4}" y="{y(v) + 3}" class="tick" text-anchor="end">{v // 1000}k</text>')
    best = max(days, key=lambda d: a["daily_steps"][d])
    for i, d in enumerate(days):
        v = a["daily_steps"][d]
        bx = l + i * bw + bw * 0.2
        bwi = bw * 0.6
        out.append(f'<rect x="{bx:.1f}" y="{y(v):.1f}" width="{bwi:.1f}" height="{y(0) - y(v):.1f}" rx="3" fill="url(#gBar)"/>')
        cls = "val strong" if d == best else "val"
        out.append(f'<text x="{bx + bwi / 2:.1f}" y="{y(v) - 4:.1f}" class="{cls}" text-anchor="middle">{v / 1000:.1f}k</text>')
        out.append(f'<text x="{bx + bwi / 2:.1f}" y="{h - 12}" class="tick" text-anchor="middle">{d:%a}</text>'
                   f'<text x="{bx + bwi / 2:.1f}" y="{h - 3}" class="tick sm" text-anchor="middle">{d.day} {d:%b}</text>')
    out.append(f'<line x1="{l}" x2="{w - r}" y1="{y(10_000)}" y2="{y(10_000)}" stroke="{GOOD}" stroke-width="1.2" stroke-dasharray="4 3"/>')
    out.append("</svg>")
    return "".join(out)


def heart_chart(a: dict) -> str:
    hr = a["hr"]
    w, h, l, r, t, b = 360, 176, 30, 6, 14, 18
    ylo, yhi = 40, 140
    x = lambda mins: l + (w - l - r) * mins / 1440
    y = lambda v: t + (h - t - b) * (1 - (min(max(v, ylo), yhi) - ylo) / (yhi - ylo))
    mins = lambda dt: (dt - datetime.combine(hr["day"], time())).total_seconds() / 60
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart">',
           '<defs><linearGradient id="gHr" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{ORANGE}" stop-opacity=".35"/><stop offset="1" stop-color="{ORANGE}" stop-opacity="0"/></linearGradient></defs>']
    for a0, a1 in hr["asleep_ranges"]:
        m0, m1 = max(mins(a0), 0), min(mins(a1), 1440)
        out.append(f'<rect x="{x(m0):.1f}" y="{t}" width="{x(m1) - x(m0):.1f}" height="{h - t - b}" fill="{VIOLET}" opacity="0.09"/>')
    if hr["asleep_ranges"]:
        first_sleep = min(hr["asleep_ranges"], key=lambda r: r[0])
        out.append(f'<text x="{(x(max(mins(first_sleep[0]), 0)) + x(min(mins(first_sleep[1]), 1440))) / 2:.1f}" y="{t + 9}" class="anno" fill="{VIOLET}" text-anchor="middle">asleep</text>')
    for s0, s1 in clock_spans(*SHIFT) if SHIFT else []:
        out.append(f'<rect x="{x(s0 * 60)}" y="{t}" width="{x(s1 * 60) - x(s0 * 60)}" height="{h - t - b}" fill="{ORANGE}" opacity="0.07"/>')
        if s1 - s0 >= 3:
            out.append(f'<text x="{(x(s0 * 60) + x(s1 * 60)) / 2}" y="{h - b - 4}" class="anno" fill="{WARN_TEXT}" text-anchor="middle">work</text>')
    for v in (60, 100, 140):
        out.append(f'<line x1="{l}" x2="{w - r}" y1="{y(v)}" y2="{y(v)}" stroke="{GRID}" stroke-width="0.6"/>'
                   f'<text x="{l - 4}" y="{y(v) + 3}" class="tick" text-anchor="end">{v}</text>')
    for hh in range(0, 25, 6):
        anchor = "end" if hh == 24 else "middle"
        out.append(f'<text x="{x(hh * 60)}" y="{h - 5}" class="tick" text-anchor="{anchor}">{hh:02d}:00</text>')
    pts = [(x(k * 5 + 2.5), y(v)) for k, v in hr["series"].items()]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    out.append(f'<polygon points="{pts[0][0]:.1f},{y(ylo)} {line} {pts[-1][0]:.1f},{y(ylo)}" fill="url(#gHr)"/>'
               f'<polyline points="{line}" fill="none" stroke="{ORANGE}" stroke-width="1.3" stroke-linejoin="round"/>')
    pk_t, pk_b = hr["peak"]
    out.append(f'<circle cx="{x(mins(pk_t)):.1f}" cy="{y(pk_b):.1f}" r="2.5" fill="{INK}"/>'
               f'<text x="{x(mins(pk_t)) - 5:.1f}" y="{y(pk_b) + 3:.1f}" class="val strong" text-anchor="end">peak {pk_b} bpm · {pk_t:%H:%M}</text>'
               f'<text x="{x(150)}" y="{y(hr["sleeping_low"]) + 14:.1f}" class="val strong">low {hr["sleeping_low"]:.0f} bpm</text>')
    out.append("</svg>")
    return "".join(out)


def hourly_chart(a: dict) -> str:
    w, h, l, r, t, b = 360, 164, 30, 6, 16, 18
    vals = a["hourly_avg"]
    vmax = 2_500
    bw = (w - l - r) / 24
    y = lambda v: t + (h - t - b) * (1 - min(v, vmax) / vmax)
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart">',
           '<defs><linearGradient id="gHour" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{AQUA}"/><stop offset="1" stop-color="#9fe3c9"/></linearGradient></defs>']
    bands = [(clock_spans(TARGET_BED[1], TARGET_WAKE[0]), VIOLET, VIOLET, "should be asleep")]
    if SHIFT:
        bands.append((clock_spans(*SHIFT), ORANGE, WARN_TEXT, "work"))
    for spans, fill, ink, label in bands:
        for s0, s1 in spans:
            out.append(f'<rect x="{l + s0 * bw}" y="{t}" width="{(s1 - s0) * bw}" height="{h - t - b}" fill="{fill}" opacity="0.08"/>')
        s0, s1 = max(spans, key=lambda sp: sp[1] - sp[0])
        out.append(f'<text x="{l + (s0 + s1) / 2 * bw}" y="{t - 5}" class="anno" fill="{ink}" text-anchor="middle">{label}</text>')
    for v in (0, 1_000, 2_000):
        out.append(f'<line x1="{l}" x2="{w - r}" y1="{y(v)}" y2="{y(v)}" stroke="{GRID}" stroke-width="0.6"/>'
                   f'<text x="{l - 4}" y="{y(v) + 3}" class="tick" text-anchor="end">{v / 1000:g}k</text>')
    peak = max(vals, key=vals.get)
    for hr in range(24):
        v = vals[hr]
        bx = l + hr * bw + 1.2
        out.append(f'<rect x="{bx:.1f}" y="{y(v):.1f}" width="{bw - 2.4:.1f}" height="{y(0) - y(v):.1f}" rx="2" fill="url(#gHour)"/>')
        if hr % 3 == 0:
            out.append(f'<text x="{bx + (bw - 2.4) / 2:.1f}" y="{h - 5}" class="tick" text-anchor="middle">{hr:02d}</text>')
    out.append(f'<text x="{l + peak * bw + bw / 2:.1f}" y="{y(vals[peak]) - 4:.1f}" class="val strong" text-anchor="middle">{vals[peak]:,.0f}</text>')
    out.append("</svg>")
    return "".join(out)


def workout_chart(a: dict) -> str:
    w, h, l, r, t, b = 360, 164, 30, 10, 8, 26
    ws = a["workouts"]
    xmax, ylo, yhi = 100, 80, 140
    x = lambda m: l + (w - l - r) * m / xmax
    y = lambda v: t + (h - t - b) * (1 - (v - ylo) / (yhi - ylo))
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart">',
           f'<rect x="{l}" y="{y(140)}" width="{w - l - r}" height="{y(110) - y(140)}" fill="{GOOD}" opacity="0.07"/>'
           f'<text x="{w - r - 4}" y="{y(140) + 10}" class="anno" fill="{GOOD}" text-anchor="end">fat-burn / cardio zone</text>']
    for v in (80, 100, 120, 140):
        out.append(f'<line x1="{l}" x2="{w - r}" y1="{y(v)}" y2="{y(v)}" stroke="{GRID}" stroke-width="0.6"/>'
                   f'<text x="{l - 4}" y="{y(v) + 3}" class="tick" text-anchor="end">{v}</text>')
    for m in (0, 25, 50, 75, 100):
        out.append(f'<text x="{x(m)}" y="{h - 13}" class="tick" text-anchor="middle">{m}</text>')
    out.append(f'<text x="{(l + w - r) / 2}" y="{h - 2}" class="tick" text-anchor="middle">active minutes per session → (y: avg bpm)</text>')
    for wk in sorted(ws, key=lambda w: -w["kcal"]):
        color = BLUE if wk["type"] == "WALKING" else ORANGE
        rad = 3 + (wk["kcal"] / max(w["kcal"] for w in ws)) ** 0.5 * 10
        out.append(f'<circle cx="{x(wk["minutes"]):.1f}" cy="{y(wk["hr"]):.1f}" r="{rad:.1f}" fill="{color}" fill-opacity="0.55" '
                   f'stroke="#fff" stroke-width="1.2"/>')
    labelled = []
    for wk in sorted(ws, key=lambda w: -w["kcal"])[:2]:
        lx, ly = x(wk["minutes"]), y(wk["hr"])
        if any(((lx - px) ** 2 + (ly - py) ** 2) ** 0.5 < 60 for px, py in labelled):
            continue  # would overlap the label already drawn
        labelled.append((lx, ly))
        out.append(f'<text x="{x(wk["minutes"]) - 4:.1f}" y="{y(wk["hr"]) - 13:.1f}" class="val strong" text-anchor="end">'
                   f'{wk["kcal"]} kcal · {wk["km"]:.1f} km</text>')
    out.append("</svg>")
    return "".join(out)


# ---------- icons (inline vector) ----------

ICONS = {
    "steps": '<path d="M7 3c2 0 3 2.2 3 5s-1 4-3 4-3-1.5-3-4 1-5 3-5zm-2.5 10.5h5l-.4 3a2.1 2.1 0 01-4.2 0zM17 7c2 0 3 2.2 3 5s-1 4-3 4-3-1.5-3-4 1-5 3-5zm-2.5 10.5h5l-.4 3a2.1 2.1 0 01-4.2 0z"/>',
    "moon": '<path d="M20 14.5A8.5 8.5 0 019.5 4a8.5 8.5 0 1010.5 10.5z"/><circle cx="17" cy="5" r="1.2"/><circle cx="20.5" cy="9" r=".8"/>',
    "bolt": '<path d="M13.5 2L4 13.5h6.5L9 22l11-12.5h-6.8z"/>',
    "scale": '<path d="M5 3h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2zm7 3.5a6 6 0 00-5.6 3.8l3.3 1.2A2.5 2.5 0 0112 10a2.5 2.5 0 011.3.4l2.1-2.8A6 6 0 0012 6.5z"/>',
    "check": '<path d="M12 2a10 10 0 110 20 10 10 0 010-20zm-1.4 13.6l6.3-6.3-1.4-1.4-4.9 4.9-2.3-2.3-1.4 1.4z"/>',
    "target": '<path d="M12 2a10 10 0 110 20 10 10 0 010-20zm0 3a7 7 0 100 14 7 7 0 000-14zm0 3a4 4 0 110 8 4 4 0 010-8zm0 2.5a1.5 1.5 0 100 3 1.5 1.5 0 000-3z"/>',
    "heart": '<path d="M12 21s-7.5-4.6-9.5-9.3C1 8 3.3 4.5 6.8 4.5c2 0 3.6 1.1 5.2 3 1.6-1.9 3.2-3 5.2-3 3.5 0 5.8 3.5 4.3 7.2C19.5 16.4 12 21 12 21z"/>',
    "watch": '<path d="M8 1h8l1 4.2A5 5 0 0119 9v6a5 5 0 01-2 3.8L16 23H8l-1-4.2A5 5 0 015 15V9a5 5 0 012-3.8zM9 7a2 2 0 00-2 2v6a2 2 0 002 2h6a2 2 0 002-2V9a2 2 0 00-2-2z"/>',
    "sun": '<circle cx="12" cy="12" r="4.5"/><path d="M11 1h2v4h-2zm0 18h2v4h-2zM1 11h4v2H1zm18 0h4v2h-4zM4.2 5.6l1.4-1.4 2.8 2.8-1.4 1.4zm11.4 11.4 1.4-1.4 2.8 2.8-1.4 1.4zM4.2 18.4l2.8-2.8 1.4 1.4-2.8 2.8zM15.6 7l2.8-2.8 1.4 1.4L17 8.4z"/>',
}


def icon(name: str, color: str, size: int = 18) -> str:
    return f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="{color}">{ICONS[name]}</svg>'


def bmi_gauge(a: dict) -> str:
    w, lo, hi = 150, 15, 35
    x = lambda v: w * (v - lo) / (hi - lo)
    bands = [(15, 18.5, "#86b6ef"), (18.5, 25, "#4cc596"), (25, 30, "#f2b04d"), (30, 35, "#e86f5a")]
    out = [f'<svg viewBox="0 -3 {w} 18" class="gauge">']
    for a0, a1, c in bands:
        out.append(f'<rect x="{x(a0):.1f}" y="2" width="{x(a1) - x(a0):.1f}" height="6" fill="{c}"/>')
    bx = x(a["bmi"])
    out.append(f'<path d="M{bx - 4:.1f} -3 L{bx + 4:.1f} -3 L{bx:.1f} 3 z" fill="{INK}"/>'
               f'<text x="{x(18.5):.1f}" y="15" class="tick sm" text-anchor="middle">18.5</text>'
               f'<text x="{x(25):.1f}" y="15" class="tick sm" text-anchor="middle">25</text>'
               f'<text x="{x(30):.1f}" y="15" class="tick sm" text-anchor="middle">30</text></svg>')
    return "".join(out)


def pill(ok: bool, text: str) -> str:
    return (f'<span class="pill good">{icon("check", GOOD, 8)} {text}</span>' if ok
            else f'<span class="pill warn">{icon("target", WARN, 8)} {text}</span>')


def build_html(a: dict) -> str:
    stages = a["total_stages"]
    asleep_total = sum(v for k, v in stages.items() if k != "AWAKE")
    pct = {k: 100 * stages[k] / asleep_total for k in ("DEEP", "LIGHT", "REM")}
    first, last = a["days"][0], a["days"][-1]
    n_walks = sum(w["type"] == "WALKING" for w in a["workouts"])
    span_days = len(a["days"])
    azm_week = a["azm"] / span_days * 7
    to_lose = a["weight"] - a["healthy_hi"]
    best_day = max(a["days"], key=lambda d: a["daily_steps"][d])
    bmi_label = "Healthy" if a["bmi"] < 25 else "Overweight" if a["bmi"] < 30 else "Obese"
    hr = a["hr"]
    wake_gap = axis_hour(TARGET_WAKE[0]) - a["avg_wake"]
    worst = a["worst_awake"]
    bed = axis_hour(TARGET_BED[0])
    hol_bed = min(a["holiday_beds"], key=night_hours) if a["holiday_beds"] else None
    short_sleep = a["avg_sleep"] < 420

    def legend_item(stage: str) -> str:
        return (f'<span class="lg"><i style="background:{STAGE_COLORS[stage]}"></i>{STAGE_LABELS[stage]}'
                + (f' <b>{pct[stage]:.0f}%</b>' if stage in pct else "") + "</span>")

    good, focus = [], []

    # Sleep first: it's the lever everything else depends on.
    if wake_gap > 0.5:
        focus.append(
            f"<b>You wake too early</b> — on work days you're up at ~{clock(a['avg_wake'])} on average, {hm(wake_gap * 60)} before your "
            f"{TARGET_WAKE[0]:02d}:00–{TARGET_WAKE[1]:02d}:00 target, so you average only {hm(a['avg_sleep_work'])} of sleep. Make the room pitch-dark "
            f"(blackout curtains/eye mask), use earplugs or white noise, phone on DND until {TARGET_WAKE[0]:02d}:00; if you wake early, stay in the dark.")
    elif short_sleep:
        focus.append(f"<b>Sleep more</b> — averaging {hm(a['avg_sleep'])}; only {a['nights_7h']} of {len(a['slept'])} nights reached 7h. Aim for 7–9h.")
    if a["early_bed_in_shift"]:
        beds = [clock(night_hours(n["bed"])) for n in a["early_bed_in_shift"]]
        focus.append(
            f"<b>Sleep during work hours</b> — on {', '.join(f'{n['wake']:%a}' for n in a['early_bed_in_shift'])} the band logged you asleep from "
            f"{min(beds)}–{max(beds)}, before your work ends at {SHIFT[1]:02d}:00. Dozing at work is a sleep-debt warning sign; "
            f"if you left early, still hold the {TARGET_BED[0]:02d}:00–{TARGET_BED[1]:02d}:00 bedtime.")
    if worst["awake"] >= 60:
        focus.append(
            f"<b>Broken night on {worst['wake']:%a %d}</b> — {hm(worst['awake'])} awake in bed. No caffeine after {clock(bed - 6)} "
            f"and no screens after {clock(bed - 1)}.")
    if hol_bed and abs(night_hours(hol_bed) - (bed + 0.5)) > 2:
        drift = night_hours(hol_bed) - (bed + 0.5)
        focus.append(
            f"<b>Day-off drift</b> — on a day off you fell asleep at {hol_bed:%H:%M}, ~{abs(drift):.0f}h {'later' if drift > 0 else 'earlier'} than your target. "
            f"Keep days off within ±1.5h of your work-day schedule so the next work day doesn't feel like jet lag.")

    steps_line = f"{a['avg_steps']:,.0f}/day, 10k hit on {a['goal_days']} of {span_days} days. Best: {a['daily_steps'][best_day]:,} ({best_day:%a %d})."
    if a["avg_steps"] >= 8000:
        good.append(f"<b>Steps are strong</b> — {steps_line}")
    else:
        focus.append(f"<b>Move more</b> — {steps_line} A daily 20-minute walk adds ~2,500 steps.")
    azm_line = f"{a['azm']} Active Zone Minutes (≈{azm_week:.0f}/week) against the WHO target of 150"
    if azm_week >= 150:
        good.append(f"<b>Cardio volume is excellent</b> — {azm_line}: {azm_week / 150:.1f}× across {n_walks} walks and {a['km']:.0f} km.")
    else:
        focus.append(f"<b>More cardio</b> — {azm_line}. Brisk walking counts; aim for 30 minutes on 5 days.")
    if hr:
        hr_line = (f"on {hr['day']:%a %d %b} your sleeping heart rate dropped to {hr['sleeping_low']:.0f} bpm "
                   f"(avg {hr['sleep_avg']:.0f} asleep vs {hr['awake_avg']:.0f} awake)")
        if hr["sleeping_low"] < 65:
            good.append(f"<b>Healthy heart at rest</b> — {hr_line}: a good sign of fitness and real recovery.")
        else:
            focus.append(f"<b>Resting heart rate</b> — {hr_line}. Regular cardio, less alcohol and earlier dinners usually bring it down.")
    quality = f"Deep {pct['DEEP']:.0f}% and REM {pct['REM']:.0f}% (healthy: 13–23% and 18–25%)"
    if 13 <= pct["DEEP"] <= 23 and 18 <= pct["REM"] <= 25:
        good.append(f"<b>Sleep quality is good</b> — {quality}." + (" The problem is quantity, not quality." if short_sleep else ""))
    else:
        focus.append(f"<b>Sleep quality</b> — {quality}. A cool, dark room and a fixed bedtime help most.")
    if a["bmi"] >= 25:
        if SHIFT and SHIFT[1] < SHIFT[0]:
            meals = (f"Night-work tip: main meal before work (~{(SHIFT[0] - 1) % 24:02d}:00), a light protein snack mid-shift, "
                     f"nothing heavy after {(SHIFT[1] - 2) % 24:02d}:00.")
        else:
            meals = f"Protein at every meal, and nothing heavy after {clock(bed - 3)}."
        focus.append(
            f"<b>Weight</b> — BMI {a['bmi']:.1f} ({bmi_label.lower()}); healthy for {a['height']:.0f} cm is {a['healthy_lo']:.0f}–{a['healthy_hi']:.0f} kg "
            f"(~{to_lose:.0f} kg to go at 0.5 kg/week). {meals}")
    elif a["bmi"] >= 18.5:
        good.append(f"<b>Healthy weight</b> — BMI {a['bmi']:.1f}, inside the 18.5–25 range.")
    good = good or ["<b>Data is flowing</b> — keep wearing the band day and night for a full picture."]
    focus = focus or ["<b>Nothing urgent</b> — keep doing what you're doing."]

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ width: 210mm; height: 297mm; font-family: -apple-system, "SF Pro Text", "Helvetica Neue", Inter, "Noto Sans", sans-serif; color: {INK};
  background: #f6f5f1; -webkit-print-color-adjust: exact; print-color-adjust: exact; overflow: hidden; }}
.hero {{ background: linear-gradient(120deg, #1c5cab 0%, {BLUE} 45%, {VIOLET} 100%); color: #fff; padding: 6mm 8mm 12mm; position: relative; overflow: hidden; }}
.hero h1 {{ font-size: 19pt; font-weight: 750; letter-spacing: -0.3px; }}
.hero p {{ font-size: 8.5pt; opacity: .92; margin-top: .8mm; }}
.hero .badge {{ position: absolute; right: 8mm; top: 6mm; text-align: right; font-size: 7.5pt; opacity: .95; display: flex; gap: 2.5mm; align-items: center; }}
.hero .badge b {{ display: block; font-size: 9.5pt; }}
.hero svg.wave {{ position: absolute; left: 0; bottom: -1px; width: 100%; height: 8mm; }}
.wrap {{ padding: 0 6mm; margin-top: -8mm; position: relative; }}
.kpis {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 2.4mm; }}
.kpi {{ background: #fff; border-radius: 9px; padding: 2.4mm 2.8mm; box-shadow: 0 2px 8px rgba(20,30,60,.08); }}
.kpi .top {{ display: flex; align-items: center; gap: 1.5mm; font-size: 6.6pt; color: {INK2}; font-weight: 650; text-transform: uppercase; letter-spacing: .3px; }}
.kpi .ic {{ width: 6mm; height: 6mm; border-radius: 50%; display: grid; place-items: center; flex: none; }}
.kpi .big {{ font-size: 15pt; font-weight: 750; margin-top: 1mm; letter-spacing: -.4px; white-space: nowrap; }}
.kpi .big small {{ font-size: 8pt; font-weight: 600; color: {INK2}; }}
.kpi .sub {{ margin-top: .6mm; }}
.pill {{ display: inline-flex; align-items: center; gap: 1mm; font-size: 6.4pt; font-weight: 700; padding: .3mm 1.6mm; border-radius: 99px; white-space: nowrap; }}
.pill.good {{ background: #e3f5ec; color: {GOOD}; }} .pill.warn {{ background: #fdeee4; color: {WARN_TEXT}; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2.4mm; margin-top: 2.4mm; }}
.wide {{ grid-column: 1 / -1; }}
.card {{ background: #fff; border-radius: 9px; padding: 2.4mm 3mm 1.6mm; box-shadow: 0 2px 8px rgba(20,30,60,.06); }}
.card h2 {{ font-size: 9pt; font-weight: 700; display: flex; align-items: center; gap: 1.5mm; }}
.card h2 span {{ font-weight: 500; color: {INK2}; font-size: 7pt; margin-left: auto; }}
svg.chart {{ width: 100%; display: block; margin-top: 1mm; }}
.tick {{ font-size: 7.5px; fill: {INK2}; }} .tick.sm {{ font-size: 6.5px; fill: {MUTED}; }}
.hol {{ font-size: 6px; fill: {VIOLET}; font-weight: 700; }} .hol-svg {{ font-size: 6.5px; fill: {VIOLET}; font-weight: 700; }}
.val {{ font-size: 7.5px; fill: {INK2}; font-weight: 600; }} .val.strong {{ fill: {INK}; font-weight: 750; }}
.anno {{ font-size: 7px; font-weight: 700; }}
.legend {{ display: flex; gap: 3mm; flex-wrap: wrap; font-size: 7pt; color: {INK2}; margin-left: auto; }}
.lg {{ display: inline-flex; align-items: center; gap: 1.1mm; }} .lg i {{ width: 2.4mm; height: 2.4mm; border-radius: 2px; display: inline-block; }}
.lg b {{ color: {INK}; }}
svg.gauge {{ width: 100%; margin-top: .6mm; }}
.insights {{ display: grid; grid-template-columns: 1fr 1.25fr; gap: 2.4mm; margin-top: 2.4mm; }}
.ins {{ border-radius: 9px; padding: 2.6mm 3.2mm 1.6mm; }}
.ins.good {{ background: linear-gradient(135deg, #e6f7ef, #f3fbf7); border: 1px solid #bfe6d2; }}
.ins.warn {{ background: linear-gradient(135deg, #fdeee4, #fff7f1); border: 1px solid #f5ccb0; }}
.ins h3 {{ font-size: 9.5pt; display: flex; align-items: center; gap: 1.5mm; margin-bottom: 1.2mm; }}
.ins.good h3 {{ color: {GOOD}; }} .ins.warn h3 {{ color: {WARN_TEXT}; }}
.ins ul {{ list-style: none; }} .ins li {{ font-size: 7pt; line-height: 1.38; color: {INK2}; padding-left: 3.2mm; position: relative; margin-bottom: 1mm; }}
.ins li b {{ color: {INK}; }}
.ins li::before {{ content: ""; position: absolute; left: 0; top: 1.3mm; width: 1.5mm; height: 1.5mm; border-radius: 50%; }}
.ins.good li::before {{ background: {GOOD}; }} .ins.warn li::before {{ background: {WARN}; }}
.empty {{ font-size: 7.5pt; color: {MUTED}; padding: 20mm 0; text-align: center; }}
footer {{ font-size: 6.3pt; color: {MUTED}; text-align: center; margin-top: 1.6mm; }}
</style></head><body>
<div class="hero">
  <h1>Your weekly health report</h1>
  <p>{first:%d %b} – {last:%d %b %Y} · {span_days} days{f" · work {SHIFT[0]:02d}:00–{SHIFT[1]:02d}:00" if SHIFT else ""} · target sleep {TARGET_BED[0]:02d}–{TARGET_BED[1]:02d}h → {TARGET_WAKE[0]:02d}–{TARGET_WAKE[1]:02d}h</p>
  <div class="badge">{icon("watch", "#fff", 24)}<div><b>{a['device']}</b>{a['weight']:.0f} kg · {a['height']:.0f} cm</div></div>
  <svg class="wave" viewBox="0 0 800 40" preserveAspectRatio="none"><path d="M0 22 C150 40 300 5 450 18 S700 34 800 14 V40 H0z" fill="#f6f5f1"/></svg>
</div>
<div class="wrap">
  <div class="kpis">
    <div class="kpi"><div class="top"><div class="ic" style="background:#e3eefb">{icon("steps", BLUE, 13)}</div>Daily steps</div>
      <div class="big">{a['avg_steps']:,.0f}</div><div class="sub">{pill(a['avg_steps'] >= 8000, f"{a['goal_days']}/{span_days} days ≥10k")}</div></div>
    <div class="kpi"><div class="top"><div class="ic" style="background:#ecebfb">{icon("moon", VIOLET, 13)}</div>Avg sleep</div>
      <div class="big">{int(a['avg_sleep'] // 60)}<small>h</small> {round(a['avg_sleep'] % 60):02d}<small>m</small></div>
      <div class="sub">{pill(not short_sleep, f"goal 7–9h · {a['nights_7h']}/{len(a['slept'])} nights")}</div></div>
    <div class="kpi"><div class="top"><div class="ic" style="background:#fff3dc">{icon("sun", "#c98500", 13)}</div>Wake · work days</div>
      <div class="big">{clock(a['avg_wake'])}</div><div class="sub">{pill(wake_gap <= 0.5, f"target {TARGET_WAKE[0]:02d}–{TARGET_WAKE[1]:02d}h")}</div></div>
    <div class="kpi"><div class="top"><div class="ic" style="background:#fdeee4">{icon("heart", ORANGE, 13)}</div>Sleeping HR</div>
      <div class="big">{f"{hr['sleeping_low']:.0f}<small> bpm</small>" if hr else "–"}</div>
      <div class="sub">{pill(hr['sleeping_low'] < 65, f"lowest · {hr['day']:%d %b}") if hr else ""}</div></div>
    <div class="kpi"><div class="top"><div class="ic" style="background:#fdeee4">{icon("scale", ORANGE, 13)}</div>BMI</div>
      <div class="big">{a['bmi']:.1f}<small> {bmi_label}</small></div>{bmi_gauge(a)}</div>
  </div>

  <div class="grid">
    <div class="card wide"><h2>{icon("moon", VIOLET, 12)} When you actually slept vs your target
      <div class="legend">{legend_item("DEEP")}{legend_item("LIGHT")}{legend_item("REM")}{legend_item("AWAKE")}<span class="lg">faded = nap</span></div></h2>{sleep_timeline(a)}</div>
    <div class="card"><h2>{icon("steps", BLUE, 12)} Daily steps <span>dashed = 10k goal</span></h2>{steps_chart(a)}</div>
    <div class="card"><h2>{icon("heart", ORANGE, 12)} Heart rate{f" · {hr['day']:%a %d %b} <span>5-min average, to {hr['until']:%H:%M}</span></h2>{heart_chart(a)}" if hr else "</h2><p class='empty'>No full day of heart-rate data in this period.</p>"}</div>
    <div class="card"><h2>{icon("bolt", AQUA, 12)} When you move <span>{a['window_steps']:,.0f} steps/day inside your sleep window</span></h2>{hourly_chart(a)}</div>
    <div class="card"><h2>{icon("bolt", BLUE, 12)} Workouts <span><span class="lg"><i style="background:{BLUE};border-radius:50%"></i>Walk {n_walks}</span>
      &nbsp;<span class="lg"><i style="background:{ORANGE};border-radius:50%"></i>Cardio {len(a['workouts']) - n_walks}</span> · size = kcal</span></h2>{workout_chart(a)}</div>
  </div>

  <div class="insights">
    <div class="ins good"><h3>{icon("check", GOOD, 15)} What's going well</h3><ul>{"".join(f"<li>{x}</li>" for x in good)}</ul></div>
    <div class="ins warn"><h3>{icon("target", WARN, 15)} What to fix — no excuses</h3><ul>{"".join(f"<li>{x}</li>" for x in focus)}</ul></div>
  </div>
  <footer>Generated {date.today():%d %b %Y} from the Google Health API · {a['device']} data only (other sources excluded to avoid double counting) · heart rate shown for one day · not medical advice.</footer>
</div>
</body></html>"""


def generate(weight: float, height_cm: float, end: date | None = None) -> Path:
    """Render the report for the REPORT_DAYS days before `end` (default: today) and return the PDF path."""
    global window
    end = end or date.today()
    window = (end - timedelta(days=REPORT_DAYS), end)
    a = analyse(weight, height_cm)
    OUT_DIR.mkdir(exist_ok=True)
    html = OUT_DIR / "fitbit-report.html"
    pdf = OUT_DIR / f"fitbit-report-{end - timedelta(days=1):%Y-%m-%d}.pdf"
    html.write_text(build_html(a))
    chrome = next(c for c in (p if Path(p).exists() else shutil.which(p) for p in CHROMES) if c)
    # Chromium refuses to run as root (inside the container) without --no-sandbox.
    sandbox = ["--no-sandbox"] if os.geteuid() == 0 else []
    subprocess.run([chrome, "--headless", "--disable-gpu", *sandbox, "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf.resolve()}", html.resolve().as_uri()],
                   check=True, capture_output=True)
    return pdf


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weight", type=float, required=True, help="kg")
    parser.add_argument("--height", type=float, required=True, help="cm")
    parser.add_argument("--end", type=date.fromisoformat, help="first day NOT included (default: today)")
    args = parser.parse_args()
    print(f"Wrote {generate(args.weight, args.height, args.end)}")


if __name__ == "__main__":
    main()
