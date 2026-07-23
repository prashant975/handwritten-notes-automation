/**
 * verify_onboarding.mjs — JS twin of verify_onboarding.py.
 *
 * Self-check that a JS / Node / frontend app is correctly wired to the PW
 * proxy. Run with Node 18+ from the folder where pw_access.js was copied:
 *
 *   node verify_onboarding.mjs [path/to/.env] [optional_google_token]
 *
 * Checks:
 *   1. pw_access.js imports and APP_NAME is set
 *   2. APP_NAME is registered on the proxy (/api/apps)
 *   3. no provider API keys remain in the given .env
 *   4. (if a Google token is passed) the live whitelist check works
 */
import { readFileSync, existsSync } from "node:fs";
import { APP_NAME, PROXY_BASE_URL, checkAllowedStatus } from "./pw_access.js";

const envPath = process.argv[2] || ".env";
const token = process.argv[3] || "";
let ok = true;

// Manual timeout (cleared on completion) — avoids a dangling timer handle that
// can trip a libuv assertion on Windows when the process exits.
async function fetchWithTimeout(url, ms) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try { return await fetch(url, { signal: ctrl.signal, headers: { connection: "close" } }); }
  finally { clearTimeout(t); }
}

console.log(`APP_NAME = ${JSON.stringify(APP_NAME)}`);
console.log(`PROXY    = ${PROXY_BASE_URL}`);

// 2. registered on the proxy?
try {
  const r = await fetchWithTimeout(`${PROXY_BASE_URL}/api/apps`, 20000);
  const apps = r.ok ? (await r.json()).apps || [] : [];
  if (apps.includes(APP_NAME)) {
    console.log(`PASS: '${APP_NAME}' is registered on the proxy`);
  } else {
    console.log(`FAIL: '${APP_NAME}' not in /api/apps ${JSON.stringify(apps)} — `
      + `add its column to the Whitelisted tab (exact spelling).`);
    ok = false;
  }
} catch (e) {
  console.log("WARN: could not reach /api/apps:", e.message);
  ok = false;
}

// 3. no provider keys left in .env
const leaked = [];
if (existsSync(envPath)) {
  for (const line of readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const s = line.trim();
    if (!s || s.startsWith("#") || !s.includes("=")) continue;
    const eq = s.indexOf("=");
    const name = s.slice(0, eq).trim();
    const val = s.slice(eq + 1).trim();
    if (val && /GEMINI|MATHPIX|SARVAM|ELEVEN|OPENAI/i.test(name)) leaked.push(name);
  }
}
if (leaked.length) {
  console.log(`FAIL: provider keys still present in ${envPath}: ${leaked.join(", ")}`);
  ok = false;
} else {
  console.log(`PASS: no provider keys in ${envPath}`);
}

// 4. optional live whitelist check
if (token) {
  const status = await checkAllowedStatus(token);
  console.log(`allowlist check for supplied token: ${status}`);
  if (status === "error") console.log("  (token invalid/expired, or proxy unreachable)");
  ok = ok && (status === "allowed" || status === "denied");
}

console.log("\nRESULT:", ok ? "ALL GOOD" : "ISSUES FOUND");
// Set the code and let Node exit naturally — calling process.exit() here can
// trip a libuv assertion on Windows while fetch's socket is still closing.
process.exitCode = ok ? 0 : 1;
