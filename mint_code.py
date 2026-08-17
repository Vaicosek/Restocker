#!/usr/bin/env python3
"""mint_code.py — mint a website login code for an account you own.

Same thing /website_login does, minus Discord. Writes a one-time code into
web_login_codes.yml; you paste it into the site's "paste your code here" box.

RUN THIS ON THE SERVER THAT SERVES THE LIVE SITE (the Wisp box), in the same
folder the bot runs from — the code must land in the web_login_codes.yml the
running site actually reads, or the login box will say "invalid or expired".

    python3 mint_code.py 776151361599438869 --name Jesse

Prints a code. Paste it into the site within 15 minutes. It is single-use.
"""
import argparse, os, secrets, string, time, sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML not installed here. Run this where the bot runs (it has yaml).")

# The site resolves data files under data/state/ relative to the bot. Match that.
# If your layout differs, pass --file with the real path to web_login_codes.yml.
DEFAULT_CANDIDATES = [
    "data/state/web_login_codes.yml",
    "data/web_login_codes.yml",
    "web_login_codes.yml",
]

def find_codes_file(explicit):
    if explicit:
        return explicit
    for c in DEFAULT_CANDIDATES:
        if os.path.exists(c):
            return c
    # none exist yet — the site creates data/state/ ; default to that so the
    # running site will read what we write.
    return "data/state/web_login_codes.yml"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("user_id", help="Discord id of the account to log in as")
    ap.add_argument("--name", default="", help="display name (cosmetic)")
    ap.add_argument("--file", default="", help="path to web_login_codes.yml (auto if omitted)")
    ap.add_argument("--minutes", type=int, default=15, help="code lifetime")
    a = ap.parse_args()

    path = find_codes_file(a.file)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

    codes = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                codes = yaml.safe_load(f) or {}
        except Exception:
            codes = {}

    # Code shape matches _handle_api_link: uppercased on submit, so mint uppercase.
    alpha = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alpha) for _ in range(6))
    while code in codes:
        code = "".join(secrets.choice(alpha) for _ in range(6))

    codes[code] = {
        "user_id": str(a.user_id),
        "name": a.name,
        "expires": time.time() + a.minutes * 60,
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(codes, f, allow_unicode=True, sort_keys=False)

    print("=" * 44)
    print(f"  CODE:  {code}")
    print(f"  for:   {a.user_id}  {a.name}")
    print(f"  file:  {os.path.abspath(path)}")
    print(f"  valid: {a.minutes} min, single use")
    print("=" * 44)
    print("Paste it into the site's login box.")
    print("If it says invalid: this wrote to the wrong file —")
    print("find the site's real web_login_codes.yml and pass --file <path>.")

if __name__ == "__main__":
    main()
