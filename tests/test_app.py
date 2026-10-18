"""Tests that need no network, Google account or model: the logic, the HTTP routes and the assistant's tool loop (model faked)."""

import json
import os
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update(HEALTH_DB=f"{TMP}/health.db", MEMORY_FILE=f"{TMP}/MEMORY.md", REPORT_DIR=f"{TMP}/reports",
                  HEIGHT_CM="175", WORK_HOURS="22-06", TARGET_BEDTIME="07-08", TARGET_WAKE="15-16", TZ="UTC")
os.environ.pop("REPORT_BASIC_AUTH", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import assistant  # noqa: E402
import healthsync  # noqa: E402
import metrics  # noqa: E402


def reply(content="", tool_calls=None, finish="stop"):
    """The part of a LiteLLM response the assistant reads."""
    msg = types.SimpleNamespace(content=content, tool_calls=tool_calls,
                                model_dump=lambda **_: {"role": "assistant", "content": content})
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason=finish)])


def tool_call(name, args):
    return types.SimpleNamespace(id="call1", function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))


class Logic(unittest.TestCase):
    def test_diet_target_comes_from_bmi_when_no_goal(self):
        over = metrics.diet_target(81, 170)
        self.assertEqual((over["category"], over["direction"], over["auto"]), ("overweight", "lose", True))
        self.assertAlmostEqual(over["goal_kg"], 24.9 * 1.7**2, places=1)
        self.assertEqual(metrics.diet_target(68, 175)["direction"], "maintain")
        self.assertEqual(metrics.diet_target(50, 175)["direction"], "gain")

    def test_explicit_goal_wins(self):
        t = metrics.diet_target(81, 170, goal_kg=75)
        self.assertEqual((t["direction"], t["goal_kg"], t["auto"]), ("lose", 75, False))

    def test_day_state_handles_night_shift_wrapping_midnight(self):
        at = lambda h, m=0: metrics.day_state(datetime(2026, 10, 19, h, m))["state"]
        self.assertEqual(at(23), "work")      # inside 22-06
        self.assertEqual(at(2), "work")       # after midnight, still the shift
        self.assertEqual(at(7, 30), "free")   # still inside the 07-08 bedtime range: falling asleep, not yet asleep
        self.assertEqual(at(8, 30), "sleep")  # from the end of the bedtime range until the wake range (15-16) starts
        self.assertEqual(at(14, 59), "sleep")
        self.assertEqual(at(17), "free")

    def test_weekly_slope(self):
        pts = [{"at": f"2026-10-{d:02d}T08:00", "kg": 80 - 0.1 * (d - 1)} for d in range(1, 22, 3)]
        self.assertAlmostEqual(metrics.weekly_slope(pts), -0.7, places=1)
        self.assertIsNone(metrics.weekly_slope(pts[:2]))


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), healthsync.WebhookHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def post(self, path, body):
        return requests.post(self.base + path, json=body, timeout=10)

    def test_page_and_icons(self):
        self.assertIn("<title>Health</title>", requests.get(self.base + "/").text)
        icon = requests.get(self.base + "/favicon.svg")
        self.assertEqual((icon.status_code, icon.headers["Content-Type"]), (200, "image/svg+xml"))
        self.assertEqual(requests.get(self.base + "/apple-touch-icon.png").headers["Content-Type"], "image/png")
        self.assertEqual(requests.get(self.base + "/healthz").text, "ok")

    def test_only_listed_files_are_served(self):
        for path in ("/../healthsync.py", "/web/index.html", "/.env", "/data/health.db"):
            self.assertEqual(requests.get(self.base + path).status_code, 404, path)

    def test_weight_logging_and_validation(self):
        self.assertEqual(self.post("/api/weight", {"kg": 5}).status_code, 400)
        self.assertEqual(self.post("/api/weight", {"kg": 80, "date": "2999-01-01"}).status_code, 400)
        state = self.post("/api/weight", {"kg": 79.5, "note": "test"}).json()
        self.assertEqual(state["profile"]["weight_kg"], 79.5)
        self.assertEqual(state["profile"]["weight_source"], "manual")

    def test_form_posts_are_rejected(self):
        r = requests.post(self.base + "/api/chat", data="message=hi", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status_code, 415)

    def test_webhook_needs_its_secret(self):
        os.environ["WEBHOOK_SECRET"] = "s3cret"
        try:
            self.assertEqual(self.post("/webhook", {}).status_code, 401)
            ok = requests.post(self.base + "/webhook", json={"type": "verification"}, headers={"Authorization": "Bearer s3cret"})
            self.assertEqual(ok.status_code, 201)
        finally:
            del os.environ["WEBHOOK_SECRET"]

    def test_basic_auth_flag(self):
        os.environ.update(REPORT_BASIC_AUTH="true", REPORT_PASSWORD="pw")
        try:
            self.assertEqual(requests.get(self.base + "/api/state").status_code, 401)
            self.assertEqual(requests.get(self.base + "/api/state", auth=("me", "pw")).status_code, 200)
            self.assertEqual(requests.get(self.base + "/healthz").status_code, 200)  # health checks stay open
        finally:
            del os.environ["REPORT_BASIC_AUTH"], os.environ["REPORT_PASSWORD"]

    def test_food_log_and_new_chat(self):
        diet = self.post("/api/food", {"text": "dal and rice", "kcal": 500}).json()
        self.assertEqual(diet["today_kcal"], 500)
        self.assertEqual(self.post("/api/food", {"text": ""}).status_code, 400)
        before = requests.get(self.base + "/api/chat").json()["messages"]
        conn = healthsync.db()
        assistant._save(conn, "user", "hello")
        self.assertEqual(len(requests.get(self.base + "/api/chat").json()["messages"]), len(before) + 1)
        self.assertEqual(self.post("/api/new-chat", {}).json()["messages"], [])
        conn.close()


class Assistant(unittest.TestCase):
    def test_tool_loop_logs_weight_then_answers(self):
        conn = healthsync.db()
        calls = []

        def fake(**kw):
            calls.append(kw)
            if kw["messages"][-1]["role"] == "tool":
                return reply("Logged 77.7 kg. Nice.")
            if kw.get("tools"):
                return reply(tool_calls=[tool_call("log_weight", {"kg": 77.7})], finish="tool_calls")
            return reply("# Health memory\n\n## Log\n- 2026-10-19 weighed 77.7 kg\n" + "." * 200)  # the memory rewrite

        real, assistant.llm = assistant.llm, fake
        try:
            self.assertEqual(assistant.chat(conn, "I weigh 77.7 today"), "Logged 77.7 kg. Nice.")
            for t in threading.enumerate():
                if t is not threading.current_thread() and t.daemon and t.is_alive() and t.name.startswith("Thread"):
                    t.join(5)
        finally:
            assistant.llm = real
        self.assertEqual(metrics.current_weight(conn)["kg"], 77.7)
        self.assertTrue(Path(os.environ["MEMORY_FILE"]).read_text().startswith("# Health memory"))
        self.assertIn("MEMORY.md", calls[0]["messages"][0]["content"])  # the memory is part of every system prompt
        conn.close()

    def test_plan_parser_rejects_garbage_and_normalises_times(self):
        with self.assertRaises(ValueError):
            assistant._parse_plan("not json")
        plan = assistant._parse_plan('```json\n{"summary":"s","daily_kcal":2000,"meals":[{"name":"a","time":"7:5","what":"x","kcal":300},'
                                     '{"name":"b","time":"19:00","what":"y","kcal":600}]}\n```')
        self.assertEqual([m["time"] for m in plan["meals"]], ["07:05", "19:00"])

    def test_sleep_window_advice_needs_no_model(self):
        self.assertEqual(assistant.sleep_advice({})["action"], "avoid")


if __name__ == "__main__":
    unittest.main()
