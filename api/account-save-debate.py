"""Saves one finished debate's summary against the signed-in account, so
progress syncs across devices. Purely additive — logged-out users keep
using localStorage-only progress tracking (see recordDebateSummary() in
index.html)."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _lib.util import send_json, send_cors_preflight, read_json_body
from _lib.db import get_conn
from _lib.auth import get_authed_payload


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_cors_preflight(self, "POST, OPTIONS")

    def do_POST(self):
        if not os.environ.get("JWT_SECRET"):
            send_json(self, 500, {
                "error": "JWT_SECRET env var is not set on the server. Add a long random string in Vercel → Settings → Environment Variables."
            })
            return
        conn = get_conn()
        if not conn:
            send_json(self, 500, {
                "error": "No Postgres database attached. In Vercel: Storage → Postgres (Neon) → connect to this project."
            })
            return

        payload = get_authed_payload(self)
        if not payload:
            send_json(self, 401, {"error": "Not signed in."})
            conn.close()
            return

        body = read_json_body(self)
        mode = str(body.get("mode") or "")[:40]
        topic = str(body.get("topic") or "")[:500]
        practice_mode = bool(body.get("practiceMode"))
        judge_persona = str(body.get("judgePersona") or "none")[:40]
        try:
            score_a = float(body.get("scoreA") or 0)
            score_b = float(body.get("scoreB") or 0)
        except (TypeError, ValueError):
            score_a, score_b = 0, 0
        fallacy_names = body.get("fallacyNames")
        if not isinstance(fallacy_names, list):
            fallacy_names = []
        fallacy_names = [str(x)[:60] for x in fallacy_names[:40]]

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_debates (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                        mode TEXT,
                        topic TEXT,
                        practice_mode BOOLEAN,
                        judge_persona TEXT,
                        score_a NUMERIC,
                        score_b NUMERIC,
                        fallacy_names JSONB
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_debates_user ON user_debates (user_id);")
                cur.execute("""
                    INSERT INTO user_debates
                        (user_id, mode, topic, practice_mode, judge_persona, score_a, score_b, fallacy_names)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """, (payload.get("uid"), mode, topic, practice_mode, judge_persona, score_a, score_b, json.dumps(fallacy_names)))
            send_json(self, 200, {"ok": True})
        except Exception as e:
            send_json(self, 500, {"error": "Database error: " + str(e)})
        finally:
            conn.close()
