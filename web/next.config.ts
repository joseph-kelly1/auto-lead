import type { NextConfig } from "next";

// No COOP/COEP headers here on purpose. Those are only needed for DuckDB's
// cross-origin-isolated bundle, which uses SharedArrayBuffer and pthreads.
// lib/duck.ts selects the mvp/eh bundle instead, and setting COEP would in
// fact break loading the worker and wasm from the CDN.
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Parcel data never passes through a function; artifacts are fetched
  // straight from object storage by the browser over range requests.
  outputFileTracingExcludes: { "*": ["../artifacts/**", "../data/**"] },
};

export default nextConfig;
