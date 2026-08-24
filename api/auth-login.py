import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _lib.util import send_json, send_cors_preflight, read_json_body
from _lib.db import get_conn
from _lib.auth import verify_password, sign_token, is_valid_email


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
        if not is_valid_email(email) or not password:
            send_json(self, 400, {"error": "Enter your email and password."})
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
                cur.execute("SELECT id, email, password_hash, display_name FROM users WHERE email = %s;", (email,))
                row = cur.fetchone()

            # Same "invalid email or password" message either way — don't reveal which part was wrong.
            if not row or not verify_password(password, row[2]):
                send_json(self, 401, {"error": "Invalid email or password."})
                return

            token = sign_token(row[0], row[1])
            send_json(self, 200, {"token": token, "user": {"id": row[0], "email": row[1], "displayName": row[3]}})
        except Exception as e:
            send_json(self, 500, {"error": "Database error: " + str(e)})
        finally:
            conn.close()
