"""Keep a local SQLite copy of your Google Health data in sync.

Commands:
    uv run healthsync.py auth        # one-time browser sign-in (needs a machine with a browser)
    uv run healthsync.py catchup     # fetch everything since the last sync, with a 48h overlap
    uv run healthsync.py worker      # fetch the ranges named in queued webhook notifications
    uv run healthsync.py serve       # webhook receiver + scheduler (what the container runs)
    uv run healthsync.py subscribe   # register the webhook with Google (one-time, uses gcloud)
"""

import argparse
import base64
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fetch import BASE_URL, SCOPES, TOKEN_FILE, time_filter_field

load_dotenv(".env")  # settings below are read at import time; Docker passes them as env vars instead

DB_FILE = Path(os.environ.get("HEALTH_DB", "data/health.db"))
WEARABLES = "users/me/dataSourceFamilies/google-wearables"
# Data types kept as individual data points, and the source family to request
# (Fitbit only where phones/other watches would double count; all sources otherwise —
# sleep and exercise reject the family filter, so the report filters those by device).
LIST_TYPES = {
    "steps": WEARABLES,
    "sleep": None,
    "exercise": None,
    "daily-resting-heart-rate": None,
    "daily-heart-rate-variability": None,
    "weight": None,
    "body-fat": None,
}
# Raw heart rate is ~35k samples/day, so it is only ever fetched as 5-minute rollups.
PAGE_SIZE = {"sleep": 25, "exercise": 25}  # API caps these two; everything else allows 10k
HR_WINDOW_SECONDS = 300
HR_MAX_RANGE = timedelta(days=14)  # API limit for heart-rate rollUp
BACKFILL_DAYS = 30
OVERLAP_DAYS = 2  # sleep and daily summaries are revised for a while after the fact
WORKER_EVERY = timedelta(minutes=15)
CATCHUP_MAX_GAP = timedelta(hours=26)  # run immediately if a power cut skipped one
REPORT_WEEKDAY = 0  # Monday, an hour after the catch-up


def catchup_hour() -> int:
    # A couple of hours after you usually wake, so last night's sleep has been processed.
    return int(os.environ.get("CATCHUP_HOUR", "10"))

log = logging.getLogger("healthsync")


# ---------- storage ----------

def db() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS data_points (
            key TEXT PRIMARY KEY, data_type TEXT NOT NULL, civil_date TEXT NOT NULL,
            payload TEXT NOT NULL, fetched_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS data_points_type_date ON data_points (data_type, civil_date);
        CREATE TABLE IF NOT EXISTS heart_rate_5min (
            start_utc TEXT PRIMARY KEY, end_utc TEXT NOT NULL,
            bpm_avg REAL, bpm_min INTEGER, bpm_max INTEGER);
        CREATE TABLE IF NOT EXISTS webhook_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL,
            payload TEXT NOT NULL, processed_at TEXT);
        CREATE TABLE IF NOT EXISTS sync_state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    return conn


def get_state(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, name: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO sync_state (name, value) VALUES (?, ?)", (name, value))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------- API ----------

def credentials() -> Credentials:
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        # Write atomically so a power cut can't leave a half-written token file.
        tmp = TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(creds.to_json())
        tmp.replace(TOKEN_FILE)
    return creds


def api_session() -> requests.Session:
    session = requests.Session()
    # Retry rate limits, server errors and network blips (e.g. the router still booting after a power cut).
    retry = Retry(total=6, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=None, respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["Authorization"] = f"Bearer {credentials().token}"
    return session


def camel(data_type: str) -> str:
    first, *rest = data_type.split("-")
    return first + "".join(p.title() for p in rest)


def local_date(ts: str, offset: str) -> date:
    utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (utc + timedelta(seconds=float(offset.rstrip("s")))).date()


def civil_date(data_type: str, point: dict) -> str:
    """The local date the API filters this point on (see fetch.time_filter_field)."""
    body = point[camel(data_type)]
    if "date" in body:
        d = body["date"]
        return date(d["year"], d["month"], d["day"]).isoformat()
    if "sampleTime" in body:
        st = body["sampleTime"]
        return local_date(st["physicalTime"], st.get("utcOffset", "0s")).isoformat()
    iv = body["interval"]
    if data_type == "sleep":
        return local_date(iv["endTime"], iv.get("endUtcOffset", "0s")).isoformat()
    return local_date(iv["startTime"], iv.get("startUtcOffset", "0s")).isoformat()


def point_key(data_type: str, point: dict) -> str:
    if point.get("name"):
        return point["name"]
    body = point[camel(data_type)]
    when = body.get("interval") or body.get("sampleTime") or body.get("date")
    return f"{data_type}:{json.dumps(when, sort_keys=True)}"


def list_points(session: requests.Session, data_type: str, start: date, end: date | None) -> list[dict]:
    field = time_filter_field(data_type)
    fmt = (lambda d: d.isoformat()) if field.endswith(".date") else (lambda d: f"{d.isoformat()}T00:00:00")
    flt = f'{field} >= "{fmt(start)}"' + (f' AND {field} < "{fmt(end)}"' if end else "")
    params = {"filter": flt, "pageSize": PAGE_SIZE.get(data_type, 10_000)}
    if LIST_TYPES[data_type]:
        params["dataSourceFamily"] = LIST_TYPES[data_type]
    points = []
    while True:
        resp = session.get(f"{BASE_URL}/dataTypes/{data_type}/dataPoints", params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        points.extend(body.get("dataPoints", []))
        if not body.get("nextPageToken"):
            return points
        params["pageToken"] = body["nextPageToken"]


def sync_range(conn: sqlite3.Connection, session: requests.Session, data_type: str, start: date, end: date | None) -> int:
    """Replace everything stored for [start, end) with what the API has now, so deletions are picked up too."""
    points = list_points(session, data_type, start, end)
    fetched = now_utc().isoformat()
    with conn:
        conn.execute("DELETE FROM data_points WHERE data_type = ? AND civil_date >= ? AND civil_date < ?",
                     (data_type, start.isoformat(), (end or date.max).isoformat()))
        conn.executemany(
            "INSERT OR REPLACE INTO data_points (key, data_type, civil_date, payload, fetched_at) VALUES (?, ?, ?, ?, ?)",
            [(point_key(data_type, p), data_type, civil_date(data_type, p), json.dumps(p), fetched) for p in points])
    log.info("%s %s..%s: %d points", data_type, start, end or "now", len(points))
    return len(points)


def sync_heart_rate(conn: sqlite3.Connection, session: requests.Session, start: datetime, end: datetime) -> int:
    total = 0
    while start < end:
        chunk_end = min(start + HR_MAX_RANGE, end)
        body = {"range": {"startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endTime": chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ")},
                "windowSize": f"{HR_WINDOW_SECONDS}s", "dataSourceFamily": WEARABLES, "pageSize": 10_000}
        rows = []
        while True:
            resp = session.post(f"{BASE_URL}/dataTypes/heart-rate/dataPoints:rollUp", json=body, timeout=60)
            resp.raise_for_status()
            page = resp.json()
            for p in page.get("rollupDataPoints", []):
                hr = p["heartRate"]
                rows.append((p["startTime"], p["endTime"], hr.get("beatsPerMinuteAvg"), hr.get("beatsPerMinuteMin"), hr.get("beatsPerMinuteMax")))
            if not page.get("nextPageToken"):
                break
            body["pageToken"] = page["nextPageToken"]
        with conn:
            conn.executemany("INSERT OR REPLACE INTO heart_rate_5min VALUES (?, ?, ?, ?, ?)", rows)
        total += len(rows)
        start = chunk_end
    log.info("heart-rate rollup: %d windows", total)
    return total


# ---------- jobs ----------

def catchup() -> None:
    conn = db()
    session = api_session()
    today = date.today()
    for data_type in LIST_TYPES:
        mark = get_state(conn, f"watermark:{data_type}")
        start = (date.fromisoformat(mark) - timedelta(days=OVERLAP_DAYS)) if mark else today - timedelta(days=BACKFILL_DAYS)
        sync_range(conn, session, data_type, start, None)
        with conn:
            set_state(conn, f"watermark:{data_type}", today.isoformat())

    end = now_utc().replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % (HR_WINDOW_SECONDS // 60))
    mark = get_state(conn, "watermark:heart-rate")
    start = (datetime.fromisoformat(mark) - timedelta(days=OVERLAP_DAYS)) if mark else end - timedelta(days=BACKFILL_DAYS)
    sync_heart_rate(conn, session, start, end)
    with conn:
        set_state(conn, "watermark:heart-rate", end.isoformat())
        set_state(conn, "last_catchup", now_utc().isoformat())
        conn.execute("DELETE FROM webhook_queue WHERE processed_at < ?", ((now_utc() - timedelta(days=30)).isoformat(),))


def worker() -> None:
    conn = db()
    rows = conn.execute("SELECT id, payload FROM webhook_queue WHERE processed_at IS NULL ORDER BY id").fetchall()
    if not rows:
        return
    # Coalesce: many notifications for the same type become one fetch of the combined date range.
    ranges: dict[str, list[date]] = {}
    for _, payload in rows:
        msg = json.loads(payload)
        data_type = str(msg.get("dataType", "")).lower().replace("_", "-")
        if data_type in ("user-revoked-access", "user-deleted"):
            alert(f"Google Health: {data_type} received — re-run `healthsync.py auth`")
            continue
        if data_type not in LIST_TYPES:
            log.info("ignoring notification for %s", data_type or msg)
            continue
        dates = [date.fromisoformat(d) for d in re.findall(r"\d{4}-\d{2}-\d{2}", json.dumps(msg.get("intervals", [])))]
        dates = dates or [date.today() - timedelta(days=1), date.today()]
        lo, hi = ranges.get(data_type, [min(dates), max(dates)])
        ranges[data_type] = [min(lo, *dates), max(hi, *dates)]

    session = api_session()
    for data_type, (lo, hi) in ranges.items():
        sync_range(conn, session, data_type, lo, hi + timedelta(days=1))
    with conn:
        conn.executemany("UPDATE webhook_queue SET processed_at = ? WHERE id = ?",
                         [(now_utc().isoformat(), row_id) for row_id, _ in rows])
    log.info("worker: %d notifications -> %d fetches", len(rows), len(ranges))


REPORT_LOCK = threading.Lock()  # report.generate shares module state and an HTML scratch file
REPORT_CACHE = timedelta(minutes=15)
_live_report: tuple[datetime, Path] | None = None


def build_report() -> Path:
    import report
    if not (os.environ.get("WEIGHT_KG") and os.environ.get("HEIGHT_CM")):
        raise RuntimeError("set WEIGHT_KG and HEIGHT_CM in .env to get reports")
    with REPORT_LOCK:
        return report.generate(float(os.environ["WEIGHT_KG"]), float(os.environ["HEIGHT_CM"]))


def weekly_report() -> None:
    path = build_report()
    alert(f"Weekly health report ready: {path.name}", priority="default")


def live_report() -> Path:
    """The last-7-days report, rebuilt at most every REPORT_CACHE so page reloads stay instant."""
    global _live_report
    if _live_report and datetime.now() - _live_report[0] < REPORT_CACHE and _live_report[1].exists():
        return _live_report[1]
    path = build_report()
    _live_report = (datetime.now(), path)
    return path


# ---------- alerting ----------

_last_alert: dict[str, float] = {}


def alert(message: str, priority: str = "high") -> None:
    """Push to ntfy (if NTFY_TOPIC is set), at most once an hour per message."""
    log.warning("ALERT: %s", message)
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or time.time() - _last_alert.get(message, 0) < 3600:
        return
    _last_alert[message] = time.time()
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode(), headers={"Priority": priority, "Title": "Health sync"}, timeout=10)
    except requests.RequestException as e:
        log.error("ntfy failed: %s", e)


def heartbeat() -> None:
    """Ping a dead-man's switch (e.g. healthchecks.io) so you hear about it when the Mini is down."""
    url = os.environ.get("HEARTBEAT_URL")
    if url:
        try:
            requests.get(url, timeout=10)
        except requests.RequestException as e:
            log.error("heartbeat failed: %s", e)


# ---------- webhook receiver + scheduler ----------

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._reply(200, b"ok")
        if path in ("/", "/report.pdf"):
            return self._report()
        self._reply(404)

    def _report(self) -> None:
        password = os.environ.get("REPORT_PASSWORD")
        if not password:
            return self._reply(404)  # the report is never public: it's off until a password is set
        user = os.environ.get("REPORT_USER", "me")
        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Health report", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            pdf = live_report()
        except (Exception, SystemExit) as e:  # report.analyse exits when there isn't enough data yet
            log.exception("report failed")
            return self._reply(503, f"Report unavailable: {e}".encode())
        body = pdf.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{pdf.name}"')
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/webhook":
            return self._reply(404)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not os.environ.get("WEBHOOK_SECRET"):
            return self._reply(404)  # polling-only mode
        expected = f"Bearer {os.environ['WEBHOOK_SECRET']}"
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            return self._reply(401)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._reply(400)
        if isinstance(data, dict) and data.get("type") == "verification":
            return self._reply(201)
        messages = data if isinstance(data, list) else [data]
        conn = db()
        with conn:
            conn.executemany("INSERT INTO webhook_queue (received_at, payload) VALUES (?, ?)",
                             [(now_utc().isoformat(), json.dumps(m)) for m in messages])
        conn.close()
        # Reply immediately; the worker fetches the data later.
        self._reply(204)

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        log.info("http %s", fmt % args)


def run_job(name: str, job) -> bool:
    try:
        job()
        return True
    except Exception as e:  # keep the scheduler alive whatever happens
        log.exception("%s failed", name)
        alert(f"{name} failed: {type(e).__name__}: {e}"[:300])
        return False


def scheduler() -> None:
    last_worker = datetime.min
    while True:
        now = datetime.now()
        if now - last_worker >= WORKER_EVERY:
            run_job("worker", worker)
            last_worker = now

        conn = db()
        last = get_state(conn, "last_catchup")
        last_dt = datetime.fromisoformat(last) if last else None
        due_today = now.hour >= catchup_hour() and (not last_dt or last_dt.astimezone().date() < now.date())
        poll = timedelta(hours=float(os.environ.get("POLL_EVERY_HOURS") or 0))
        overdue = not last_dt or now_utc() - last_dt > (poll or CATCHUP_MAX_GAP)
        if (due_today or overdue) and run_job("catchup", catchup):
            heartbeat()

        last_report = get_state(conn, "last_report")
        if now.weekday() == REPORT_WEEKDAY and now.hour >= catchup_hour() + 1 and last_report != now.date().isoformat():
            if run_job("report", weekly_report):
                with conn:
                    set_state(conn, "last_report", now.date().isoformat())
        conn.close()
        time.sleep(60)


def serve() -> None:
    if not os.environ.get("WEBHOOK_SECRET"):
        log.info("WEBHOOK_SECRET not set: polling only (daily catch-up%s)",
                 f" + every {os.environ['POLL_EVERY_HOURS']}h" if os.environ.get("POLL_EVERY_HOURS") else "")
    threading.Thread(target=scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", "18787"))
    log.info("listening on :%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler).serve_forever()


def subscribe() -> None:
    """Register (or update) the webhook. Needs `gcloud auth login` as a project owner."""
    project = os.environ["GOOGLE_PROJECT_NUMBER"]
    subscriber_id = os.environ.get("SUBSCRIBER_ID", "health-sync")
    token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
    headers = {"Authorization": f"Bearer {token}", "x-goog-user-project": project}
    payload = {
        "endpointUri": os.environ["WEBHOOK_URL"],
        "subscriberConfigs": [{"dataTypes": list(LIST_TYPES), "subscriptionCreatePolicy": "AUTOMATIC"}],
        "endpointAuthorization": {"secret": f"Bearer {os.environ['WEBHOOK_SECRET']}"},
    }
    url = f"https://health.googleapis.com/v4/projects/{project}/subscribers"
    resp = requests.post(url, params={"subscriberId": subscriber_id}, json=payload, headers=headers, timeout=60)
    if resp.status_code == 409:
        resp = requests.patch(f"{url}/{subscriber_id}", json=payload, headers=headers, timeout=60,
                              params={"updateMask": "endpointUri,subscriberConfigs,endpointAuthorization"})
    print(resp.status_code, resp.text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["auth", "catchup", "worker", "serve", "subscribe"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "auth":
        from fetch import get_credentials
        get_credentials()
        print(f"Saved {TOKEN_FILE}")
    else:
        {"catchup": catchup, "worker": worker, "serve": serve, "subscribe": subscribe}[args.command]()


if __name__ == "__main__":
    main()
