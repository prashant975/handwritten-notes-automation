/**
 * pw_access.js — shared PW app-access client for JavaScript apps.
 *
 * The JS twin of pw_access.py. Use it in:
 *   - browser SPAs / static sites (React, Vue, plain JS), and
 *   - Node / Vercel / edge backends.
 *
 * Talks ONLY to the shared proxy and holds NO keys — so it's safe even in a
 * public frontend bundle. Every call takes the signed-in user's Google token
 * (access token OR id token); the proxy verifies it, checks the app-wise
 * whitelist, calls the paid API with its own key, logs usage, returns result.
 *
 * Works anywhere `fetch` exists: modern browsers, Node 18+, edge runtimes.
 * This file is ESM. For CommonJS, swap `export` for `module.exports = { ... }`.
 */

// --- PER-APP CONFIG — the only thing each app changes ---------------------
// APP_NAME must EXACTLY match a header in row 1 of the `Whitelisted` tab.
export const APP_NAME = "Final ZIP Package";
export const PROXY_BASE_URL = "https://pw-apps-proxy.vercel.app";

const TIMEOUT_MS = 30_000;      // allowlist / logging
const AI_TIMEOUT_MS = 300_000;  // gemini / mathpix / sarvam

export class PWAccessError extends Error {}

function authHeaders(googleToken) {
  return { Authorization: `Bearer ${googleToken}`, "Content-Type": "application/json" };
}

async function postJSON(path, googleToken, body, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(`${PROXY_BASE_URL}${path}`, {
      method: "POST",
      headers: authHeaders(googleToken),
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } finally {
    clearTimeout(timer);
  }
}

/** "allowed" | "denied" | "error" — lets callers treat "proxy unreachable"
 *  (error) differently from a real "no" (denied). */
export async function checkAllowedStatus(googleToken, app = APP_NAME) {
  if (!googleToken) return "denied";
  try {
    const r = await postJSON("/api/allowlist", googleToken, { app }, TIMEOUT_MS);
    if (r.status === 200) return (await r.json()).allowed ? "allowed" : "denied";
    if (r.status === 403) return "denied";
    return "error";
  } catch {
    return "error";
  }
}

/** Call before EVERY paid/main run. Fail closed (false on any error). */
export async function checkAllowed(googleToken, app = APP_NAME) {
  return (await checkAllowedStatus(googleToken, app)) === "allowed";
}

/** Append one Usage Cost row per item. Never throws — returns null on failure. */
export async function logUsage(googleToken, { filename, input_unit, count, items, app = APP_NAME }) {
  try {
    const r = await postJSON("/api/usage-log", googleToken,
      { app, filename, input_unit, count, items }, TIMEOUT_MS);
    return r.status === 200 ? await r.json() : null;
  } catch {
    return null;
  }
}

/** Accumulates a task's provider usage and writes ONE row per provider on
 *  flush() — so many calls to the same provider collapse into a single Usage
 *  Cost row (one Gemini row, one Mathpix row, one Sarvam row). */
export class UsageSession {
  constructor(googleToken, { filename = "", input_unit = "", count = null, app = APP_NAME } = {}) {
    this.token = googleToken;
    this.filename = filename;
    this.input_unit = input_unit;
    this.count = count;
    this.app = app;
    this._byModel = new Map();
  }
  add(model, tokensIn = 0, tokensOut = 0, costInr = 0) {
    const k = model || "";
    const a = this._byModel.get(k) || { tokens_in: 0, tokens_out: 0, cost_inr: 0 };
    a.tokens_in += Number(tokensIn || 0);
    a.tokens_out += Number(tokensOut || 0);
    a.cost_inr += Number(costInr || 0);
    this._byModel.set(k, a);
  }
  async flush() {
    const items = [...this._byModel.entries()].map(([model, v]) => ({
      model, tokens_in: v.tokens_in, tokens_out: v.tokens_out,
      cost_inr: Math.round(v.cost_inr * 1e4) / 1e4,
    }));
    this._byModel.clear();
    if (!items.length) return null;
    return logUsage(this.token,
      { filename: this.filename, input_unit: this.input_unit, count: this.count, items, app: this.app });
  }
}

/** Gemini through the proxy. Returns { ok, result, usage, cost_inr };
 *  `result` is the raw generateContent response. */
export async function geminiGenerate(googleToken,
  { model, request, filename = "", input_unit = "", count = null, app = APP_NAME, session = null }) {
  const body = { app, model, request, filename, input_unit, count };
  if (session) body.log = false;
  const r = await postJSON("/api/gemini/generate", googleToken, body, AI_TIMEOUT_MS);
  if (r.status !== 200) throw new PWAccessError(`gemini proxy ${r.status}: ${(await r.text()).slice(0, 300)}`);
  const data = await r.json();
  if (session) session.add(data.model || model, data.usage?.tokens_in, data.usage?.tokens_out, data.cost_inr);
  return data;
}

/** Mathpix OCR through the proxy. Returns { ok, result, cost_inr }. */
export async function mathpixOcr(googleToken, { request, filename = "", count = 1, app = APP_NAME, session = null }) {
  const body = { app, request, filename, count };
  if (session) body.log = false;
  const r = await postJSON("/api/mathpix/ocr", googleToken, body, AI_TIMEOUT_MS);
  if (r.status !== 200) throw new PWAccessError(`mathpix proxy ${r.status}: ${(await r.text()).slice(0, 300)}`);
  const data = await r.json();
  if (session) session.add(data.model || "Mathpix OCR", data.usage?.tokens_in, data.usage?.tokens_out, data.cost_inr);
  return data;
}

/** Sarvam Text-to-Speech through the proxy. Returns { ok, result, cost_inr };
 *  `result` includes the base64 audio (result.audios). */
export async function sarvamTts(googleToken, { request, filename = "", count = null, app = APP_NAME, session = null }) {
  const body = { app, request, filename, count };
  if (session) body.log = false;
  const r = await postJSON("/api/sarvam/tts", googleToken, body, AI_TIMEOUT_MS);
  if (r.status !== 200) throw new PWAccessError(`sarvam proxy ${r.status}: ${(await r.text()).slice(0, 300)}`);
  const data = await r.json();
  if (session) session.add(data.model || "Sarvam TTS", data.usage?.tokens_in, data.usage?.tokens_out, data.cost_inr);
  return data;
}

/*
USAGE EXAMPLES

--- Browser SPA (Sign in with Google → call the proxy directly) ---
  // 1) Get a Google token in the browser via Google Identity Services (GIS).
  //    For identity/allowlist, an id_token from the credential flow is enough:
  //      google.accounts.id.initialize({ client_id: YOUR_GOOGLE_CLIENT_ID, callback: onCredential });
  //    onCredential(resp) => const googleToken = resp.credential;   // a Google id_token
  // 2) Gate + call:
  import { checkAllowed, geminiGenerate } from "./pw_access.js";
  if (!(await checkAllowed(googleToken))) throw new Error("Not authorized");
  const out = await geminiGenerate(googleToken, {
    model: "gemini-2.5-flash",
    request: { contents: [{ role: "user", parts: [{ text: "Hello" }] }] },
    filename: "demo", input_unit: "No. of questions", count: 1,
  });
  console.log(out.result);   // raw Gemini response

--- Node / Vercel backend (token forwarded from your frontend) ---
  import { checkAllowed, sarvamTts } from "./pw_access.js";
  // googleToken = the user's token your backend received after sign-in
  if (!(await checkAllowed(googleToken))) return res.status(403).end();
  const tts = await sarvamTts(googleToken, {
    request: { text: "नमस्ते", target_language_code: "hi-IN", model: "bulbul:v3", speaker: "anushka" },
    count: 6,
  });
*/
