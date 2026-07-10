"""
verify_onboarding.py — self-check that an app is correctly wired to the PW
proxy. The onboarding AI runs this at the end so nothing is missed.

Run from the app's backend folder (where pw_access.py was copied):

    python verify_onboarding.py [path/to/.env] [optional_google_token]

Checks:
  1. pw_access imports and APP_NAME is set
  2. APP_NAME is registered on the proxy (/api/apps)
  3. no provider API keys remain in the given .env
  4. (if a Google token is passed) the live whitelist check works
"""
import os
import sys


def main():
    env_path = sys.argv[1] if len(sys.argv) > 1 else ".env"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    ok = True

    # 1. client present + configured
    try:
        import pw_access
    except Exception as e:
        print("FAIL: cannot import pw_access:", e)
        sys.exit(1)
    print(f"APP_NAME = {pw_access.APP_NAME!r}")
    print(f"PROXY    = {pw_access.PROXY_BASE_URL}")

    import requests

    # 2. registered on the proxy?
    try:
        r = requests.get(f"{pw_access.PROXY_BASE_URL}/api/apps", timeout=20)
        apps = r.json().get("apps", []) if r.status_code == 200 else []
        if pw_access.APP_NAME in apps:
            print(f"PASS: '{pw_access.APP_NAME}' is registered on the proxy")
        else:
            print(f"FAIL: '{pw_access.APP_NAME}' not in /api/apps {apps} — "
                  f"add its column to the Whitelisted tab (exact spelling).")
            ok = False
    except Exception as e:
        print("WARN: could not reach /api/apps:", e)
        ok = False

    # 3. no provider keys left in .env
    leaked = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("#") or "=" not in s:
                    continue
                name, _, val = s.partition("=")
                if val.strip() and any(k in name.upper()
                                       for k in ("GEMINI", "MATHPIX", "SARVAM", "OPENAI")):
                    leaked.append(name.strip())
    if leaked:
        print(f"FAIL: provider keys still present in {env_path}: {leaked}")
        ok = False
    else:
        print(f"PASS: no provider keys in {env_path}")

    # 4. optional live whitelist check
    if token:
        status = pw_access.check_allowed_status(token)
        print(f"allowlist check for supplied token: {status}")
        if status == "error":
            print("  (token invalid/expired, or proxy unreachable)")
        ok = ok and status in ("allowed", "denied")

    print("\nRESULT:", "ALL GOOD" if ok else "ISSUES FOUND")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
