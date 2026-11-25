// Translate Claude Mem's OpenAI-compatible chat parameters on the Luna leg only.
const http = require("node:http");
const { pipeline } = require("node:stream/promises");

http.createServer(async (req, res) => {
  if (req.method !== "POST" || req.url !== "/v1/chat/completions") {
    res.writeHead(404).end();
    return;
  }
  const authorization = req.headers.authorization;
  if (!authorization?.startsWith("Bearer ")) {
    res.writeHead(401).end();
    return;
  }
  const abort = new AbortController();
  res.on("close", () => abort.abort());
  try {
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 16 * 1024 * 1024) {
        res.writeHead(413).end();
        return;
      }
      chunks.push(chunk);
    }
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (body?.model !== "gpt-6-luna") {
      res.writeHead(403).end();
      return;
    }
    if (body.max_tokens !== undefined) {
      body.max_completion_tokens ??= body.max_tokens;
      delete body.max_tokens;
    }
    delete body.temperature;
    if (body.reasoning?.enabled === false) delete body.reasoning;

    const upstream = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: { authorization, "content-type": "application/json" },
      body: JSON.stringify(body),
      redirect: "error",
      signal: abort.signal,
    });
    for (const header of ["content-type", "retry-after", "x-request-id"]) {
      const value = upstream.headers.get(header);
      if (value) res.setHeader(header, value);
    }
    res.writeHead(upstream.status);
    if (upstream.body) await pipeline(upstream.body, res);
    else res.end();
  } catch (error) {
    if (res.headersSent || res.destroyed) res.destroy();
    else res.writeHead(error instanceof SyntaxError ? 400 : 502).end();
  }
}).listen(20130, "127.0.0.1");
