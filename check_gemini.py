from __future__ import annotations

import argparse
import os

import pw_access
from src.ai_client import GeminiClient
from src.config import DEFAULT_MODEL


def main():
    parser = argparse.ArgumentParser(
        description="Check Gemini access through the PW proxy. The app ships no "
        "Gemini key; the proxy holds it. Pass a signed-in @pw.live Google token."
    )
    parser.add_argument("--google-token", default=os.getenv("PW_GOOGLE_TOKEN", ""), help="Signed-in @pw.live Google access/id token (or set PW_GOOGLE_TOKEN)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    print(f"Proxy: {pw_access.PROXY_BASE_URL}")
    print(f"App:   {pw_access.APP_NAME}")
    print(f"Model: {args.model}")

    token = args.google_token.strip()
    if not token:
        raise SystemExit("No token. Pass --google-token or set PW_GOOGLE_TOKEN.")

    status = pw_access.check_allowed_status(token)
    print(f"Allowlist status: {status}")
    if status == "expired":
        raise SystemExit("Token expired or invalid — grab a fresh Google token and retry.")
    if status != "allowed":
        raise SystemExit("Not allowed (or proxy unreachable). Fix access before testing generation.")

    client = GeminiClient(token, model=args.model)
    resp = client.test_connection()
    print("OK")
    print(f"Provider used: {resp.provider}")
    print(f"Model used: {resp.model}")
    print(f"Response: {resp.text[:200]}")


if __name__ == "__main__":
    main()
