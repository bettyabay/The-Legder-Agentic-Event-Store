from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run narrative tests and write output artifact.")
    parser.add_argument("--out", default="artifacts/narrative_test_results.txt")
    args = parser.parse_args()

    cmd = [
        "uv",
        "run",
        "--with",
        "pytest",
        "--with",
        "pytest-asyncio",
        "python",
        "-m",
        "pytest",
        "tests/test_narratives.py",
        "-q",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = (proc.stdout or "") + (proc.stderr or "")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output, encoding="utf-8")
    print(f"OK: wrote {out_path} (exit={proc.returncode})")

    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
