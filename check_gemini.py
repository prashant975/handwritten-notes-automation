from __future__ import annotations

import argparse
from src.ai_client import GeminiClient
from src.config import DEFAULT_MODEL, DEFAULT_PROVIDER, GEMINI_API_KEY, extract_api_key, mask_secret


def main():
    parser = argparse.ArgumentParser(description="Test Gemini API key and model.")
    parser.add_argument("--api-key", default=GEMINI_API_KEY, help="Gemini API key, URL, or pasted curl containing ?key=")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=["auto", "google_genai_sdk", "developer_rest", "aiplatform_rest"])
    args = parser.parse_args()
    key = extract_api_key(args.api_key)
    print(f"Testing Gemini key: {mask_secret(key)}")
    print(f"Model: {args.model}")
    print(f"Provider: {args.provider}")
    client = GeminiClient(api_key=key, model=args.model, provider=args.provider)
    resp = client.test_connection()
    print("OK")
    print(f"Provider used: {resp.provider}")
    print(f"Model used: {resp.model}")
    print(f"Response: {resp.text[:200]}")


if __name__ == "__main__":
    main()
