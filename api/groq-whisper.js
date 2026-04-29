/*
 * Vercel serverless proxy for Groq Whisper transcription (multipart/form-data).
 * NOTE on Hobby tier: request body is capped at 4.5 MB. Upload-mode chunks
 * audio to ~2-min slices (~3.8 MB at 16 kHz mono) to stay under the cap.
 */

export const config = {
  api: { bodyParser: false }, // multipart must pass through verbatim
};

export default async function handler(req, res) {
  if (req.method === "OPTIONS") {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");
    return res.status(204).end();
  }
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Method not allowed" });
  }

  const key = process.env.GROQ_API_KEY;
  if (!key) {
    return res.status(500).json({
      error: "GROQ_API_KEY env var is not set on the server. Add it in Vercel → Settings → Environment Variables.",
    });
  }

  let body;
  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    body = Buffer.concat(chunks);
  } catch (e) {
    return res.status(400).json({ error: "Failed to read request body: " + (e.message || String(e)) });
  }

  // Preserve the multipart boundary by passing the original Content-Type through
  const contentType = req.headers["content-type"];
  if (!contentType || !contentType.toLowerCase().includes("multipart/")) {
    return res.status(400).json({ error: "Expected multipart/form-data Content-Type." });
  }

  try {
    const upstream = await fetch("https://api.groq.com/openai/v1/audio/transcriptions", {
      method: "POST",
      headers: {
        Authorization: "Bearer " + key,
        "Content-Type": contentType,
      },
      body,
    });
    const text = await upstream.text();
    res.status(upstream.status);
    res.setHeader("Content-Type", upstream.headers.get("content-type") || "application/json");
    res.send(text);
  } catch (e) {
    res.status(502).json({ error: "Upstream error: " + (e.message || String(e)) });
  }
}
