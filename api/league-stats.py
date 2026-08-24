"""Aggregate stats for a league code — total rounds, unique students,
most-common fallacies, scores by judge persona, and a recent-rounds feed.
Backs the League Dashboard screen.

No real auth — see api/league-submit.py for the same caveat."""
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _lib.util import send_json, send_cors_preflight
from _lib.db import get_conn


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_cors_preflight(self, "GET, OPTIONS")

    def do_GET(self):
        conn = get_conn()
        if not conn:
            send_json(self, 500, {
                "error": "No Postgres database attached. In Vercel: Storage → Postgres (Neon) → connect to this project."
            })
            return

        query = parse_qs(urlparse(self.path).query)
        league_code = (query.get("code", [""])[0] or "").strip()[:40]
        if not league_code:
            send_json(self, 400, {"error": "Missing ?code="})
            conn.close()
            return

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
                cur.execute("""
                    SELECT student_name, ts, mode, topic, practice_mode, judge_persona, score_a, score_b, fallacy_names
                    FROM league_debates
                    WHERE league_code = %s
                    ORDER BY ts DESC
                    LIMIT 500;
                """, (league_code,))
                rows = cur.fetchall()

            students = set()
            fallacy_counts = {}
            persona_groups = {}
            for row in rows:
                student_name = row[0]
                judge_persona = row[5]
                score_a = row[6]
                fallacy_names = row[8]
                students.add(student_name)
                names = fallacy_names if isinstance(fallacy_names, list) else []
                for n in names:
                    fallacy_counts[n] = fallacy_counts.get(n, 0) + 1
                key = judge_persona if judge_persona and judge_persona != "none" else "none"
                persona_groups.setdefault(key, []).append(float(score_a or 0))

            recent = []
            for row in rows[:50]:
                student_name, ts, mode, topic, practice_mode, judge_persona, score_a, score_b, fallacy_names = row
                recent.append({
                    "studentName": student_name,
                    "ts": ts.isoformat() if ts else None,
                    "topic": topic,
                    "practiceMode": practice_mode,
                    "scoreA": float(score_a or 0),
                    "scoreB": float(score_b or 0),
                })

            send_json(self, 200, {
                "leagueCode": league_code,
                "totalRounds": len(rows),
                "uniqueStudents": len(students),
                "fallacyCounts": fallacy_counts,
                "personaGroups": persona_groups,
                "recent": recent,
            })
        except Exception as e:
            send_json(self, 500, {"error": "Database error: " + str(e)})
        finally:
            conn.close()
