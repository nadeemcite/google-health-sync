"""HTTP routes for the web app: the page, the report, and a small JSON API for the assistant and weight log.

`route` is called by healthsync's request handler for everything that isn't /healthz or /webhook, and
returns (status, content type, body). The caller owns auth and the database connection.
"""

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import assistant
import metrics

log = logging.getLogger("healthsync")

WEB_DIR = Path(__file__).parent / "web"
REPORT_CACHE = timedelta(minutes=15)
_report_lock = threading.Lock()  # report.generate_html shares module state
_report_page: tuple[datetime, tuple, str] | None = None  # (built at, weight and height it used, html)

# Public static files: the page icons. Only these are served from web/, never a user-supplied path.
STATIC = {"/favicon.svg": ("favicon.svg", "image/svg+xml"), "/favicon.ico": ("favicon.svg", "image/svg+xml"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png")}

Response = tuple[int, str, bytes]
sync_hooks = None  # (start, status) pairs, set by healthsync's request handler
diet_hooks = None


def json_response(data, status: int = 200) -> Response:
    return status, "application/json", json.dumps(data).encode()


def error(message: str, status: int = 400) -> Response:
    return json_response({"error": message}, status)


def report_html(conn: sqlite3.Connection) -> str:
    """The last-7-days report as a web page, rebuilt at most every REPORT_CACHE."""
    global _report_page
    import report
    weight, height = metrics.current_weight(conn), metrics.get_profile(conn)["height_cm"]
    if not weight or not height:
        raise RuntimeError("log your weight and set your height first")
    last_sync = conn.execute("SELECT value FROM sync_state WHERE name = 'last_catchup'").fetchone()
    inputs = (weight["kg"], height, last_sync[0] if last_sync else None)  # new data or weight means a fresh report
    with _report_lock:
        if _report_page and _report_page[1] == inputs and datetime.now() - _report_page[0] < REPORT_CACHE:
            return _report_page[2]
        html = report.generate_html(weight["kg"], height)
        _report_page = (datetime.now(), inputs, html)
        return html


def state(conn: sqlite3.Connection) -> dict:
    profile = metrics.get_profile(conn)
    weight = metrics.current_weight(conn)
    last_sync = conn.execute("SELECT value FROM sync_state WHERE name = 'last_catchup'").fetchone()
    series = metrics.weight_series(conn, 120)
    return {
        "profile": {**profile, "weight_kg": weight["kg"] if weight else None,
                    "weight_source": weight["source"] if weight else None, "weight_at": weight["at"] if weight else None,
                    "bmi": round(metrics.bmi(weight["kg"], profile["height_cm"]), 1) if weight and profile["height_cm"] else None},
        "weights": series,
        "weekly_trend_kg": metrics.weekly_slope(series[-20:]),
        "last_sync": last_sync[0] if last_sync else None,
        "sync": sync_hooks[1]() if sync_hooks else {"running": False, "error": None},
    }


def messages(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, ts, role, content FROM chat_messages WHERE session = ? ORDER BY id DESC LIMIT 200",
                        (assistant.current_session(conn),)).fetchall()[::-1]
    return [{"id": i, "ts": ts, "role": role, "content": content} for i, ts, role, content in rows]


def diet(conn: sqlite3.Connection) -> dict:
    profile = metrics.get_profile(conn)
    weight = metrics.current_weight(conn)
    advice, advice_stale = assistant.current_advice(conn) if assistant.plan_inputs(conn) else (None, False)
    today = metrics.food_log(conn, 1)
    return {"prefs": {k: profile[k] for k in ("diet_style", "diet_avoid", "kcal_target", "goal_weight_kg")},
            "target": metrics.diet_target(weight["kg"], profile["height_cm"], profile["goal_weight_kg"]) if weight and profile["height_cm"] else None,
            "routine": metrics.routine(), "lifestyle": assistant.lifestyle_memory(),
            "plan": assistant.saved_plan(conn), "plan_stale": assistant.plan_is_stale(conn),
            "now": advice, "now_stale": advice_stale,
            "refresh": diet_hooks[1]() if diet_hooks else {"running": False, "error": None},
            "today": today, "today_kcal": sum(f["kcal"] or 0 for f in today),
            "unknown_kcal": sum(f["kcal"] is None for f in today)}


def number(body: dict, key: str, lo: float, hi: float) -> float | None:
    if body.get(key) in (None, ""):
        return None
    value = float(body[key])
    if not lo <= value <= hi:
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    return value


def assistant_call(fn, *args) -> Response | None:
    """Run an assistant action, turning a missing key or an API failure into a message the page can show."""
    try:
        return fn(*args)
    except Exception as e:
        log.exception("assistant failed")
        return error(f"The assistant is unavailable ({type(e).__name__}). Check ASSISTANT_MODEL and the provider key in .env: {e}"[:300], 502)


def route(method: str, path: str, body: bytes, conn: sqlite3.Connection) -> Response:
    if method == "GET":
        if path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", (WEB_DIR / "index.html").read_bytes()
        if path in STATIC:
            name, ctype = STATIC[path]
            return 200, ctype, (WEB_DIR / name).read_bytes()
        if path == "/report":
            try:
                return 200, "text/html; charset=utf-8", report_html(conn).encode()
            except (Exception, SystemExit) as e:  # report.analyse exits when there isn't enough data yet
                log.exception("report failed")
                return 503, "text/plain; charset=utf-8", f"Report unavailable: {e}".encode()
        if path == "/api/state":
            return json_response(state(conn))
        if path == "/api/chat":
            return json_response({"messages": messages(conn)})
        if path == "/api/diet":
            return json_response(diet(conn))
        if path == "/api/memory":
            return json_response({"markdown": assistant.read_memory()})
        return 404, "text/plain", b""

    if method == "POST":
        try:
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            if path == "/api/briefing":
                return assistant_call(lambda: json_response({"created": assistant.briefing(conn), "messages": messages(conn)}))
            if path == "/api/sync":
                started = sync_hooks[0]() if sync_hooks else False
                return json_response({"started": started, **state(conn)}, 202 if started else 200)
            if path == "/api/new-chat":
                assistant.new_session(conn)
                return json_response({"messages": messages(conn)})
            if path == "/api/chat":
                text = str(data.get("message", "")).strip()
                if not text:
                    return error("message is empty")
                return assistant_call(lambda: json_response({"reply": assistant.chat(conn, text[:4000])}))
            if path == "/api/weight":
                kg = number(data, "kg", 20, 400)
                if kg is None:
                    return error("kg is required")
                when = None
                if data.get("date"):
                    day = date.fromisoformat(data["date"])
                    if day > date.today():
                        return error("date is in the future")
                    when = datetime.combine(day, datetime.now().time()) if day != date.today() else None
                metrics.log_weight(conn, kg, when, str(data.get("note", ""))[:200])
                return json_response(state(conn))
            if path == "/api/profile":
                values = {k: v for k, v in (("height_cm", number(data, "height_cm", 80, 260)),
                                            ("goal_weight_kg", number(data, "goal_weight_kg", 20, 400)),
                                            ("kcal_target", number(data, "kcal_target", 800, 6000))) if v is not None}
                for key in ("diet_style", "diet_avoid"):
                    if key in data:
                        values[key] = str(data[key]).strip()[:200]
                for key in ("goal_weight_kg", "kcal_target"):
                    if data.get(key) == "":
                        values[key] = ""  # emptied in the form: go back to deriving it
                metrics.set_profile(conn, **values)
                return json_response({**state(conn), "diet": diet(conn)})
            if path == "/api/food":
                text = str(data.get("text", "")).strip()
                if not text:
                    return error("describe what you ate")
                kcal = number(data, "kcal", 0, 5000)
                metrics.log_food(conn, text[:300], int(kcal) if kcal is not None else None)
                return json_response(diet(conn))
            if path == "/api/food-delete":
                with conn:
                    conn.execute("DELETE FROM food_log WHERE id = ?", (int(data.get("id", 0)),))
                return json_response(diet(conn))
            if path == "/api/diet-refresh":
                if diet_hooks:
                    diet_hooks[0](bool(data.get("full")))
                return json_response(diet(conn))
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return error(str(e))
        return 404, "text/plain", b""

    return 405, "text/plain", b""
