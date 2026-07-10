"""No provider key is needed any more.

This app now calls Gemini through the shared PW proxy (see pw_access.py), which
holds the Gemini key on its side. There is nothing to paste here — sign in with
your @pw.live Google account in the app instead.

Kept as a stub so older shortcuts that call it don't break.
"""

print(
    "No Gemini key needed. This app uses the PW proxy — the Gemini key lives on\n"
    "the proxy, not in this app. Just sign in with your @pw.live Google account.\n"
    "See pw-app-kit/CONNECT-TO-PW-PROXY.md for details."
)
