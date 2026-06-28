#!/usr/bin/env python3
"""ekp-forge package add — wraps uv add with deterministic knowledge harvesting.

Usage:
    ekp-forge package add flask
    ekp-forge package add fastapi uvicorn --uv-args "--extra dev"

Design:
- Always runs ``uv add <pkg>`` first (synchronous, captures exit code).
- On success, calls ``KnowledgeHarvester`` for each package.
- Saves compressed docs to ``.ai-knowledge/libs/<pkg>.md``.
- Outputs structured JSON summary for MCP/API integration.

This is the official hook for Phase 3 knowledge harvesting. It intentionally
does NOT intercept ``uv add`` via shell aliases or filesystem watchers —
those patterns introduce environment-dependent failure points.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Ensure the package is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ekp_forge.knowledge.harvester import KnowledgeHarvester  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add Python packages with automatic knowledge harvesting",
    )
    parser.add_argument("packages", nargs="+", help="Package names to add")
    parser.add_argument(
        "--uv-args",
        default="",
        help="Additional arguments forwarded to uv add (e.g. '--extra dev')",
    )
    args = parser.parse_args()

    # Step 1: Run uv add
    uv_cmd = ["uv", "add", *args.packages]
    if args.uv_args:
        uv_cmd.extend(args.uv_args.split())

    result = subprocess.run(uv_cmd, capture_output=False)
    if result.returncode != 0:
        sys.exit(result.returncode)

    # Step 2: Harvest knowledge for each package
    harvester = KnowledgeHarvester(project_root=Path.cwd())
    harvest_results: list[dict[str, str | None]] = []

    for pkg in args.packages:
        info = harvester.harvest(pkg)
        if info:
            path = harvester.save(info)
            harvest_results.append(
                {
                    "package": pkg,
                    "status": "harvested",
                    "path": str(path),
                }
            )
        else:
            harvest_results.append(
                {
                    "package": pkg,
                    "status": "no_docs",
                    "path": None,
                }
            )

    # Output JSON summary (useful for MCP/API consumers)
    output = {"uv_exit_code": 0, "results": harvest_results}
    print(json.dumps(output, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
