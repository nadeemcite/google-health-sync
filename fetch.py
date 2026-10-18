"""One-off export of raw Google Health API data to data/<type>.json (handy for exploring the API).

For continuous syncing use healthsync.py instead. Avoid --types heart-rate here: raw heart rate
is ~35k samples per day; healthsync.py stores 5-minute rollups instead.

Usage:
    uv run fetch.py                      # last 7 days of steps, sleep, exercise
    uv run fetch.py --days 30 --types steps weight
"""

import argparse
import json
import os
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_URL = "https://health.googleapis.com/v4/users/me"
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
]
TOKEN_FILE = Path("credentials/token.json")
OUTPUT_DIR = Path("data")
SAMPLE_TYPES = {"weight", "body_fat", "blood_glucose", "heart_rate"}


REDIRECT = "http://localhost:8080"  # a Desktop client accepts any localhost redirect, with no server needed for the paste flow


def can_open_browser() -> bool:
    """False on servers: no display, or no browser the standard library can launch."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        webbrowser.get()
        return True
    except webbrowser.Error:
        return False


def sign_in_by_paste(flow: InstalledAppFlow) -> Credentials:
    """Sign-in for machines without a browser: open the link on any device, then paste back what the browser ends up on.

    After you approve, Google redirects to http://localhost:8080/?code=... Nothing is listening there on your other device, so the
    page shows an error. That is expected: copy the full address from the address bar (or just the code= value) and paste it here.
    """
    flow.redirect_uri = REDIRECT
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("\nNo browser on this machine. Sign in from any device:\n")
    print(f"  1. Open this link in a browser (your laptop or phone):\n\n{url}\n")
    print("  2. Approve access. Your browser then shows 'This site can't be reached' on localhost:8080. That is expected.")
    print("  3. Copy the FULL address from the address bar (it starts with http://localhost:8080/?state=...) and paste it below.\n")
    pasted = input("Paste here: ").strip()
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # the redirect is plain http://localhost, as in run_local_server
    if pasted.startswith("http"):
        flow.fetch_token(authorization_response=pasted)
    else:
        flow.fetch_token(code=pasted)  # they pasted just the code= value
    return flow.credentials


def get_credentials(headless: bool = False) -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            # Refresh tokens expire after 7 days while the consent screen is in Testing mode.
            creds = None
    if not creds or not creds.valid:
        client_config = {
            "installed": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        if headless or not can_open_browser():
            creds = sign_in_by_paste(flow)
        else:
            creds = flow.run_local_server(
                port=8080, redirect_uri_trailing_slash=False, access_type="offline", prompt="consent"
            )
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def time_filter_field(data_type: str) -> str:
    # Filterable time field depends on record kind: https://developers.google.com/health/filters
    # Path uses hyphens (body-fat); filter field uses underscores (body_fat).
    name = data_type.replace("-", "_")
    if name.startswith("daily_"):
        return f"{name}.date"
    if name == "sleep":
        return "sleep.interval.civil_end_time"
    if name in SAMPLE_TYPES:
        return f"{name}.sample_time.civil_time"
    return f"{name}.interval.civil_start_time"


def list_data_points(session: requests.Session, data_type: str, since: date) -> list[dict]:
    params = {"filter": f'{time_filter_field(data_type)} >= "{since.isoformat()}"'}
    points = []
    while True:
        resp = session.get(f"{BASE_URL}/dataTypes/{data_type}/dataPoints", params=params)
        if not resp.ok:
            print(f"  {data_type}: HTTP {resp.status_code} {resp.text[:300]}")
            break
        body = resp.json()
        points.extend(body.get("dataPoints", []))
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    return points


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--types", nargs="+", default=["steps", "sleep", "exercise"])
    args = parser.parse_args()
    load_dotenv()

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {get_credentials().token}"

    identity = session.get(f"{BASE_URL}/identity")
    if not identity.ok:
        # ACCOUNT_NOT_LINKED means you haven't set up a profile in the Google Health app.
        raise SystemExit(f"Identity check failed: HTTP {identity.status_code} {identity.text}")
    print(f"Linked account: {identity.json()}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    since = date.today() - timedelta(days=args.days)
    for data_type in args.types:
        points = list_data_points(session, data_type, since)
        out = OUTPUT_DIR / f"{data_type}.json"
        out.write_text(json.dumps(points, indent=2))
        print(f"  {data_type}: {len(points)} data points -> {out}")


if __name__ == "__main__":
    main()
