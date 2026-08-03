const encoder = new TextEncoder();

export const SESSION_COOKIE = "teardown_session";
export const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30;

function appKey() {
  const key = process.env.APP_ACCESS_KEY?.trim();
  return key && key.length >= 24 ? key : null;
}

function bytesToBase64Url(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlToBytes(value: string) {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function hmacKey(secret: string) {
  return crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

async function signature(secret: string, value: string) {
  const signed = await crypto.subtle.sign(
    "HMAC",
    await hmacKey(secret),
    encoder.encode(value),
  );
  return new Uint8Array(signed);
}

export function isAccessKeyConfigured() {
  return appKey() !== null;
}

export async function accessKeyMatches(candidate: string) {
  const expected = appKey();
  if (!expected || !candidate) return false;

  const candidateSignature = await signature(candidate, "teardown-login");
  return crypto.subtle.verify(
    "HMAC",
    await hmacKey(expected),
    candidateSignature,
    encoder.encode("teardown-login"),
  );
}

export async function createSessionToken() {
  const secret = appKey();
  if (!secret) throw new Error("APP_ACCESS_KEY must contain at least 24 characters");

  const expires = String(Date.now() + SESSION_MAX_AGE_SECONDS * 1000);
  const signed = await signature(secret, expires);
  return `${expires}.${bytesToBase64Url(signed)}`;
}

export async function verifySessionToken(token: string | undefined) {
  const secret = appKey();
  if (!secret || !token) return false;

  const [expires, encodedSignature, extra] = token.split(".");
  if (!expires || !encodedSignature || extra || Number(expires) <= Date.now())
    return false;

  try {
    return crypto.subtle.verify(
      "HMAC",
      await hmacKey(secret),
      base64UrlToBytes(encodedSignature),
      encoder.encode(expires),
    );
  } catch {
    return false;
  }
}
