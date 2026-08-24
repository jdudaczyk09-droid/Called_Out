"""Multi-key pool for the Groq proxies. Lets a deployment spread traffic
across several free-tier Groq API keys instead of paying for a higher tier
on one — each request tries a random key first, and fails over to the next
key in the pool only if that one comes back rate-limited (HTTP 429).

Configure with GROQ_API_KEYS as a comma-separated list in Vercel's
environment variables. Falls back to the single GROQ_API_KEY var if
GROQ_API_KEYS isn't set, so existing single-key deployments keep working
unchanged."""
import os
import random


def get_groq_keys():
    raw = os.environ.get("GROQ_API_KEYS", "").strip()
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if keys:
            random.shuffle(keys)
            return keys
    single = os.environ.get("GROQ_API_KEY", "").strip()
    return [single] if single else []
