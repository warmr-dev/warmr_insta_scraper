"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { Account } from "@/lib/queries";

const INSTAGRAM_URL = "https://www.instagram.com/";

export function SessionManager({ accounts }: { accounts: Account[] }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [cookies, setCookies] = useState("");
  const [fileName, setFileName] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [checking, setChecking] = useState<string | null>(null);

  async function onFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setFileName(file.name);
    setCookies(await file.text());
  }

  async function addSession(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const response = await fetch("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, cookies }),
      });
      // Not every failure answers with JSON: a platform-level timeout or crash
      // returns Next's HTML error page, and parsing that threw "Unexpected
      // token '<'" — hiding whatever actually went wrong.
      const text = await response.text();
      let data: {
        error?: string;
        detail?: string;
        alive?: boolean;
        missing?: string[];
      };
      try {
        data = JSON.parse(text);
      } catch {
        setResult({
          ok: false,
          text: `Server error (HTTP ${response.status}) — the response was not JSON. Check the deployment logs.`,
        });
        return;
      }

      if (!response.ok) {
        setResult({
          ok: false,
          text: data.detail ?? data.error ?? "Failed to save",
        });
        return;
      }
      const missing = data.missing?.length
        ? ` Missing: ${data.missing.join(", ")}.`
        : "";
      setResult({
        ok: data.alive === true,
        text: data.alive
          ? `Saved and verified — ${data.detail}`
          : `Saved but NOT working — ${data.detail}.${missing}`,
      });
      if (data.alive) {
        setUsername("");
        setCookies("");
        setFileName("");
      }
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  async function recheck(name: string) {
    setChecking(name);
    try {
      await fetch("/api/sessions", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: name }),
      });
      router.refresh();
    } finally {
      setChecking(null);
    }
  }

  async function remove(name: string) {
    await fetch("/api/sessions", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name }),
    });
    router.refresh();
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={() => setOpen(!open)}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-sky-500"
        >
          {open ? "Cancel" : "+ Add session"}
        </button>
        <a
          href={INSTAGRAM_URL}
          target="_blank"
          rel="noreferrer"
          className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 transition hover:bg-slate-800"
        >
          Open Instagram to renew ↗
        </a>
      </div>

      {open && (
        <form
          onSubmit={addSession}
          className="rounded-xl border border-slate-800 bg-slate-900/50 p-5"
        >
          <p className="text-sm text-slate-400">
            Cookies cannot be renewed from code — copy them from a logged-in
            browser. Open{" "}
            <span className="text-slate-300">
              DevTools → Application → Cookies → instagram.com
            </span>
            , or export them with a cookie extension and upload the JSON here.
          </p>

          <label className="mt-4 block text-sm text-slate-300">
            Instagram username
            <input
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="yrsayl7"
              className="mt-1 w-full max-w-xs rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 outline-none focus:border-sky-500"
            />
          </label>

          <div className="mt-4">
            <label className="block text-sm text-slate-300">
              Cookie file (JSON export)
              <input
                type="file"
                accept=".json,.txt"
                onChange={onFile}
                className="mt-1 block w-full text-sm text-slate-400 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-800 file:px-4 file:py-2 file:text-sm file:text-slate-200 hover:file:bg-slate-700"
              />
            </label>
            {fileName && (
              <p className="mt-1 text-xs text-emerald-400">Loaded {fileName}</p>
            )}
          </div>

          <label className="mt-4 block text-sm text-slate-300">
            …or paste the cookie string
            <textarea
              value={cookies}
              onChange={(e) => {
                setCookies(e.target.value);
                setFileName("");
              }}
              rows={4}
              placeholder="sessionid=…; csrftoken=…; ds_user_id=…; ig_did=…; mid=…; datr=…; rur=…"
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs outline-none focus:border-sky-500"
            />
          </label>

          {result && (
            <p
              className={`mt-4 rounded-lg px-3 py-2 text-sm ${
                result.ok
                  ? "bg-emerald-950/60 text-emerald-300"
                  : "bg-amber-950/60 text-amber-300"
              }`}
            >
              {result.text}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !cookies}
            className="mt-5 rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-sky-500 disabled:opacity-50"
          >
            {busy ? "Verifying with Instagram…" : "Save session"}
          </button>
        </form>
      )}

      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-900/70 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3 font-medium">Account</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium">Cookies</th>
              <th className="px-4 py-3 font-medium">Updated</th>
              <th className="px-4 py-3 font-medium">Last error</th>
              <th className="px-4 py-3 font-medium"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {accounts.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                  No sessions yet — add one to start collecting.
                </td>
              </tr>
            ) : (
              accounts.map((account) => (
                <tr key={account.username} className="hover:bg-slate-900/40">
                  <td className="px-4 py-3 font-medium">@{account.username}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs ${
                        account.is_active
                          ? "bg-emerald-950/60 text-emerald-300"
                          : "bg-red-950/60 text-red-300"
                      }`}
                    >
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          account.is_active ? "bg-emerald-400" : "bg-red-400"
                        }`}
                      />
                      {account.is_active ? "Live" : "Dead"}
                    </span>
                  </td>
                  <td className="px-4 py-3 tabular-nums">
                    <span
                      className={
                        Number(account.cookie_count) < 7 ? "text-amber-400" : ""
                      }
                    >
                      {account.cookie_count}/7
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-400">
                    {new Date(account.updated_at).toLocaleString("en-GB", {
                      dateStyle: "short",
                      timeStyle: "short",
                    })}
                  </td>
                  <td className="max-w-xs truncate px-4 py-3 text-xs text-slate-500">
                    {account.last_error ?? "—"}
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 text-right">
                    <button
                      onClick={() => recheck(account.username)}
                      disabled={checking === account.username}
                      className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs transition hover:bg-slate-800 disabled:opacity-50"
                    >
                      {checking === account.username ? "Checking…" : "Re-check"}
                    </button>
                    <button
                      onClick={() => remove(account.username)}
                      className="ml-2 rounded-lg border border-slate-800 px-3 py-1.5 text-xs text-slate-500 transition hover:bg-red-950/40 hover:text-red-300"
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
