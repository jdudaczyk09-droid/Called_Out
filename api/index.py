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

Every route checks cheap things first (env vars configured, auth header
valid, request body sane) *before* ever opening a database connection —
psycopg2.connect() is a real blocking network call, not something you want
to pay for on a request that was always going to fail anyway, and a
suspended free-tier Neon database or a network blip should come back as a
clean JSON error, not an unhandled crash.

Routes (unchanged from the previous per-file version, same paths, same
request/response JSON shapes — the client in app.html needed zero changes,
plus the new profile route):
  POST /api/groq-chat             - Groq chat-completions proxy
  POST /api/groq-whisper          - Groq Whisper transcription proxy
  POST /api/league-submit         - record a debate against a league code
  GET  /api/league-stats          - aggregate stats for a league code
  POST /api/auth-signup           - create an account (bcrypt-hashed password)
  POST /api/auth-login            - log in, get a JWT session token
  GET  /api/auth-me               - validate a session token
  POST /api/account-profile       - update display name / bio / avatar
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

DB_ERROR = {"error": "Couldn't reach the database. Make sure a Postgres database is attached in Vercel's Storage tab — if it already is, it may just be waking up from being idle; try again in a few seconds."}
NO_KEY_ERROR = {"error": "No Groq API key configured on the server. Add GROQ_API_KEY (or GROQ_API_KEYS for a rotating pool) in Vercel → Settings → Environment Variables."}
NO_JWT_ERROR = {"error": "JWT_SECRET env var is not set on the server. Add a long random string in Vercel → Settings → Environment Variables."}
NOT_SIGNED_IN = {"error": "Not signed in."}


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
    """Postgres connection using POSTGRES_URL (set automatically when a
    Postgres/Neon database is attached in Vercel). Returns None if it isn't
    configured OR if the connection attempt itself fails (unreachable,
    suspended free-tier database, bad credentials, etc.) — every caller
    treats None as "respond with DB_ERROR", never lets the exception
    propagate into an unhandled 500."""
    url = os.environ.get("POSTGRES_URL")
    if not url or not psycopg2:
        return None
    try:
        conn = psycopg2.connect(url, connect_timeout=10)
        conn.autocommit = True
        return conn
    except Exception:
        return None


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
    Authorization header, or None if missing/invalid/expired. Cheap and
    local — no DB or network call, safe to check before opening a connection."""
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
    """row: (id, email, display_name, bio, avatar) — bio/avatar optional,
    missing gracefully for rows fetched before those columns existed."""
    bio = row[3] if len(row) > 3 else ""
    avatar = row[4] if len(row) > 4 else ""
    return {"id": row[0], "email": row[1], "displayName": row[2], "bio": bio or "", "avatar": avatar or ""}


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
        return jsonify(NO_KEY_ERROR), 500

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
        return jsonify(NO_KEY_ERROR), 500

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

    body = request.get_json(silent=True) or {}
    league_code = str(body.get("leagueCode") or "").strip()[:40]
    student_name = str(body.get("studentName") or "").strip()[:80]
    if not league_code or not student_name:
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

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
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

    league_code = (request.args.get("code") or "").strip()[:40]
    if not league_code:
        return jsonify({"error": "Missing ?code="}), 400

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
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
# Added after the fact — migrates any table created before profiles existed.
USERS_TABLE_PROFILE_COLUMNS = """
    ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT NOT NULL DEFAULT '';
    ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar TEXT NOT NULL DEFAULT '';
"""
# Avatars are small data: URIs (client resizes to ~160px before upload) stored
# straight in the row — no separate file storage service to configure. Capped
# well above what a resized JPEG needs, as a sanity limit, not a target size.
MAX_AVATAR_LEN = 300_000
MAX_BIO_LEN = 500
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
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500

    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    display_name = str(body.get("displayName") or "").strip()[:60] or email.split("@")[0]

    if not is_valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
    try:
        with conn.cursor() as cur:
            cur.execute(USERS_TABLE)
            cur.execute(USERS_TABLE_PROFILE_COLUMNS)
            cur.execute("SELECT id FROM users WHERE email = %s;", (email,))
            if cur.fetchone():
                return jsonify({"error": "An account with that email already exists — try logging in instead."}), 409

            password_hash = hash_password(password)
            cur.execute("""
                INSERT INTO users (email, password_hash, display_name)
                VALUES (%s, %s, %s)
                RETURNING id, email, display_name, bio, avatar;
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
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500

    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    if not is_valid_email(email) or not password:
        return jsonify({"error": "Enter your email and password."}), 400

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
    try:
        with conn.cursor() as cur:
            cur.execute(USERS_TABLE)
            cur.execute(USERS_TABLE_PROFILE_COLUMNS)
            cur.execute("SELECT id, email, password_hash, display_name, bio, avatar FROM users WHERE email = %s;", (email,))
            row = cur.fetchone()

        # Same "invalid email or password" message either way — don't reveal which part was wrong.
        if not row or not verify_password(password, row[2]):
            return jsonify({"error": "Invalid email or password."}), 401

        token = sign_token(row[0], row[1])
        user_row = (row[0], row[1], row[3], row[4], row[5])  # drop password_hash before returning
        return jsonify({"token": token, "user": public_user(user_row)})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/auth-me", methods=["GET", "OPTIONS"])
def auth_me():
    if request.method == "OPTIONS":
        return "", 204
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500

    payload = get_authed_payload()
    if not payload:
        return jsonify(NOT_SIGNED_IN), 401

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
    try:
        with conn.cursor() as cur:
            cur.execute(USERS_TABLE_PROFILE_COLUMNS)
            cur.execute("SELECT id, email, display_name, bio, avatar FROM users WHERE id = %s;", (payload.get("uid"),))
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "Account no longer exists."}), 401
        return jsonify({"user": public_user(row)})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/account-profile", methods=["POST", "OPTIONS"])
def account_profile():
    """Updates the signed-in user's display name, bio, and/or avatar.
    Any field omitted from the request body is left unchanged."""
    if request.method == "OPTIONS":
        return "", 204
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500

    payload = get_authed_payload()
    if not payload:
        return jsonify(NOT_SIGNED_IN), 401

    body = request.get_json(silent=True) or {}
    updates = []
    params = []

    if "displayName" in body:
        display_name = str(body.get("displayName") or "").strip()[:60]
        if not display_name:
            return jsonify({"error": "Display name can't be empty."}), 400
        updates.append("display_name = %s")
        params.append(display_name)

    if "bio" in body:
        bio = str(body.get("bio") or "")[:MAX_BIO_LEN]
        updates.append("bio = %s")
        params.append(bio)

    if "avatar" in body:
        avatar = str(body.get("avatar") or "")
        if avatar and not avatar.startswith("data:image/"):
            return jsonify({"error": "Avatar must be an image."}), 400
        if len(avatar) > MAX_AVATAR_LEN:
            return jsonify({"error": "That image is too large — try a smaller photo."}), 400
        updates.append("avatar = %s")
        params.append(avatar)

    if not updates:
        return jsonify({"error": "Nothing to update."}), 400

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
    try:
        with conn.cursor() as cur:
            cur.execute(USERS_TABLE_PROFILE_COLUMNS)
            params.append(payload.get("uid"))
            cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s RETURNING id, email, display_name, bio, avatar;", params)
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
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500

    payload = get_authed_payload()
    if not payload:
        return jsonify(NOT_SIGNED_IN), 401

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

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
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
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500

    payload = get_authed_payload()
    if not payload:
        return jsonify(NOT_SIGNED_IN), 401

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
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


# =============================================================================
# Online Debate — two signed-in users, two devices, one shared room. No
# WebSockets on this stack, so this is a polling-based shared room in
# Postgres: both clients GET /api/room-state every couple seconds, and the
# device whose turn it is POSTs its own local speech-to-text result when
# the turn ends. The server doesn't know the format's phase list (that
# lives client-side in DEBATE_MODES) — it just stores whatever phase
# index/side/timer the client computes and reports, the same way both
# clients already independently render phases from the same format data
# in local mode. This is a cooperative 2-player tool, not an adversarial
# one, so that's an acceptable trust model here.
# =============================================================================
ONLINE_ROOMS_TABLE = """
    CREATE TABLE IF NOT EXISTS online_rooms (
        id SERIAL PRIMARY KEY,
        room_code TEXT UNIQUE NOT NULL,
        mode TEXT NOT NULL,
        topic TEXT,
        host_user_id INTEGER NOT NULL,
        host_name TEXT NOT NULL,
        guest_user_id INTEGER,
        guest_name TEXT,
        status TEXT NOT NULL DEFAULT 'waiting',
        phase_index INTEGER NOT NULL DEFAULT 0,
        current_side TEXT NOT NULL DEFAULT 'a',
        turn_seconds INTEGER NOT NULL DEFAULT 60,
        turn_remaining INTEGER NOT NULL DEFAULT 60,
        turn_started_at TIMESTAMPTZ,
        score_a NUMERIC NOT NULL DEFAULT 0,
        score_b NUMERIC NOT NULL DEFAULT 0,
        transcript JSONB NOT NULL DEFAULT '[]',
        score_log JSONB NOT NULL DEFAULT '[]',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
"""
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I — easier to read aloud/type


def gen_room_code():
    return "".join(random.choice(ROOM_CODE_ALPHABET) for _ in range(6))


ROOM_COLUMNS = "room_code, mode, topic, host_user_id, host_name, guest_user_id, guest_name, status, phase_index, current_side, turn_seconds, turn_remaining, turn_started_at, score_a, score_b, transcript, score_log"


def room_public_state(row, uid):
    (room_code, mode, topic, host_user_id, host_name, guest_user_id, guest_name, status,
     phase_index, current_side, turn_seconds, turn_remaining, turn_started_at,
     score_a, score_b, transcript, score_log) = row
    live_remaining = turn_remaining
    if turn_started_at:
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - turn_started_at).total_seconds()
        live_remaining = max(0, turn_remaining - elapsed)
    return {
        "roomCode": room_code,
        "mode": mode,
        "topic": topic,
        "hostName": host_name,
        "guestName": guest_name,
        "yourSide": "a" if uid == host_user_id else ("b" if uid == guest_user_id else None),
        "status": status,
        "phaseIndex": phase_index,
        "currentSide": current_side,
        "turnSeconds": turn_seconds,
        "turnRemaining": round(live_remaining),
        "timerRunning": turn_started_at is not None,
        "scoreA": float(score_a or 0),
        "scoreB": float(score_b or 0),
        "transcript": transcript if isinstance(transcript, list) else [],
        "scoreLog": score_log if isinstance(score_log, list) else [],
    }


def fetch_room_for_participant(cur, room_code, uid):
    """Returns the room row if it exists and uid is the host or guest, else None."""
    cur.execute(f"SELECT {ROOM_COLUMNS} FROM online_rooms WHERE room_code = %s;", (room_code,))
    row = cur.fetchone()
    if not row:
        return None
    host_user_id, guest_user_id = row[3], row[5]
    if uid != host_user_id and uid != guest_user_id:
        return None
    return row


@app.route("/api/room-create", methods=["POST", "OPTIONS"])
def room_create():
    if request.method == "OPTIONS":
        return "", 204
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500
    payload = get_authed_payload()
    if not payload:
        return jsonify(NOT_SIGNED_IN), 401

    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "casual")[:40]
    topic = str(body.get("topic") or "")[:500]
    host_name = str(body.get("displayName") or payload.get("email") or "Host")[:80]
    try:
        turn_seconds = int(body.get("turnSeconds") or 60)
    except (TypeError, ValueError):
        turn_seconds = 60

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
    try:
        with conn.cursor() as cur:
            cur.execute(ONLINE_ROOMS_TABLE)
            code = gen_room_code()
            for _ in range(5):  # collision retry — astronomically unlikely, cheap to guard anyway
                cur.execute("SELECT 1 FROM online_rooms WHERE room_code = %s;", (code,))
                if not cur.fetchone():
                    break
                code = gen_room_code()
            cur.execute(f"""
                INSERT INTO online_rooms (room_code, mode, topic, host_user_id, host_name, turn_seconds, turn_remaining)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {ROOM_COLUMNS};
            """, (code, mode, topic, payload.get("uid"), host_name, turn_seconds, turn_seconds))
            row = cur.fetchone()
        return jsonify({"room": room_public_state(row, payload.get("uid"))})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/room-join", methods=["POST", "OPTIONS"])
def room_join():
    if request.method == "OPTIONS":
        return "", 204
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500
    payload = get_authed_payload()
    if not payload:
        return jsonify(NOT_SIGNED_IN), 401

    body = request.get_json(silent=True) or {}
    room_code = str(body.get("roomCode") or "").strip().upper()[:12]
    guest_name = str(body.get("displayName") or payload.get("email") or "Guest")[:80]
    if not room_code:
        return jsonify({"error": "Enter a room code."}), 400

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
    try:
        with conn.cursor() as cur:
            cur.execute(ONLINE_ROOMS_TABLE)
            cur.execute(f"SELECT {ROOM_COLUMNS} FROM online_rooms WHERE room_code = %s;", (room_code,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "No room with that code — check it and try again."}), 404

            uid = payload.get("uid")
            host_user_id, guest_user_id = row[3], row[5]
            if uid == host_user_id:
                return jsonify({"room": room_public_state(row, uid)})  # host "joining" their own room — just return state
            if guest_user_id and guest_user_id != uid:
                return jsonify({"error": "That room already has two debaters in it."}), 409

            cur.execute(f"""
                UPDATE online_rooms SET guest_user_id = %s, guest_name = %s, status = 'active', updated_at = now()
                WHERE room_code = %s
                RETURNING {ROOM_COLUMNS};
            """, (uid, guest_name, room_code))
            row = cur.fetchone()
        return jsonify({"room": room_public_state(row, uid)})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/room-state", methods=["GET", "OPTIONS"])
def room_state():
    if request.method == "OPTIONS":
        return "", 204
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500
    payload = get_authed_payload()
    if not payload:
        return jsonify(NOT_SIGNED_IN), 401

    room_code = (request.args.get("code") or "").strip().upper()[:12]
    if not room_code:
        return jsonify({"error": "Missing ?code="}), 400

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
    try:
        with conn.cursor() as cur:
            cur.execute(ONLINE_ROOMS_TABLE)
            row = fetch_room_for_participant(cur, room_code, payload.get("uid"))
        if not row:
            return jsonify({"error": "Room not found, or you're not a participant in it."}), 404
        return jsonify({"room": room_public_state(row, payload.get("uid"))})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


@app.route("/api/room-action", methods=["POST", "OPTIONS"])
def room_action():
    if request.method == "OPTIONS":
        return "", 204
    if not os.environ.get("JWT_SECRET"):
        return jsonify(NO_JWT_ERROR), 500
    payload = get_authed_payload()
    if not payload:
        return jsonify(NOT_SIGNED_IN), 401

    body = request.get_json(silent=True) or {}
    room_code = str(body.get("roomCode") or "").strip().upper()[:12]
    action = str(body.get("action") or "")
    if not room_code or action not in ("start_turn", "pause_turn", "next_turn", "score", "end"):
        return jsonify({"error": "Invalid room action."}), 400

    conn = get_conn()
    if not conn:
        return jsonify(DB_ERROR), 500
    uid = payload.get("uid")
    try:
        with conn.cursor() as cur:
            cur.execute(ONLINE_ROOMS_TABLE)
            row = fetch_room_for_participant(cur, room_code, uid)
            if not row:
                return jsonify({"error": "Room not found, or you're not a participant in it."}), 404

            (_, _, _, _, _, _, _, status, phase_index, current_side, turn_seconds,
             turn_remaining, turn_started_at, score_a, score_b, transcript, score_log) = row
            transcript = transcript if isinstance(transcript, list) else []
            score_log = score_log if isinstance(score_log, list) else []

            if action == "start_turn":
                if turn_started_at is None:
                    cur.execute("UPDATE online_rooms SET turn_started_at = now(), updated_at = now() WHERE room_code = %s;", (room_code,))

            elif action == "pause_turn":
                if turn_started_at is not None:
                    elapsed = (datetime.datetime.now(datetime.timezone.utc) - turn_started_at).total_seconds()
                    new_remaining = max(0, round(turn_remaining - elapsed))
                    cur.execute("UPDATE online_rooms SET turn_remaining = %s, turn_started_at = NULL, updated_at = now() WHERE room_code = %s;", (new_remaining, room_code))

            elif action == "next_turn":
                entry = body.get("transcriptEntry") or {}
                transcript.append({
                    "side": str(entry.get("side") or current_side)[:1],
                    "phaseName": str(entry.get("phaseName") or "")[:200],
                    "text": str(entry.get("text") or "")[:20000],
                })
                try:
                    next_phase_index = int(body.get("nextPhaseIndex", phase_index + 1))
                except (TypeError, ValueError):
                    next_phase_index = phase_index + 1
                next_side = str(body.get("nextSide") or current_side)[:4]
                try:
                    next_turn_seconds = int(body.get("nextTurnSeconds") or turn_seconds)
                except (TypeError, ValueError):
                    next_turn_seconds = turn_seconds
                ended = bool(body.get("ended"))
                cur.execute("""
                    UPDATE online_rooms
                    SET transcript = %s, phase_index = %s, current_side = %s,
                        turn_seconds = %s, turn_remaining = %s, turn_started_at = NULL,
                        status = %s, updated_at = now()
                    WHERE room_code = %s;
                """, (json.dumps(transcript), next_phase_index, next_side, next_turn_seconds,
                      next_turn_seconds, "ended" if ended else status, room_code))

            elif action == "score":
                side = "a" if str(body.get("side")) == "a" else "b"
                try:
                    delta = float(body.get("delta") or 0)
                except (TypeError, ValueError):
                    delta = 0
                label = str(body.get("label") or "")[:120]
                new_a = float(score_a or 0) + (delta if side == "a" else 0)
                new_b = float(score_b or 0) + (delta if side == "b" else 0)
                score_log.append({"side": side, "delta": delta, "label": label, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()})
                score_log = score_log[-100:]
                cur.execute("UPDATE online_rooms SET score_a = %s, score_b = %s, score_log = %s, updated_at = now() WHERE room_code = %s;",
                            (new_a, new_b, json.dumps(score_log), room_code))

            elif action == "end":
                cur.execute("UPDATE online_rooms SET status = 'ended', turn_started_at = NULL, updated_at = now() WHERE room_code = %s;", (room_code,))

            cur.execute(f"SELECT {ROOM_COLUMNS} FROM online_rooms WHERE room_code = %s;", (room_code,))
            row = cur.fetchone()
        return jsonify({"room": room_public_state(row, uid)})
    except Exception as e:
        return jsonify({"error": "Database error: " + str(e)}), 500
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True)
