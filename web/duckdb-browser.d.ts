// The browser subpath ships no .d.ts of its own; its shape is the package root.
declare module "@duckdb/duckdb-wasm/dist/duckdb-browser.mjs" {
  export * from "@duckdb/duckdb-wasm";
}
