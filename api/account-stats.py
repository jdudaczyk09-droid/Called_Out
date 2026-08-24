"""Aggregate stats for the signed-in account — same response shape as
api/league-stats.py so the client can reuse one renderer for both."""
import os
from http.server import BaseHTTPRequestHandler

from _lib.util import send_json, send_cors_preflight
from _lib.db import get_conn
from _lib.auth import get_authed_payload


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_cors_preflight(self, "GET, OPTIONS")

    def do_GET(self):
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
                cur.execute("""
                    SELECT ts, mode, topic, practice_mode, judge_persona, score_a, score_b, fallacy_names
                    FROM user_debates
                    WHERE user_id = %s
                    ORDER BY ts DESC
                    LIMIT 500;
                """, (payload.get("uid"),))
                rows = cur.fetchall()

            fallacy_counts = {}
            persona_groups = {}
            for row in rows:
                judge_persona = row[4]
                score_a = row[5]
                fallacy_names = row[7]
                names = fallacy_names if isinstance(fallacy_names, list) else []
                for n in names:
                    fallacy_counts[n] = fallacy_counts.get(n, 0) + 1
                key = judge_persona if judge_persona and judge_persona != "none" else "none"
                persona_groups.setdefault(key, []).append(float(score_a or 0))

            recent = []
            for row in rows[:50]:
                ts, mode, topic, practice_mode, judge_persona, score_a, score_b, fallacy_names = row
                recent.append({
                    "studentName": "You",
                    "ts": ts.isoformat() if ts else None,
                    "topic": topic,
                    "practiceMode": practice_mode,
                    "scoreA": float(score_a or 0),
                    "scoreB": float(score_b or 0),
                })

            send_json(self, 200, {
                "totalRounds": len(rows),
                "uniqueStudents": 1,
                "fallacyCounts": fallacy_counts,
                "personaGroups": persona_groups,
                "recent": recent,
            })
        except Exception as e:
            send_json(self, 500, {"error": "Database error: " + str(e)})
        finally:
            conn.close()
