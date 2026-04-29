/*
 * LOCAL CONFIG TEMPLATE — copy to config.local.js for local development.
 *
 * config.local.js is GITIGNORED. Put your real API keys here.
 * If both config.js and config.local.js define the same key, this one wins.
 *
 * For production (Vercel), do NOT use this file — set GROQ_API_KEY as an
 * environment variable in the Vercel dashboard instead.
 */
(function () {
  // Merge into the existing APP_CONFIG so toggles from config.js still apply
  window.APP_CONFIG = Object.assign(window.APP_CONFIG || {}, {
    // Disable proxy mode locally so the client calls Groq directly using the
    // key below. Set to true if you want to test the /api/* proxy locally
    // with `vercel dev`.
    PROXY_MODE: false,

    // Paste your key here — only on YOUR machine.
    GROQ_API_KEY: "",

    // Optional local overrides:
    // UPLOAD_CHUNK_MINUTES: 10,  // bigger chunks OK locally (no body cap)
    // USE_WHISPER: true,
  });
})();
