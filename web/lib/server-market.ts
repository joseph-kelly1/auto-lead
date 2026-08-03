import "server-only";

import type { MlsMarketData, ZillowMarketData } from "@/lib/market";

const BRIDGE = "https://api.bridgedataoutput.com/api/v2";
const ON_MARKET = new Set([
  "active",
  "coming soon",
  "pending",
  "contingent",
  "active under contract",
]);

function text(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function number(value: unknown) {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function record(value: unknown) {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

function records(payload: unknown) {
  const body = record(payload);
  const collection = body?.value ?? body?.bundle;
  return Array.isArray(collection)
    ? collection.map(record).filter((item): item is Record<string, unknown> => Boolean(item))
    : [];
}

async function bridgeJson(url: URL, token: string) {
  const response = await fetch(url, {
    headers: { authorization: `Bearer ${token}`, accept: "application/json" },
    signal: AbortSignal.timeout(10_000),
    cache: "no-store",
  });
  if (!response.ok)
    throw new Error(`Bridge returned ${response.status} ${response.statusText}`);
  return response.json() as Promise<unknown>;
}

export async function fetchZillow(
  token: string,
  address: string,
  town: string,
): Promise<ZillowMarketData | undefined> {
  const url = new URL(`${BRIDGE}/zestimates_v2/zestimates`);
  url.searchParams.set("address", `${address}, ${town}, MA`);
  const first = records(await bridgeJson(url, token))[0];
  if (!first) return undefined;

  return {
    zestimate: number(first.zestimate ?? first.Zestimate),
    low: number(first.lower ?? first.Low),
    high: number(first.upper ?? first.High),
    asOf: text(first.date ?? first.Date),
    url: text(first.zillowUrl ?? first.ZillowUrl),
  };
}

function odataString(value: string) {
  return `'${value.toLowerCase().replace(/'/g, "''")}'`;
}

function normalizedAddress(value: unknown) {
  return (text(value) ?? "")
    .toUpperCase()
    .replace(/\bROAD\b/g, "RD")
    .replace(/\bSTREET\b/g, "ST")
    .replace(/\bAVENUE\b/g, "AVE")
    .replace(/\bDRIVE\b/g, "DR")
    .replace(/\bLANE\b/g, "LN")
    .replace(/\bPLACE\b/g, "PL")
    .replace(/\bCOURT\b/g, "CT")
    .replace(/[^A-Z0-9]/g, "");
}

async function mlsRecords(
  token: string,
  dataset: string,
  filter: string,
) {
  const url = new URL(`${BRIDGE}/OData/${encodeURIComponent(dataset)}/Property`);
  url.searchParams.set("$filter", filter);
  url.searchParams.set("$top", "20");
  url.searchParams.set("$orderby", "BridgeModificationTimestamp desc");
  url.searchParams.set("$unselect", "Media");
  return records(await bridgeJson(url, token));
}

export async function fetchMls(
  token: string,
  dataset: string,
  address: string,
  lat: number | null,
  lon: number | null,
): Promise<MlsMarketData | undefined> {
  let found = await mlsRecords(
    token,
    dataset,
    `tolower(UnparsedAddress) eq ${odataString(address)}`,
  );

  if (!found.length && lat != null && lon != null)
    found = await mlsRecords(
      token,
      dataset,
      `geo.distance(Coordinates,POINT(${lon} ${lat})) lt 0.05`,
    );
  if (!found.length) return undefined;

  const target = normalizedAddress(address);
  const exact = found.filter(
    (item) => normalizedAddress(item.UnparsedAddress) === target,
  );
  const candidates = exact.length ? exact : found;
  const selected =
    candidates.find((item) => ON_MARKET.has((text(item.StandardStatus) ?? "").toLowerCase())) ??
    candidates[0];
  const status = text(selected.StandardStatus) ?? "Off Market";

  return {
    status,
    onMarket: ON_MARKET.has(status.toLowerCase()),
    listPrice: number(selected.ListPrice),
    closePrice: number(selected.ClosePrice),
    listingId: text(selected.ListingId),
    modifiedAt: text(
      selected.BridgeModificationTimestamp ?? selected.ModificationTimestamp,
    ),
  };
}
