export const RAMP = ["#1c3a38", "#2c5c55", "#47857a", "#78a986", "#b9c98e", "#d8c48f"];

/** Score -> ramp colour. Monotonic in lightness, so it reads in greyscale. */
export function rampColor(score: number) {
  const i = Math.min(RAMP.length - 1, Math.max(0, Math.round(score * (RAMP.length - 1))));
  return RAMP[i];
}

const usd0 = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

export const fmt = {
  usd: (v: number | null) => (v == null ? "-" : usd0.format(v)),
  usdK: (v: number | null) =>
    v == null ? "-" : v >= 1e6 ? `$${(v / 1e6).toFixed(2)}M` : `$${Math.round(v / 1e3)}k`,
  num: (v: number | null, d = 0) =>
    v == null ? "-" : v.toLocaleString("en-US", { maximumFractionDigits: d }),
  year: (v: number | null) => (v == null ? "-" : String(Math.trunc(v))),
  pct: (v: number | null, d = 0) => (v == null ? "-" : `${(v * 100).toFixed(d)}%`),
  ratio: (v: number | null) => (v == null ? "-" : v.toFixed(2)),
  ratio3: (v: number | null) => (v == null ? "-" : v.toFixed(3)),
  acres: (v: number | null) => (v == null ? "-" : `${v.toFixed(2)} ac`),
  /**
   * DuckDB-WASM hands a parquet DATE back as epoch milliseconds, not a string,
   * so a bare String() renders "838857600000" where a date belongs.
   */
  date: (v: string | number | Date | null) => {
    if (v == null || v === "") return "-";
    if (typeof v === "string") return v.slice(0, 10);
    const d = v instanceof Date ? v : new Date(v);
    return Number.isNaN(d.getTime()) ? "-" : d.toISOString().slice(0, 10);
  },
};

export function fmtBy(kind: string, v: number | null) {
  switch (kind) {
    case "usd":
      return fmt.usd(v);
    case "pct":
      return fmt.pct(v);
    case "ratio":
      return fmt.ratio(v);
    case "ratio3":
      return fmt.ratio3(v);
    default:
      return fmt.num(v);
  }
}
