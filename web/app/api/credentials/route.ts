import { NextResponse } from "next/server";
import { isAuthenticated } from "@/lib/server-auth";
import {
  deleteSharedCredentials,
  readSharedCredentials,
  writeSharedCredentials,
} from "@/lib/server-credentials";

type CredentialInput = {
  zillowToken?: unknown;
  mlsToken?: unknown;
  mlsDataset?: unknown;
};

function clean(value: unknown, maxLength: number) {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  if (trimmed.length > maxLength) throw new Error("Credential value is too long.");
  return trimmed;
}

function storageError(reason: unknown) {
  const message = reason instanceof Error ? reason.message : "Shared credential storage failed.";
  const missing = message.startsWith("Shared credential storage is not configured.");
  return NextResponse.json(
    { error: missing ? message : "Could not access shared credential storage." },
    { status: missing ? 503 : 502 },
  );
}

export async function GET() {
  if (!(await isAuthenticated()))
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  try {
    const credentials = await readSharedCredentials();
    return NextResponse.json({
      zillow: Boolean(credentials.zillowToken),
      mls: Boolean(credentials.mlsToken && credentials.mlsDataset),
      dataset: credentials.mlsDataset ?? null,
    });
  } catch (reason) {
    return storageError(reason);
  }
}

export async function PUT(request: Request) {
  if (!(await isAuthenticated()))
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  let input: CredentialInput;
  let cleaned: {
    zillowToken?: string;
    mlsToken?: string;
    mlsDataset?: string;
  };
  try {
    const parsed = (await request.json()) as CredentialInput | null;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      throw new Error("Invalid credentials.");
    input = parsed;
    cleaned = {
      zillowToken: clean(input.zillowToken, 1_200),
      mlsToken: clean(input.mlsToken, 1_200),
      mlsDataset: clean(input.mlsDataset, 128),
    };
  } catch (reason) {
    return NextResponse.json(
      { error: reason instanceof Error ? reason.message : "Invalid credentials." },
      { status: 400 },
    );
  }

  try {
    const current = await readSharedCredentials();
    const credentials = {
      zillowToken: cleaned.zillowToken ?? current.zillowToken,
      mlsToken: cleaned.mlsToken ?? current.mlsToken,
      mlsDataset: cleaned.mlsDataset ?? current.mlsDataset,
    };

    await writeSharedCredentials(credentials);
    return NextResponse.json({
      ok: true,
      zillow: Boolean(credentials.zillowToken),
      mls: Boolean(credentials.mlsToken && credentials.mlsDataset),
      dataset: credentials.mlsDataset ?? null,
    });
  } catch (reason) {
    return storageError(reason);
  }
}

export async function DELETE() {
  if (!(await isAuthenticated()))
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  try {
    await deleteSharedCredentials();
    return NextResponse.json({ ok: true });
  } catch (reason) {
    return storageError(reason);
  }
}
