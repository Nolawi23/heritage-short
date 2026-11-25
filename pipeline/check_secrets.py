"""`make check-secrets` (also the git pre-commit hook): fail if a tracked or staged file contains a key.

Checks (1) known key shapes and (2) the literal values from .env and ~/.kaggle/access_token.
Prints only file:line and the rule name, never the matched text.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    "Google API key (AIza...)": re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    "Google OAuth/AQ token (AQ....)": re.compile(r"AQ\.[0-9A-Za-z_\-]{20,}"),
    "Cloudflare token (cfut_...)": re.compile(r"cfut_[0-9A-Za-z_\-]{20,}"),
    "Kaggle token (KGAT_...)": re.compile(r"KGAT_[0-9A-Za-z_\-]{16,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def secret_values() -> dict[str, str]:
    vals = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip().strip("'\"")
                if len(v) >= 8:
                    vals[f".env value of {k.strip()}"] = v
    tok = Path("~/.kaggle/access_token").expanduser()
    if tok.exists() and len(tok.read_text().strip()) >= 8:
        vals["~/.kaggle/access_token"] = tok.read_text().strip()
    return vals


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=ROOT, capture_output=True, check=True).stdout
    return [f for f in out.decode().split("\0") if f]


def main() -> int:
    files = tracked_files()
    if ".env" in files:
        print("FAIL: .env is tracked by git (git rm --cached .env)")
        return 1
    values = secret_values()
    hits = 0
    for name in files:
        p = ROOT / name
        if p.is_symlink() or not p.is_file():
            continue
        data = p.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary media
        text = data.decode("utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for rule, rx in PATTERNS.items():
                if rx.search(line):
                    print(f"FAIL: {name}:{lineno}: {rule}")
                    hits += 1
            for rule, val in values.items():
                if val in line:
                    print(f"FAIL: {name}:{lineno}: contains the {rule}")
                    hits += 1
    print(f"check-secrets: {len(files)} tracked files, {len(values)} secret values checked, {hits} problem(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
