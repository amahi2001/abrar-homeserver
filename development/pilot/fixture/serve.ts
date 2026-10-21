import { readFileSync } from "node:fs";

const script = readFileSync(new URL("dist/app.js", import.meta.url));
Bun.serve({
  hostname: "0.0.0.0", port: 8000,
  fetch(request) {
    if (new URL(request.url).pathname === "/app.js") return new Response(script, { headers: { "content-type": "text/javascript" } });
    return new Response('<!doctype html><title>Dev container pilot</title><label>Name<input aria-label="Name"></label><button>Save</button><p role="status">Ready</p><script type="module" src="/app.js"></script>', { headers: { "content-type": "text/html" } });
  },
});
