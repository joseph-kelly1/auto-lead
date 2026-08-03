#!/usr/bin/env node
/**
 * Local stand-in for the R2 bucket: serves artifacts/ with the range support
 * and CORS headers that DuckDB-WASM and pmtiles need.
 *
 *   node scripts/serve-artifacts.mjs [port]
 *
 * Then point the viewer at it:  NEXT_PUBLIC_ARTIFACT_BASE=http://localhost:8787
 */
import { createReadStream, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(fileURLToPath(new URL("..", import.meta.url)), "artifacts");
const PORT = Number(process.argv[2] ?? 8787);
const TYPES = {
  ".parquet": "application/vnd.apache.parquet",
  ".pmtiles": "application/octet-stream",
  ".csv": "text/csv",
  ".json": "application/json",
};

const cors = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, HEAD, OPTIONS",
  "access-control-allow-headers": "range, if-match",
  "access-control-expose-headers": "etag, content-length, content-range, accept-ranges",
};

createServer((req, res) => {
  if (req.method === "OPTIONS") return res.writeHead(204, cors).end();
  if (req.method !== "GET" && req.method !== "HEAD")
    return res.writeHead(405, cors).end();

  // normalize() collapses ..; the leading-slash check keeps it under ROOT.
  const rel = normalize(decodeURIComponent(new URL(req.url, "http://x").pathname));
  const path = join(ROOT, rel);
  if (!path.startsWith(ROOT)) return res.writeHead(403, cors).end();

  let stat;
  try {
    stat = statSync(path);
  } catch {
    return res
      .writeHead(404, { ...cors, "content-type": "text/plain" })
      .end(`${rel} is not in artifacts/. Run the pipeline first.\n`);
  }
  if (stat.isDirectory()) return res.writeHead(403, cors).end();

  const head = {
    ...cors,
    "accept-ranges": "bytes",
    "content-type": TYPES[extname(path)] ?? "application/octet-stream",
    etag: `"${stat.size}-${stat.mtimeMs}"`,
    "cache-control": "no-cache",
  };

  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? "");
  if (m) {
    const start = m[1] ? Number(m[1]) : stat.size - Number(m[2]);
    const end = m[1] && m[2] ? Number(m[2]) : stat.size - 1;
    if (!(start >= 0 && end < stat.size && start <= end))
      return res
        .writeHead(416, { ...head, "content-range": `bytes */${stat.size}` })
        .end();
    res.writeHead(206, {
      ...head,
      "content-range": `bytes ${start}-${end}/${stat.size}`,
      "content-length": end - start + 1,
    });
    if (req.method === "HEAD") return res.end();
    return createReadStream(path, { start, end }).pipe(res);
  }

  res.writeHead(200, { ...head, "content-length": stat.size });
  if (req.method === "HEAD") return res.end();
  createReadStream(path).pipe(res);
}).listen(PORT, () => {
  console.log(`artifacts on http://localhost:${PORT} from ${ROOT}`);
});
