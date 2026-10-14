"use client";

import type * as DuckDB from "@duckdb/duckdb-wasm";

/**
 * DuckDB-WASM reading parcels.parquet over HTTP range requests.
 *
 * No parcel data passes through a Vercel Function: functions cap bodies at
 * 4.5 MB and a single town exceeds that. The browser fetches the artifact
 * directly from object storage, and after the first load every filter change
 * is a local query with no network round trip.
 *
 * Bundles come from jsDelivr and the worker is created from a blob URL, which
 * sidesteps bundler-specific worker and wasm asset handling entirely. The
 * mvp/eh bundle is selected rather than the coi one, so no SharedArrayBuffer
 * and therefore no COOP/COEP headers - which would break these CDN fetches.
 */

// Empty means same origin: the artifacts are staged into public/ at build
// time. Point this at an R2 bucket once the dataset outgrows the 100 MB
// Vercel source-upload cap, or at the local artifact server to test that path.
// DuckDB opens artifacts through XMLHttpRequest, which rejects a root-relative
// path, so the same-origin default has to resolve to a full origin.
const CONFIGURED = process.env.NEXT_PUBLIC_ARTIFACT_BASE?.replace(/\/$/, "");
export const ARTIFACT_BASE =
  CONFIGURED || (typeof window === "undefined" ? "" : window.location.origin);

let ready: Promise<DuckDB.AsyncDuckDBConnection> | null = null;

async function connect() {
  // The browser entry by exact subpath, imported at call time. The package
  // root resolves to duckdb-node.cjs during Next's server pass over this
  // client module, and that file has a dynamic require webpack cannot follow.
  // Types still come from the root via the `import type` above.
  const duckdb: typeof DuckDB = await import(
    "@duckdb/duckdb-wasm/dist/duckdb-browser.mjs"
  );
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker!}");`], {
      type: "text/javascript",
    }),
  );
  const db = new duckdb.AsyncDuckDB(
    new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING),
    new Worker(workerUrl),
  );
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);

  const conn = await db.connect();
  await db.registerFileURL(
    "parcels.parquet",
    `${ARTIFACT_BASE}/parcels.parquet`,
    duckdb.DuckDBDataProtocol.HTTP,
    false,
  );
  await conn.query(
    `CREATE VIEW parcels AS SELECT * FROM read_parquet('parcels.parquet')`,
  );
  return conn;
}

export function db() {
  if (!ready) ready = connect();
  return ready;
}

/** Arrow rows, with BigInt and Date flattened to things React can render. */
export async function query<T = Record<string, unknown>>(
  sql: string,
): Promise<T[]> {
  const conn = await db();
  const table = await conn.query(sql);
  return table.toArray().map((row) => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(row.toJSON())) {
      out[k] =
        typeof v === "bigint"
          ? Number(v)
          : v instanceof Date
            ? v.toISOString().slice(0, 10)
            : v;
    }
    return out as T;
  });
}

export async function queryOne<T = Record<string, unknown>>(sql: string) {
  return (await query<T>(sql))[0];
}

export function sqlString(s: string) {
  return `'${s.replace(/'/g, "''")}'`;
}
