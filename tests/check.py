"""Run every local check: JS syntax, CSS syntax, then the test suite.

    python tests/check.py

Exits non-zero if anything fails. The test suite runs in a throwaway data
folder (see conftest.py), so this never touches your real settings or chats.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    failed = []

    if shutil.which("node"):
        js_files = sorted([*ROOT.glob("*.js"), *(ROOT / "web").rglob("*.js")])
        for js in js_files:
            r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
            if r.returncode:
                failed.append(f"JS   {js.relative_to(ROOT).as_posix()}: {r.stderr.strip()}")
        print(f"JS syntax:  {len(js_files)} files checked")

        checker = ROOT / "css-syntax-check.cjs"
        css_files = sorted([*ROOT.glob("*.css"), *(ROOT / "web").rglob("*.css")])
        for css in css_files:
            rel = css.relative_to(ROOT).as_posix()
            r = subprocess.run(["node", str(checker), rel], input=css.read_bytes(),
                               capture_output=True, cwd=ROOT)
            try:
                ok = json.loads(r.stdout or b"{}").get("check_status") == "passed"
            except ValueError:
                ok = False
            if not ok:
                failed.append(f"CSS  {rel}: {(r.stdout or r.stderr).decode(errors='replace').strip()}")
        print(f"CSS syntax: {len(css_files)} files checked")
    else:
        print("JS/CSS syntax: skipped (Node.js isn't installed)")

    print("Tests:")
    if subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
                      cwd=ROOT).returncode:
        failed.append("TESTS failed (see output above)")

    print()
    if failed:
        print("FAILED:")
        for line in failed:
            print("  " + line)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
