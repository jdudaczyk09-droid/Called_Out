"""Vercel serverless proxy for Groq Whisper transcription (multipart/form-data).
NOTE on Hobby tier: request body is capped at 4.5 MB. Upload-mode chunks
audio to ~2-min slices (~3.8 MB at 16 kHz mono) to stay under the cap.

Supports a multi-key pool (GROQ_API_KEYS) — see api/_lib/groqkeys.py."""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from _lib.util import send_json, send_cors_preflight, read_raw_body
from _lib.groqkeys import get_groq_keys

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_cors_preflight(self, "POST, OPTIONS")

    def do_POST(self):
        keys = get_groq_keys()
        if not keys:
            send_json(self, 500, {
                "error": "No Groq API key configured on the server. Add GROQ_API_KEY (or GROQ_API_KEYS for a rotating pool) in Vercel → Settings → Environment Variables."
            })
            return

        content_type = self.headers.get("Content-Type", "") or ""
        if "multipart/" not in content_type.lower():
            send_json(self, 400, {"error": "Expected multipart/form-data Content-Type."})
            return

        body = read_raw_body(self)

        upstream = None
        for key in keys:
            try:
                upstream = requests.post(
                    GROQ_URL,
                    data=body,
                    headers={"Authorization": "Bearer " + key, "Content-Type": content_type},
                    timeout=120,
                )
            except Exception as e:
                send_json(self, 502, {"error": "Upstream error: " + str(e)})
                return
            if upstream.status_code != 429:
                break  # success, or a non-rate-limit error — no point trying another key

        payload = upstream.content
        self.send_response(upstream.status_code)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
