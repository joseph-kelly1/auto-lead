import { NextResponse } from "next/server";
import {
  SESSION_COOKIE,
  SESSION_MAX_AGE_SECONDS,
  accessKeyMatches,
  createSessionToken,
  isAccessKeyConfigured,
} from "@/lib/session";

export async function POST(request: Request) {
  if (!isAccessKeyConfigured())
    return NextResponse.json(
      { error: "APP_ACCESS_KEY is not configured on the server." },
      { status: 503 },
    );

  const body = (await request.json().catch(() => null)) as { key?: unknown } | null;
  const key = typeof body?.key === "string" ? body.key : "";
  if (!(await accessKeyMatches(key)))
    return NextResponse.json({ error: "Incorrect access key." }, { status: 401 });

  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, await createSessionToken(), {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    maxAge: SESSION_MAX_AGE_SECONDS,
    path: "/",
  });
  return response;
}
