export type ZillowMarketData = {
  zestimate: number | null;
  low: number | null;
  high: number | null;
  asOf: string | null;
  url: string | null;
};

export type MlsMarketData = {
  status: string;
  onMarket: boolean;
  listPrice: number | null;
  closePrice: number | null;
  listingId: string | null;
  modifiedAt: string | null;
};

export type MarketData = {
  configured: boolean;
  zillowConfigured?: boolean;
  mlsConfigured?: boolean;
  zillow?: ZillowMarketData;
  mls?: MlsMarketData;
  errors?: string[];
  checkedAt?: string;
};

const CACHE_PREFIX = "teardown-market-v1:";
const CACHE_TTL = 24 * 60 * 60 * 1000;

function cacheKey(address: string, town: string) {
  return `${CACHE_PREFIX}${address.trim().toUpperCase()}|${town.trim().toUpperCase()}`;
}

export function clearMarketCache() {
  if (typeof window === "undefined") return;
  for (let index = window.localStorage.length - 1; index >= 0; index--) {
    const key = window.localStorage.key(index);
    if (key?.startsWith(CACHE_PREFIX)) window.localStorage.removeItem(key);
  }
  window.dispatchEvent(new Event("market-credentials-changed"));
}

export async function loadMarketData({
  address,
  town,
  lat,
  lon,
  signal,
}: {
  address: string;
  town: string;
  lat: number | null;
  lon: number | null;
  signal?: AbortSignal;
}) {
  const key = cacheKey(address, town);
  try {
    const cached = JSON.parse(window.localStorage.getItem(key) ?? "null") as {
      savedAt?: number;
      data?: MarketData;
    } | null;
    if (
      cached?.savedAt &&
      cached.data &&
      Date.now() - cached.savedAt < CACHE_TTL
    )
      return cached.data;
  } catch {
    window.localStorage.removeItem(key);
  }

  const parameters = new URLSearchParams({ address, town });
  if (lat != null) parameters.set("lat", String(lat));
  if (lon != null) parameters.set("lon", String(lon));
  const response = await fetch(`/api/market?${parameters}`, { signal });
  const result = (await response.json()) as MarketData & { error?: string };
  if (!response.ok) throw new Error(result.error ?? "Could not load market data.");

  // Missing credentials are intentionally not cached: adding them should make
  // data appear immediately. Provider results and errors refresh once per day.
  if (result.configured)
    window.localStorage.setItem(
      key,
      JSON.stringify({ savedAt: Date.now(), data: result }),
    );
  return result;
}
