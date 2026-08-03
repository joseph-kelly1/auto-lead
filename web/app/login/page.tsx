"use client";

import { FormEvent, useState } from "react";

export default function LoginPage() {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const result = (await response.json()) as { error?: string };
      if (!response.ok) throw new Error(result.error ?? "Could not sign in.");
      window.location.assign("/");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not sign in.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="flex min-h-full items-center justify-center p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-[420px] space-y-5 rounded-sm border border-line bg-panel p-6"
      >
        <div>
          <p className="label mb-2">Private access</p>
          <h1 className="text-[22px] font-semibold tracking-tight">
            Teardown screener
          </h1>
          <p className="mt-2 leading-relaxed text-muted">
            Enter the shared access key to continue.
          </p>
        </div>

        <label className="block">
          <span className="label mb-2 block">Access key</span>
          <input
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            autoComplete="current-password"
            autoFocus
            required
            className="w-full rounded-sm border border-line bg-surface px-3 py-2 font-mono"
          />
        </label>

        {error && <p className="text-[12px] text-select">{error}</p>}

        <button
          type="submit"
          disabled={loading || !key}
          className="w-full rounded-sm border border-select bg-select/10 py-2 text-select disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? "Checking…" : "Continue"}
        </button>
      </form>
    </main>
  );
}
