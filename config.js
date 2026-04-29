/*
 * COMMITTED CONFIG — safe to push to GitHub.
 * NO API keys live here. Toggles only.
 *
 * For local development, copy `config.local.example.js` to `config.local.js`
 * and add your own GROQ_API_KEY there. `config.local.js` is gitignored and
 * overrides anything set here.
 *
 * For deployment (Vercel), set GROQ_API_KEY as an environment variable in
 * the Vercel dashboard. The /api/* serverless proxies inject it server-side
 * and the browser never sees it.
 */
(function () {
  window.APP_CONFIG = {
    // PROXY_MODE: when true, the client routes API calls through /api/* on the
    // same origin instead of calling api.groq.com directly. Vercel deploys
    // auto-route /api/<file>.js to your serverless functions.
    // Leave true here — a local config.local.js can flip it off for direct mode.
    PROXY_MODE: true,

    // Model defaults (no keys — keys live in env vars / config.local.js)
    GROQ_MODEL: "llama-3.3-70b-versatile",
    GROQ_WHISPER_MODEL: "whisper-large-v3-turbo",

    // --- Auto fact-check tuning ---
    AUTO_FACTCHECK_DEFAULT: true,
    AUTO_FACTCHECK_MIN_CHARS: 8,
    AUTO_FACTCHECK_MIN_WORDS: 2,
    AUTO_FACTCHECK_COOLDOWN_MS: 1000,
    AUTO_FACTCHECK_CONFIDENCE: 0.7,

    // --- Accuracy boosters ---
    USE_WIKIPEDIA_GROUNDING: true,
    USE_CONTEXT_WINDOW: true,
    CONTEXT_SENTENCES: 3,
    REQUIRE_CITATIONS: true,
    MULTI_CLAIM_EXTRACTION: true,
    CONSENSUS_BORDERLINE_LOW: 0.6,
    CONSENSUS_BORDERLINE_HIGH: 0.85,
    USE_CONSENSUS: false,
    CONFIDENCE_WEIGHTED_SCORING: true,

    // --- Reliability ---
    MAX_RETRIES: 2,
    PER_DEBATE_BUDGET: 200,

    // --- UX ---
    TTS_VERDICTS: true,
    TTS_VOLUME: 0.7,
    TTS_RATE: 1.05,
    AUTO_FALLACY_DETECTION: true,
    SHOW_CLAIM_LOG: true,

    // --- Whisper (live mode) ---
    USE_WHISPER: false,
    WHISPER_CHUNK_MS: 4000,

    // --- Upload mode ---
    // 2-minute chunks fit Vercel Hobby's 4.5 MB body cap (~3.8 MB at 16 kHz mono).
    // Local dev with PROXY_MODE: false can crank this up to 10 for fewer API calls.
    UPLOAD_CHUNK_MINUTES: 2,
    UPLOAD_AUDIO_RATE: 16000,
  };
})();
