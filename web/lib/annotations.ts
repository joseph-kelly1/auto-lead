"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * The only writes in the app. localStorage, plus explicit export and import.
 *
 * Vercel function filesystems are ephemeral, so a server-side SQLite would not
 * survive; and for one person on one machine a database is infrastructure with
 * no user. Keyed on `loc_id|prop_id` because a parcel polygon can legitimately
 * carry two single-family records.
 */

const KEY = "teardown-annotations-v1";

export const STATUSES = ["new", "watching", "vetting", "contacted", "passed"] as const;
export type Status = (typeof STATUSES)[number];

/** The things that kill a deal, none of which are in the assessor data. */
export const CHECKS = [
  { key: "demo_delay", label: "Demolition delay bylaw checked" },
  { key: "sewer", label: "Sewer confirmed (not septic / Title 5)" },
  { key: "wetlands", label: "Wetlands and resource areas checked" },
  { key: "envelope", label: "Setback, coverage and height envelope fits" },
  { key: "abatement", label: "Pre-1978 asbestos and lead assessed" },
] as const;

export type Annotation = {
  status?: Status;
  dismissed?: boolean;
  note?: string;
  checks?: Partial<Record<(typeof CHECKS)[number]["key"], boolean>>;
  updated?: string;
};

export type Store = Record<string, Annotation>;

function read(): Store {
  if (typeof window === "undefined") return {};
  try {
    return JSON.parse(window.localStorage.getItem(KEY) ?? "{}") as Store;
  } catch {
    return {};
  }
}

export function useAnnotations() {
  const [store, setStore] = useState<Store>({});

  useEffect(() => {
    setStore(read());
    const sync = (e: StorageEvent) => e.key === KEY && setStore(read());
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);

  const update = useCallback((id: string, patch: Annotation) => {
    setStore((prev) => {
      const merged: Annotation = {
        ...prev[id],
        ...patch,
        checks: { ...prev[id]?.checks, ...patch.checks },
        updated: new Date().toISOString(),
      };
      const next = { ...prev, [id]: merged };
      window.localStorage.setItem(KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  const replace = useCallback((next: Store) => {
    window.localStorage.setItem(KEY, JSON.stringify(next));
    setStore(next);
  }, []);

  return { store, update, replace };
}

export function exportFile(store: Store) {
  const blob = new Blob([JSON.stringify(store, null, 2)], {
    type: "application/json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `teardown-notes-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/** Merged, not replaced: importing a partial file must not drop your notes. */
export async function importFile(file: File, current: Store): Promise<Store> {
  const parsed = JSON.parse(await file.text()) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
    throw new Error("that file is not an annotations export");
  const merged: Store = { ...current };
  for (const [id, a] of Object.entries(parsed as Store)) {
    const mine = current[id];
    // Last write wins per parcel, by the timestamp in the file.
    if (!mine || (a.updated ?? "") >= (mine.updated ?? "")) merged[id] = a;
  }
  return merged;
}

export function indexAnnotations(store: Store) {
  const dismissed: string[] = [];
  const byStatus: Record<string, string[]> = {};
  for (const [id, a] of Object.entries(store)) {
    if (a.dismissed) dismissed.push(id);
    if (a.status) (byStatus[a.status] ??= []).push(id);
  }
  return { dismissed, byStatus };
}
