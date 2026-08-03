import "server-only";

import { del, get, put } from "@vercel/blob";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

const CREDENTIALS_PATHNAME = "private/market-credentials.v1.enc";

export type MarketCredentials = {
  zillowToken?: string;
  mlsToken?: string;
  mlsDataset?: string;
};

export function sharedCredentialStorageConfigured() {
  return Boolean(process.env.BLOB_READ_WRITE_TOKEN || process.env.BLOB_STORE_ID);
}

function requireSharedCredentialStorage() {
  if (!sharedCredentialStorageConfigured())
    throw new Error(
      "Shared credential storage is not configured. Connect a private Vercel Blob store to this project.",
    );
}

function base64Url(bytes: Uint8Array) {
  return Buffer.from(bytes).toString("base64url");
}

function encryptionSecret() {
  const secret = process.env.APP_ACCESS_KEY?.trim();
  if (!secret || secret.length < 24)
    throw new Error("APP_ACCESS_KEY must contain at least 24 characters");
  return secret;
}

async function encryptionKey() {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    encoder.encode(`market-credentials:${encryptionSecret()}`),
  );
  return crypto.subtle.importKey("raw", digest, "AES-GCM", false, [
    "encrypt",
    "decrypt",
  ]);
}

export async function sealCredentials(credentials: MarketCredentials) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const encrypted = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    await encryptionKey(),
    encoder.encode(JSON.stringify(credentials)),
  );
  return `${base64Url(iv)}.${base64Url(new Uint8Array(encrypted))}`;
}

export async function unsealCredentials(value: string | undefined) {
  if (!value) return {} as MarketCredentials;
  const [encodedIv, encodedPayload, extra] = value.split(".");
  if (!encodedIv || !encodedPayload || extra) return {} as MarketCredentials;

  try {
    const decrypted = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: Buffer.from(encodedIv, "base64url") },
      await encryptionKey(),
      Buffer.from(encodedPayload, "base64url"),
    );
    const parsed = JSON.parse(decoder.decode(decrypted)) as MarketCredentials;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {} as MarketCredentials;
  }
}

export async function readSharedCredentials() {
  requireSharedCredentialStorage();
  const result = await get(CREDENTIALS_PATHNAME, {
    access: "private",
    // Credentials are tiny and infrequently read. Bypass the CDN so a save or
    // removal is visible immediately in every browser.
    useCache: false,
  });
  if (!result || result.statusCode !== 200) return {} as MarketCredentials;
  return unsealCredentials(await new Response(result.stream).text());
}

export async function writeSharedCredentials(credentials: MarketCredentials) {
  requireSharedCredentialStorage();
  await put(CREDENTIALS_PATHNAME, await sealCredentials(credentials), {
    access: "private",
    addRandomSuffix: false,
    allowOverwrite: true,
    cacheControlMaxAge: 60,
    contentType: "application/octet-stream",
  });
}

export async function deleteSharedCredentials() {
  requireSharedCredentialStorage();
  await del(CREDENTIALS_PATHNAME);
}
