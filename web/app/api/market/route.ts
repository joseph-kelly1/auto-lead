import { NextResponse } from "next/server";
import type { MarketData } from "@/lib/market";
import { isAuthenticated } from "@/lib/server-auth";
import {
  readSharedCredentials,
  sharedCredentialStorageConfigured,
} from "@/lib/server-credentials";
import { fetchMls, fetchZillow } from "@/lib/server-market";

function coordinate(value: string | null, min: number, max: number) {
  if (value == null) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= min && parsed <= max ? parsed : null;
}

export async function GET(request: Request) {
  if (!(await isAuthenticated()))
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const { searchParams } = new URL(request.url);
  const address = searchParams.get("address")?.trim() ?? "";
  const town = searchParams.get("town")?.trim() ?? "";
  if (!address || !town || address.length > 160 || town.length > 80)
    return NextResponse.json({ error: "A valid address and town are required." }, { status: 400 });

  const lat = coordinate(searchParams.get("lat"), -90, 90);
  const lon = coordinate(searchParams.get("lon"), -180, 180);
  if (!sharedCredentialStorageConfigured())
    return NextResponse.json({ configured: false } satisfies MarketData);

  let credentials;
  try {
    credentials = await readSharedCredentials();
  } catch {
    return NextResponse.json(
      { error: "Could not access shared credential storage." },
      { status: 502 },
    );
  }
  const zillowConfigured = Boolean(credentials.zillowToken);
  const mlsConfigured = Boolean(credentials.mlsToken && credentials.mlsDataset);
  if (!zillowConfigured && !mlsConfigured)
    return NextResponse.json({ configured: false } satisfies MarketData);

  const result: MarketData = {
    configured: true,
    zillowConfigured,
    mlsConfigured,
    errors: [],
    checkedAt: new Date().toISOString(),
  };

  const [zillow, mls] = await Promise.allSettled([
    credentials.zillowToken
      ? fetchZillow(credentials.zillowToken, address, town)
      : Promise.resolve(undefined),
    credentials.mlsToken && credentials.mlsDataset
      ? fetchMls(credentials.mlsToken, credentials.mlsDataset, address, lat, lon)
      : Promise.resolve(undefined),
  ]);

  if (zillow.status === "fulfilled") result.zillow = zillow.value;
  else result.errors?.push(`Zillow: ${zillow.reason instanceof Error ? zillow.reason.message : "lookup failed"}`);
  if (mls.status === "fulfilled") result.mls = mls.value;
  else result.errors?.push(`MLS: ${mls.reason instanceof Error ? mls.reason.message : "lookup failed"}`);

  return NextResponse.json(result, {
    headers: { "cache-control": "private, no-store" },
  });
}
