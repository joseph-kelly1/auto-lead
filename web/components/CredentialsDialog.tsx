"use client";

import { FormEvent, useState } from "react";
import { clearMarketCache } from "@/lib/market";

type CredentialStatus = {
  zillow: boolean;
  mls: boolean;
  dataset: string | null;
};

export default function CredentialsDialog() {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [zillowToken, setZillowToken] = useState("");
  const [mlsToken, setMlsToken] = useState("");
  const [mlsDataset, setMlsDataset] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function show() {
    setOpen(true);
    setMessage(null);
    try {
      const response = await fetch("/api/credentials");
      const result = (await response.json()) as CredentialStatus & { error?: string };
      if (!response.ok) throw new Error(result.error ?? "Could not read credentials.");
      setStatus(result);
      setMlsDataset(result.dataset ?? "");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Could not read credentials.");
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setMessage(null);
    try {
      const response = await fetch("/api/credentials", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ zillowToken, mlsToken, mlsDataset }),
      });
      const result = (await response.json()) as CredentialStatus & { error?: string };
      if (!response.ok) throw new Error(result.error ?? "Could not save credentials.");
      setStatus(result);
      setZillowToken("");
      setMlsToken("");
      clearMarketCache();
      setMessage("Shared credentials saved for everyone with app access.");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Could not save credentials.");
    } finally {
      setSaving(false);
    }
  }

  async function clear() {
    if (!window.confirm("Remove the shared Zillow and MLS credentials for everyone?"))
      return;
    setMessage(null);
    try {
      const response = await fetch("/api/credentials", { method: "DELETE" });
      const result = (await response.json()) as { error?: string };
      if (!response.ok)
        throw new Error(result.error ?? "Could not remove shared credentials.");
      setStatus({ zillow: false, mls: false, dataset: null });
      setZillowToken("");
      setMlsToken("");
      setMlsDataset("");
      clearMarketCache();
      setMessage("Shared credentials removed for everyone.");
    } catch (reason) {
      setMessage(
        reason instanceof Error
          ? reason.message
          : "Could not remove shared credentials.",
      );
    }
  }

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.assign("/login");
  }

  return (
    <>
      <button
        type="button"
        onClick={show}
        className="w-full rounded-sm border border-line py-1.5 hover:border-muted"
      >
        API credentials
      </button>

      {open && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="credentials-title"
        >
          <form
            onSubmit={save}
            className="max-h-full w-full max-w-[520px] space-y-5 overflow-y-auto rounded-sm border border-line bg-panel p-5 shadow-2xl"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="label mb-1">Private configuration</p>
                <h2 id="credentials-title" className="text-[15px] font-semibold">
                  Market-data credentials
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close credentials"
                className="px-1 text-muted hover:text-text"
              >
                ✕
              </button>
            </div>

            <p className="text-[12px] leading-relaxed text-muted">
              Use Bridge server tokens, not browser tokens. Saved values are
              encrypted before entering private shared storage and are never
              shown again.
            </p>

            <label className="block">
              <span className="label mb-2 flex justify-between">
                Zillow Zestimate token
                <span className="normal-case tracking-normal">
                  {status?.zillow ? "configured" : "not configured"}
                </span>
              </span>
              <input
                type="password"
                value={zillowToken}
                onChange={(event) => setZillowToken(event.target.value)}
                autoComplete="off"
                placeholder={status?.zillow ? "Leave blank to keep existing token" : "Bridge server token"}
                className="w-full rounded-sm border border-line bg-surface px-3 py-2 font-mono text-[12px]"
              />
            </label>

            <div className="space-y-3 border-t border-line pt-4">
              <label className="block">
                <span className="label mb-2 flex justify-between">
                  MLS PIN / Bridge token
                  <span className="normal-case tracking-normal">
                    {status?.mls ? "configured" : "not configured"}
                  </span>
                </span>
                <input
                  type="password"
                  value={mlsToken}
                  onChange={(event) => setMlsToken(event.target.value)}
                  autoComplete="off"
                  placeholder={status?.mls ? "Leave blank to keep existing token" : "Bridge server token"}
                  className="w-full rounded-sm border border-line bg-surface px-3 py-2 font-mono text-[12px]"
                />
              </label>
              <label className="block">
                <span className="label mb-2 block">MLS dataset code</span>
                <input
                  type="text"
                  value={mlsDataset}
                  onChange={(event) => setMlsDataset(event.target.value)}
                  autoComplete="off"
                  placeholder="From Bridge Approved Datasets"
                  className="w-full rounded-sm border border-line bg-surface px-3 py-2 font-mono text-[12px]"
                />
              </label>
            </div>

            {message && <p className="text-[12px] text-select">{message}</p>}

            <div className="flex flex-wrap gap-2">
              <button
                type="submit"
                disabled={saving}
                className="rounded-sm border border-select px-3 py-1.5 text-select disabled:opacity-50"
              >
                {saving ? "Saving…" : "Save credentials"}
              </button>
              <button
                type="button"
                onClick={clear}
                className="rounded-sm border border-line px-3 py-1.5 text-muted hover:border-muted"
              >
                Remove credentials
              </button>
              <button
                type="button"
                onClick={logout}
                className="ml-auto rounded-sm border border-line px-3 py-1.5 text-muted hover:border-muted"
              >
                Log out
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
