"""Vercel serverless proxy for Groq chat-completions.
The client posts the same body it would post to api.groq.com directly;
this function injects the GROQ_API_KEY env var and forwards the request,
so the key never reaches the browser."""
import os
from http.server import BaseHTTPRequestHandler

import requests

from _lib.util import send_json, send_cors_preflight, read_raw_body

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_cors_preflight(self, "POST, OPTIONS")

    def do_POST(self):
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            send_json(self, 500, {
                "error": "GROQ_API_KEY env var is not set on the server. Add it in Vercel → Settings → Environment Variables."
            })
            return

        body = read_raw_body(self)
        content_type = self.headers.get("Content-Type", "application/json")

        try:
            upstream = requests.post(
                GROQ_URL,
                data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": content_type},
                timeout=60,
            )
        except Exception as e:
            send_json(self, 502, {"error": "Upstream error: " + str(e)})
            return

        payload = upstream.content
        self.send_response(upstream.status_code)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
