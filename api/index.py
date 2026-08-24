"""Verdict AI backend — a single Flask app handling every /api/* route.

Deliberately one file, zero local imports between files: Vercel's Python
runtime treats each file under /api/ as its own isolated serverless
function, and the previous multi-file setup (nine entry files importing
shared helpers from api/_lib/) depended on Vercel correctly bundling that
_lib package alongside every single one of them — exactly the kind of
cross-file import that's hard to verify without a live deploy. Putting
everything in one file removes that risk entirely.

vercel.json rewrites every /api/* request to this file; Flask's own
@app.route() does the rest of the dispatching internally.

Routes (unchanged from the previous per-file version, same paths, same
request/response JSON shapes — the client in index.html needed zero changes):
  POST /api/groq-chat             - Groq chat-completions proxy
  POST /api/groq-whisper          - Groq Whisper transcription proxy
  POST /api/league-submit         - record a debate against a league code
  GET  /api/league-stats          - aggregate stats for a league code
  POST /api/auth-signup           - create an account (bcrypt-hashed password)
  POST /api/auth-login            - log in, get a JWT session token
  GET  /api/auth-me               - validate a session token
  POST /api/account-save-debate   - sync a signed-in user's debate summary
  GET  /api/account-stats         - aggregate stats for a signed-in account
"""
import datetime
import json
import os
import random
import re

import bcrypt
import jwt
import requests
from flask import Flask, Response, jsonify, request

try:
    import psycopg2
except ImportError:  # pragma: no cover — psycopg2-binary ships via requirements.txt
    psycopg2 = None

app = Flask(__name__)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
TOKEN_TTL_DAYS = 30
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


# =============================================================================
# Shared helpers
# =============================================================================
def get_groq_keys():
    """Multi-key pool so a deployment can spread traffic across several
    free-tier Groq keys instead of paying for a higher tier on one. Reads
    every source that's configured and merges them into one deduplicated,
    shuffled pool:
      - Verdict_1, Verdict_2, ... (contiguous from 1) — how the keys are
        actually set up in this project's Vercel dashboard.
      - GROQ_API_KEYS — a single comma-separated list.
      - GROQ_API_KEY — a single key.
    """
    keys = []

    raw = os.environ.get("GROQ_API_KEYS", "").strip()
    if raw:
        keys.extend(k.strip() for k in raw.split(",") if k.strip())

    i = 1
    while True:
        v = os.environ.get(f"Verdict_{i}", "").strip()
        if not v:
            break
        keys.append(v)
        i += 1

    single = os.environ.get("GROQ_API_KEY", "").strip()
    if single:
        keys.append(single)

    keys = list(dict.fromkeys(keys))  # de-dupe, keep it simple
    random.shuffle(keys)
    return keys


def get_conn():
    """Lazy Postgres connection using POSTGRES_URL (set automatically when a
    Postgres/Neon database is attached in Vercel). Returns None if it isn't
    configured — callers respond with a clear error instead of crashing."""
    url = os.environ.get("POSTGRES_URL")
    if not url or not psycopg2:
        return None
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def sign_token(user_id, email):
    secret = os.environ["JWT_SECRET"]
    payload = {
        "uid": user_id,
        "email": email,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def get_authed_payload():
    """Returns the decoded {uid, email} payload from the current request's
    Authorization header, or None if missing/invalid/expired."""
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        return None
    auth_header = request.headers.get("Authorization", "") or ""
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None


def is_valid_email(email):
    return isinstance(email, str) and bool(EMAIL_RE.match(email)) and len(email) <= 200


def public_user(row):
    """row: (id, email, display_name)"""
    return {"id": row[0], "email": row[1], "displayName": row[2]}


def need_db():
    """Returns an error Response if no database is configured, else None."""
    if not os.environ.get("POSTGRES_URL"):
        return jsonify({"error": "No Postgres database attached. In Vercel: Storage → Postgres (Neon) → connect to this project."}), 500
    return None


def need_jwt_secret():
    if not os.environ.get("JWT_SECRET"):
        return jsonify({"error": "JWT_SECRET env var is not set on the server. Add a long random string in Vercel → Settings → Environment Variables."}), 500
    return None


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


# =============================================================================
# Groq proxies
# =============================================================================
@app.route("/api/groq-chat", methods=["POST", "OPTIONS"])
def groq_chat():
    if request.method == "OPTIONS":
        return "", 204

    keys = get_groq_keys()
    if not keys:
        return jsonify({"error": "No Groq API key configured on the server. Add GROQ_API_KEY (or GROQ_API_KEYS for a rotating pool) in Vercel → Settings → Environment Variables."}), 500

    body = request.get_data()
    content_type = request.headers.get("Content-Type", "application/json")

    upstream = None
    for key in keys:
        try:
            upstream = requests.post(
                GROQ_CHAT_URL,
                data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": content_type},
                timeout=60,
            )
        except Exception as e:
            return jsonify({"error": "Upstream error: " + str(e)}), 502
        if upstream.status_code != 429:
            break  # success, or a non-rate-limit error — no point trying another key

    return Response(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )


@app.route("/api/groq-whisper", methods=["POST", "OPTIONS"])
def groq_whisper():
    if request.method == "OPTIONS":
        return "", 204

    keys = get_groq_keys()
    if not keys:
        return jsonify({"error": "No Groq API key configured on the server. Add GROQ_API_KEY (or GROQ_API_KEYS for a rotating pool) in Vercel → Settings → Environment Variables."}), 500

    content_type = request.headers.get("Content-Type", "") or ""
    if "multipart/" not in content_type.lower():
        return jsonify({"error": "Expected multipart/form-data Content-Type."}), 400

    body = request.get_data()

    upstream = None
    for key in keys:
        try:
            upstream = requests.post(
                GROQ_WHISPER_URL,
                data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": content_type},
                timeout=120,
            )
        except Exception as e:
            return jsonify({"error": "Upstream error: " + str(e)}), 502
        if upstream.status_code != 429:
            break

    return Response(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )


# =============================================================================
# League Dashboards — no real auth, a league code is a shared classroom-style
# code, not a password.
# =============================================================================
LEAGUE_DEBATES_TABLE = """
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
"""


@app.route("/api/league-submit", methods=["POST", "OPTIONS"])
def league_submit():
    if request.method == "OPTIONS":
        return "", 204
    err = need_db()
    if err:
        return err
    conn = get_conn()

    body = request.get_json(silent=True) or {}
    league_code = str(body.get("leagueCode") or "").strip()[:40]
    student_name = str(body.get("studentName") or "").strip()[:80]
    if not league_code or not student_name:
        conn.close()
        return jsonify({"error": "leagueCode and studentName are required."}), 400

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
            cur.execute(LEAGUE_DEBATES_TABLE)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_league_debates_code ON league_debates (league_code);")
            cur.execute("""
                INSERT INTO league_debates
                    (league_code, student_name, mode, topic, practice_mode, judge_persona, score_a, score_b, fallacy_names)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (league_code, student_name, mode, topic, practice_mode, judge_persona, score_a, score_b, json.dumps(fallacy_names)))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/league-stats", methods=["GET", "OPTIONS"])
def league_stats():
    if request.method == "OPTIONS":
        return "", 204
    err = need_db()
    if err:
        return err
    conn = get_conn()

    league_code = (request.args.get("code") or "").strip()[:40]
    if not league_code:
        conn.close()
        return jsonify({"error": "Missing ?code="}), 400

    try:
        with conn.cursor() as cur:
            cur.execute(LEAGUE_DEBATES_TABLE)
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
            student_name, judge_persona, score_a = row[0], row[5], row[6]
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

        return jsonify({
            "leagueCode": league_code,
            "totalRounds": len(rows),
            "uniqueStudents": len(students),
            "fallacyCounts": fallacy_counts,
            "personaGroups": persona_groups,
            "recent": recent,
        })
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


# =============================================================================
# Accounts — fully optional, bcrypt-hashed passwords + JWT sessions
# =============================================================================
USERS_TABLE = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
"""
USER_DEBATES_TABLE = """
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
"""


@app.route("/api/auth-signup", methods=["POST", "OPTIONS"])
def auth_signup():
    if request.method == "OPTIONS":
        return "", 204
    err = need_jwt_secret() or need_db()
    if err:
        return err
    conn = get_conn()

    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    display_name = str(body.get("displayName") or "").strip()[:60] or email.split("@")[0]

    if not is_valid_email(email):
        conn.close()
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        conn.close()
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    try:
        with conn.cursor() as cur:
            cur.execute(USERS_TABLE)
            cur.execute("SELECT id FROM users WHERE email = %s;", (email,))
            if cur.fetchone():
                return jsonify({"error": "An account with that email already exists — try logging in instead."}), 409

            password_hash = hash_password(password)
            cur.execute("""
                INSERT INTO users (email, password_hash, display_name)
                VALUES (%s, %s, %s)
                RETURNING id, email, display_name;
            """, (email, password_hash, display_name))
            row = cur.fetchone()

        token = sign_token(row[0], row[1])
        return jsonify({"token": token, "user": public_user(row)})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/auth-login", methods=["POST", "OPTIONS"])
def auth_login():
    if request.method == "OPTIONS":
        return "", 204
    err = need_jwt_secret() or need_db()
    if err:
        return err
    conn = get_conn()

    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    if not is_valid_email(email) or not password:
        conn.close()
        return jsonify({"error": "Enter your email and password."}), 400

    try:
        with conn.cursor() as cur:
            cur.execute(USERS_TABLE)
            cur.execute("SELECT id, email, password_hash, display_name FROM users WHERE email = %s;", (email,))
            row = cur.fetchone()

        # Same "invalid email or password" message either way — don't reveal which part was wrong.
        if not row or not verify_password(password, row[2]):
            return jsonify({"error": "Invalid email or password."}), 401

        token = sign_token(row[0], row[1])
        return jsonify({"token": token, "user": {"id": row[0], "email": row[1], "displayName": row[3]}})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/auth-me", methods=["GET", "OPTIONS"])
def auth_me():
    if request.method == "OPTIONS":
        return "", 204
    err = need_jwt_secret() or need_db()
    if err:
        return err
    conn = get_conn()

    payload = get_authed_payload()
    if not payload:
        conn.close()
        return jsonify({"error": "Not signed in."}), 401

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email, display_name FROM users WHERE id = %s;", (payload.get("uid"),))
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "Account no longer exists."}), 401
        return jsonify({"user": public_user(row)})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/account-save-debate", methods=["POST", "OPTIONS"])
def account_save_debate():
    if request.method == "OPTIONS":
        return "", 204
    err = need_jwt_secret() or need_db()
    if err:
        return err
    conn = get_conn()

    payload = get_authed_payload()
    if not payload:
        conn.close()
        return jsonify({"error": "Not signed in."}), 401

    body = request.get_json(silent=True) or {}
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
            cur.execute(USER_DEBATES_TABLE)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_user_debates_user ON user_debates (user_id);")
            cur.execute("""
                INSERT INTO user_debates
                    (user_id, mode, topic, practice_mode, judge_persona, score_a, score_b, fallacy_names)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
            """, (payload.get("uid"), mode, topic, practice_mode, judge_persona, score_a, score_b, json.dumps(fallacy_names)))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/account-stats", methods=["GET", "OPTIONS"])
def account_stats():
    if request.method == "OPTIONS":
        return "", 204
    err = need_jwt_secret() or need_db()
    if err:
        return err
    conn = get_conn()

    payload = get_authed_payload()
    if not payload:
        conn.close()
        return jsonify({"error": "Not signed in."}), 401

    try:
        with conn.cursor() as cur:
            cur.execute(USER_DEBATES_TABLE)
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
            judge_persona, score_a, fallacy_names = row[4], row[5], row[7]
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

        return jsonify({
            "totalRounds": len(rows),
            "uniqueStudents": 1,
            "fallacyCounts": fallacy_counts,
            "personaGroups": persona_groups,
            "recent": recent,
        })
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True)
