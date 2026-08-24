"""Validates the session token and returns the current user — used on page
load to restore a logged-in session from the token stored in the browser."""
import os
from http.server import BaseHTTPRequestHandler

from _lib.util import send_json, send_cors_preflight
from _lib.db import get_conn
from _lib.auth import get_authed_payload, public_user


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
                cur.execute("SELECT id, email, display_name FROM users WHERE id = %s;", (payload.get("uid"),))
                row = cur.fetchone()
            if not row:
                send_json(self, 401, {"error": "Account no longer exists."})
                return
            send_json(self, 200, {"user": public_user(row)})
        except Exception as e:
            send_json(self, 500, {"error": "Database error: " + str(e)})
        finally:
            conn.close()
