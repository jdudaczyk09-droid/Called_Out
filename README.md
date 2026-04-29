# Called Out

Live and uploaded debate scoring with fact-checking and fallacy detection. Powered by Groq (Llama 3.3 70B for reasoning, Whisper-large-v3-turbo for transcription).

## Quick start (local dev)

1. Copy the local config template:
   ```bash
   cp config.local.example.js config.local.js
   ```
2. Open `config.local.js` and paste your Groq API key into `GROQ_API_KEY`.
   Get a key at <https://console.groq.com/keys>.
3. Open `index.html` directly in a browser, or run a tiny static server:
   ```bash
   npx serve .
   ```

`config.local.js` is gitignored, so your key never leaves your machine. With `PROXY_MODE: false` set there, the browser calls Groq directly.

## Deploy to Vercel

The app ships as static HTML plus two serverless proxies in `/api/` that hold the Groq key server-side, so visitors of your deployment never see it.

1. Push the repo to GitHub.
2. Import the project in the Vercel dashboard.
3. Under **Settings → Environment Variables**, add:
   - `GROQ_API_KEY` — your Groq key.
4. Hit **Deploy**.

The committed `config.js` has `PROXY_MODE: true`, so deployed builds automatically route fetches through `/api/groq-chat` and `/api/groq-whisper`. Don't commit `config.local.js` — it overrides PROXY_MODE for your local machine only.

### Hobby tier note

Vercel's Hobby tier caps request bodies at 4.5 MB. Upload mode chunks audio into 2-minute slices (~3.8 MB at 16 kHz mono) to stay under the cap. Local dev with `PROXY_MODE: false` can use bigger chunks via `UPLOAD_CHUNK_MINUTES` in `config.local.js`.

## Files

- `index.html` — the entire app (UI, recording, scoring, fact-check, judge).
- `config.js` — committed defaults (toggles only, no keys).
- `config.local.example.js` — template for local development.
- `config.local.js` — your local config, gitignored, holds your Groq key.
- `.env` — server-side env mirror, gitignored.
- `api/groq-chat.js` — Vercel serverless proxy for Groq chat completions.
- `api/groq-whisper.js` — Vercel serverless proxy for Groq Whisper.
- `vercel.json` — security headers + cache config.

## Scripts

```bash
npm run dev       # vercel dev (local emulation of /api/* proxies)
npm run deploy    # vercel --prod
```
