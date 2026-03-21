from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def _run_step(name: str, cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, f"[{name}] exit={proc.returncode}\n{out}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Week-5 artifacts in one command.")
    parser.add_argument("--out-dir", default="artifacts")
    parser.add_argument(
        "--application-id",
        default="APEX-0029",
        help="Application id for regulatory package generation.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip narrative test run and only generate DB-derived artifacts.",
    )
    parser.add_argument(
        "--skip-regulatory",
        action="store_true",
        help="Skip regulatory package generation.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    steps: list[tuple[str, list[str]]] = []
    if not args.skip_tests:
        steps.append(
            (
                "narratives",
                [
                    "uv",
                    "run",
                    "--with",
                    "pytest",
                    "--with",
                    "pytest-asyncio",
                    "python",
                    "scripts/write_narrative_test_results.py",
                    "--out",
                    str(out_dir / "narrative_test_results.txt"),
                ],
            )
        )
    steps.append(
        (
            "api_cost",
            [
                "uv",
                "run",
                "--with",
                "asyncpg",
                "python",
                "scripts/generate_api_cost_report.py",
                "--out",
                str(out_dir / "api_cost_report.txt"),
            ],
        )
    )
    if not args.skip_regulatory:
        steps.append(
            (
                "regulatory_package",
                [
                    "uv",
                    "run",
                    "--with",
                    "asyncpg",
                    "python",
                    "scripts/generate_regulatory_package_artifact.py",
                    "--application-id",
                    args.application_id,
                    "--out",
                    str(out_dir / f"regulatory_package_{args.application_id}.json"),
                ],
            )
        )

    log_lines: list[str] = []
    failed = False
    for name, cmd in steps:
        code, text = _run_step(name, cmd)
        log_lines.append(text)
        if code != 0:
            failed = True

    log_path = out_dir / "artifact_generation_log.txt"
    log_path.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"OK: wrote {log_path}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
