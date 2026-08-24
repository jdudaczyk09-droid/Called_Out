"""Lazy Postgres connection using the POSTGRES_URL env var Vercel sets when
a Postgres (Neon) database is attached to the project. Returns None if the
database isn't configured — callers respond with a clear error instead of
crashing."""
import os

try:
    import psycopg2
except ImportError:  # pragma: no cover — psycopg2-binary ships via requirements.txt
    psycopg2 = None


def get_conn():
    url = os.environ.get("POSTGRES_URL")
    if not url or not psycopg2:
        return None
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn
