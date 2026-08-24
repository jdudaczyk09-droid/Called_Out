"""Records one finished debate's summary against a league code so a
coach/tournament director can see aggregate stats.

No real auth — a league code is a shared classroom-style code, not a
password. Don't put anything sensitive behind it. Requires a Postgres
database attached to the Vercel project (Storage → Postgres, powered by
Neon) so POSTGRES_URL is set as an env var."""
import json
from http.server import BaseHTTPRequestHandler

from _lib.util import send_json, send_cors_preflight, read_json_body
from _lib.db import get_conn


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_cors_preflight(self, "POST, OPTIONS")

    def do_POST(self):
        conn = get_conn()
        if not conn:
            send_json(self, 500, {
                "error": "No Postgres database attached. In Vercel: Storage → Postgres (Neon) → connect to this project."
            })
            return

        body = read_json_body(self)
        league_code = str(body.get("leagueCode") or "").strip()[:40]
        student_name = str(body.get("studentName") or "").strip()[:80]
        if not league_code or not student_name:
            send_json(self, 400, {"error": "leagueCode and studentName are required."})
            conn.close()
            return

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
                    CREATE TABLE IF NOT EXISTS league_debates (
                        id SERIAL PRIMARY KEY,
                        league_code TEXT NOT NULL,
                        student_name TEXT NOT NULL,
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
                cur.execute("CREATE INDEX IF NOT EXISTS idx_league_debates_code ON league_debates (league_code);")
                cur.execute("""
                    INSERT INTO league_debates
                        (league_code, student_name, mode, topic, practice_mode, judge_persona, score_a, score_b, fallacy_names)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (league_code, student_name, mode, topic, practice_mode, judge_persona, score_a, score_b, json.dumps(fallacy_names)))
            send_json(self, 200, {"ok": True})
        except Exception as e:
            send_json(self, 500, {"error": "Database error: " + str(e)})
        finally:
            conn.close()
