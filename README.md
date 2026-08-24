# Verdict AI

Live and uploaded debate scoring with fact-checking and fallacy detection. Powered by Groq (GPT-OSS 120B for reasoning, Whisper-large-v3-turbo for transcription).

## Quick start (local dev)

1. Copy the local config template:
   ```bash
   cp config.local.example.js config.local.js
   ```
2. Open `config.local.js` and paste your Groq API key into `GROQ_API_KEY`.
   Get a key at <https://console.groq.com/keys>.
3. Open `app.html` directly in a browser, or run a tiny static server:
   ```bash
   npx serve .
   ```

`config.local.js` is gitignored, so your key never leaves your machine. With `PROXY_MODE: false` set there, the browser calls Groq directly.

## Deploy to Vercel

The app ships as static HTML plus a single Flask app (`api/index.py`, Vercel's Python runtime — dependencies in `requirements.txt`) that holds the Groq key server-side, so visitors of your deployment never see it. `vercel.json` rewrites every `/api/*` request to this one function; Flask's own routing handles the rest internally — one file, no cross-file imports for Vercel to bundle correctly.

1. Push the repo to GitHub.
2. Import the project in the Vercel dashboard.
3. Under **Settings → Environment Variables**, add either:
   - `GROQ_API_KEY` — a single Groq key, or
   - `GROQ_API_KEYS` — a comma-separated pool of Groq keys (e.g. `gsk_aaa...,gsk_bbb...,gsk_ccc...`). Each request tries a random key from the pool and fails over to the next one only if that key comes back rate-limited (HTTP 429) — a cheap way to multiply your effective free-tier capacity across several Groq accounts instead of paying for a higher tier on one. If both vars are set, `GROQ_API_KEYS` wins.
4. Hit **Deploy**.

The committed `config.js` has `PROXY_MODE: true`, so deployed builds automatically route fetches through `/api/groq-chat` and `/api/groq-whisper`. Don't commit `config.local.js` — it overrides PROXY_MODE for your local machine only.

### League Dashboards (optional — needs a database)

Practice history, progress tracking, and everything else run entirely client-side (localStorage) with no setup required. **League Dashboards** are the one feature that needs a real backend, since a coach viewing aggregate stats across many students requires data to live somewhere other than each student's own browser.

1. In the Vercel dashboard: **Storage → Create Database → Postgres** (powered by Neon), then connect it to this project. Vercel sets `POSTGRES_URL` (and friends) automatically — no manual schema step, the API creates its table on first use.
2. Students opt in by entering a **League code** in Setup (Debaters card) — it's a shared classroom-style code, not a password. Anyone with the code can view or submit to that league's dashboard.
3. Coaches/tournament directors open the **League** tab, enter the code, and see total rounds, unique students, most-common fallacies, average score by judge persona, and a recent-rounds feed.

Without a Postgres database attached, the League tab shows a clear error instead of failing silently — every other feature keeps working normally.

### Accounts (optional — needs the same database, plus one more secret)

Every feature of Verdict AI works fully without an account — it's a purely optional layer for syncing practice history and progress across devices. Passwords are hashed with bcrypt before they ever touch the database; the plaintext password is never stored.

1. You need the same Postgres database as League Dashboards (see above) — the accounts feature reuses it and creates its own `users` / `user_debates` tables automatically on first use.
2. In the Vercel dashboard, add one more environment variable: `JWT_SECRET` — a long random string (e.g. `openssl rand -hex 32`) used to sign session tokens. Without it, the log in / sign up buttons show a clear error instead of failing silently.
3. Users sign up or log in from the account control in the header. Signed-in users get their debate summaries synced to `/api/account-save-debate` and can see cross-device stats on the Progress screen; everyone else keeps using localStorage exactly as before.

### Hobby tier note

Vercel's Hobby tier caps request bodies at 4.5 MB. Upload mode chunks audio into 2-minute slices (~3.8 MB at 16 kHz mono) to stay under the cap. Local dev with `PROXY_MODE: false` can use bigger chunks via `UPLOAD_CHUNK_MINUTES` in `config.local.js`.

## Files

- `index.html` — the marketing landing page (waitlist form, product pitch).
- `app.html` — the entire app (UI, recording, scoring, fact-check, judge).
- `config.js` — committed defaults (toggles only, no keys).
- `config.local.example.js` — template for local development.
- `config.local.js` — your local config, gitignored, holds your Groq key.
- `.env` — server-side env mirror, gitignored.
- `api/index.py` — the whole backend: one Flask app handling every `/api/*` route (Groq proxies, League Dashboards, accounts). See the module docstring at the top of the file for the full route list.
- `requirements.txt` (root and `api/`, identical — kept in both in case Vercel's builder only checks one) — Python dependencies (`flask`, `requests`, `psycopg2-binary`, `bcrypt`, `PyJWT`).
- `vercel.json` — rewrites all `/api/*` traffic to `api/index.py`, plus security headers + cache config.

## Scripts

```bash
npm run dev       # vercel dev (local emulation of /api/* — runs the Flask app too)
npm run deploy    # vercel --prod
```

The `vercel` CLI itself is a Node tool, but nothing in `/api/` is JavaScript anymore — `vercel dev` just also knows how to run Python functions locally (it reads `requirements.txt` and executes them with Vercel's Python runtime).

You can also run the backend directly for local testing, same as any Flask app:
```bash
pip install -r requirements.txt
python api/index.py    # starts on http://127.0.0.1:5000
```
