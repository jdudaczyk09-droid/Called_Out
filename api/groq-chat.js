/*
 * Vercel serverless proxy for Groq chat-completions.
 * The client posts the same body it would post to api.groq.com directly;
 * this function injects the GROQ_API_KEY env var and forwards the request,
 * so the key never reaches the browser.
 */

export const config = {
  api: { bodyParser: false }, // we want the raw bytes for forwarding
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

  // Buffer the raw incoming body so we can forward it untouched
  let body;
  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    body = Buffer.concat(chunks);
  } catch (e) {
    return res.status(400).json({ error: "Failed to read request body: " + (e.message || String(e)) });
  }

  try {
    const upstream = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: {
        Authorization: "Bearer " + key,
        "Content-Type": req.headers["content-type"] || "application/json",
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
