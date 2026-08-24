"""Account creation. Fully optional — the app works without ever calling this.
Passwords are hashed with bcrypt before touching the database; the plain
password is never stored or logged."""
import os
from http.server import BaseHTTPRequestHandler

from _lib.util import send_json, send_cors_preflight, read_json_body
from _lib.db import get_conn
from _lib.auth import hash_password, sign_token, is_valid_email, public_user


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

        body = read_json_body(self)
        email = str(body.get("email") or "").strip().lower()
        password = str(body.get("password") or "")
        display_name = str(body.get("displayName") or "").strip()[:60] or email.split("@")[0]

        if not is_valid_email(email):
            send_json(self, 400, {"error": "Enter a valid email address."})
            conn.close()
            return
        if len(password) < 8:
            send_json(self, 400, {"error": "Password must be at least 8 characters."})
            conn.close()
            return

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                """)
                cur.execute("SELECT id FROM users WHERE email = %s;", (email,))
                if cur.fetchone():
                    send_json(self, 409, {"error": "An account with that email already exists — try logging in instead."})
                    return

                password_hash = hash_password(password)
                cur.execute("""
                    INSERT INTO users (email, password_hash, display_name)
                    VALUES (%s, %s, %s)
                    RETURNING id, email, display_name;
                """, (email, password_hash, display_name))
                row = cur.fetchone()

            token = sign_token(row[0], row[1])
            send_json(self, 200, {"token": token, "user": public_user(row)})
        except Exception as e:
            send_json(self, 500, {"error": "Database error: " + str(e)})
        finally:
            conn.close()
