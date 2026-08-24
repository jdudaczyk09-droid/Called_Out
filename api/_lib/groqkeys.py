"""Multi-key pool for the Groq proxies. Lets a deployment spread traffic
across several free-tier Groq API keys instead of paying for a higher tier
on one — each request tries a random key first, and fails over to the next
key in the pool only if that one comes back rate-limited (HTTP 429).

Reads keys from whichever of these are configured (all are merged into one
pool if more than one is present):
  - Verdict_1, Verdict_2, Verdict_3, ... — one key per numbered variable,
    contiguous starting at 1 (this is how the keys are actually set up in
    this project's Vercel dashboard).
  - GROQ_API_KEYS — a single comma-separated list, e.g. "gsk_aaa,gsk_bbb".
  - GROQ_API_KEY — a single key, for the simplest single-key deployments.
"""
import os
import random


def get_groq_keys():
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
