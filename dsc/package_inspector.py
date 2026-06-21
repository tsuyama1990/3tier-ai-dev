#!/usr/bin/env python3
"""
DSC Stage 1: Package Inspector

Scans a project's .venv to identify installed packages with their exact
versions and source origins. Outputs a structured JSON manifest consumed
by downstream DSC stages (source_miner, smoke_tracer, asset_synthesizer).

Design requirements (architecture_design.md §3.1):
  1. Dynamically locate .venv/lib/python*/site-packages/ without
     hardcoding the Python minor version.
  2. Parse METADATA (PEP 566) to extract exact package name and version.
  3. Parse direct_url.json (PEP 610) to identify install source:
     pypi | github | vcs | local | url.
  4. Detect existing ~/.knowledge-cache/{name}/{version}/ entries and
     enumerate their assets for downstream delta computation.

Usage:
    python3 dsc/package_inspector.py --project /path/to/project --target ase mesa
    python3 dsc/package_inspector.py --project /path/to/project --all
    python3 dsc/package_inspector.py --project /path/to/project --target ase --output manifest.json
"""

import sys
import json
import argparse
import re
from email import message_from_string
from pathlib import Path
from typing import Optional


# ── Constants ─────────────────────────────────────────────────────────────────

from dsc.config import KNOWLEDGE_CACHE


# ── Site-packages discovery ────────────────────────────────────────────────────

def find_site_packages(venv: Path) -> Optional[Path]:
    """
    Locate site-packages under .venv, agnostic of Python minor version.

    Tries POSIX layout (lib/pythonX.Y/site-packages) first, then the
    Windows layout (Lib/site-packages). Returns the lexicographically
    latest match so that e.g. python3.13 is preferred over python3.11
    if both happen to exist.
    """
    matches = sorted(venv.glob("lib/python*/site-packages"))
    if matches:
        return matches[-1]
    matches = sorted(venv.glob("Lib/site-packages"))
    if matches:
        return matches[0]
    return None


# ── .dist-info discovery ───────────────────────────────────────────────────────

def find_dist_info(site_packages: Path, package_name: str) -> Optional[Path]:
    """
    Find the .dist-info directory for `package_name`.

    Package names are normalised per PEP 503: hyphens, underscores, and
    dots are equivalent.  We build a glob pattern that matches any of them.
    """
    # Build a glob-safe normalised pattern
    norm = re.sub(r"[-_.]", "[-_.]", re.escape(package_name))
    for d in site_packages.glob(f"{norm}-*.dist-info"):
        return d  # First match wins (versions differ in the glob suffix)

    # Slower case-insensitive fallback (handles mixed-case names)
    target = package_name.lower().replace("-", "_").replace(".", "_")
    for d in site_packages.iterdir():
        if d.is_dir() and d.suffix == ".dist-info":
            base = d.stem.rsplit("-", 1)[0]  # strip version
            if base.lower().replace("-", "_").replace(".", "_") == target:
                return d
    return None


# ── METADATA parsing (PEP 566) ────────────────────────────────────────────────

def parse_metadata(dist_info: Path) -> dict:
    """
    Parse the METADATA (or legacy PKG-INFO) file inside a .dist-info dir.

    Returns a dict with keys:
        name, version, home_page, github_url
    """
    for fname in ("METADATA", "PKG-INFO"):
        mf = dist_info / fname
        if mf.exists():
            break
    else:
        return {"name": None, "version": None, "home_page": None, "github_url": None}

    result: dict = {"name": None, "version": None, "home_page": None, "github_url": None}
    try:
        msg = message_from_string(mf.read_text(encoding="utf-8", errors="replace"))
        result["name"]      = msg.get("Name")
        result["version"]   = msg.get("Version")
        result["home_page"] = msg.get("Home-page") or None

        # Scan all Project-URL headers for a GitHub/GitLab/Gitea URL
        for key, val in msg.items():
            if key.lower() == "project-url":
                _, _, url = val.partition(",")
                url = url.strip()
                if any(h in url for h in ("github.com", "gitlab.com", "bitbucket.org")):
                    result["github_url"] = url
                    break

        # Fall back to Home-page
        if not result["github_url"] and result["home_page"]:
            if any(h in result["home_page"]
                   for h in ("github.com", "gitlab.com", "bitbucket.org")):
                result["github_url"] = result["home_page"]

    except Exception as exc:
        result["parse_error"] = str(exc)
    return result


# ── direct_url.json parsing (PEP 610) ─────────────────────────────────────────

def parse_direct_url(dist_info: Path) -> dict:
    """
    Parse direct_url.json to determine install source.

    Possible source_type values:
        'pypi'   — installed from PyPI index (no direct_url.json present)
        'github' — VCS install from a GitHub/GitLab URL
        'vcs'    — VCS install from another host
        'local'  — installed from a local directory (editable or not)
        'url'    — installed from an explicit HTTP URL (wheel/sdist)
        'unknown'— direct_url.json present but unparseable
    """
    duf = dist_info / "direct_url.json"
    if not duf.exists():
        return {"source_type": "pypi", "source_url": None}

    try:
        data = json.loads(duf.read_text(encoding="utf-8"))
        url = data.get("url", "")

        if "vcs_info" in data:
            vcs    = data["vcs_info"].get("vcs", "git")
            commit = data["vcs_info"].get("commit_id", "")
            stype  = "github" if any(h in url for h in ("github.com", "gitlab.com")) else "vcs"
            return {"source_type": stype, "source_url": url, "vcs": vcs, "commit": commit}

        if "dir_info" in data:
            local_path = url.removeprefix("file://")
            editable   = data["dir_info"].get("editable", False)
            return {"source_type": "local", "source_url": local_path, "editable": editable}

        return {"source_type": "url", "source_url": url}

    except Exception as exc:
        return {"source_type": "unknown", "parse_error": str(exc)}


# ── Single-package inspection ─────────────────────────────────────────────────

def inspect_package(site_packages: Path, package_name: str) -> dict:
    """
    Produce a full inspection record for one package.

    The record is self-contained: downstream stages (source_miner etc.)
    only need to consume this JSON — they do not re-scan the venv.
    """
    dist_info = find_dist_info(site_packages, package_name)
    if dist_info is None:
        return {
            "name":  package_name,
            "found": False,
            "error": f"No .dist-info directory found for '{package_name}' "
                     f"in {site_packages}",
        }

    meta       = parse_metadata(dist_info)
    direct_url = parse_direct_url(dist_info)

    name    = meta.get("name")    or package_name
    version = meta.get("version") or "unknown"

    cache_path = KNOWLEDGE_CACHE / name.lower() / version

    # Build PyPI canonical URL when source is PyPI
    source_url = direct_url.get("source_url")
    if direct_url.get("source_type") == "pypi":
        source_url = f"https://pypi.org/project/{name}/{version}/"

    record: dict = {
        "name":         name,
        "version":      version,
        "found":        True,
        "dist_info":    str(dist_info),
        "source_type":  direct_url.get("source_type", "pypi"),
        "source_url":   source_url,
        "github_url":   meta.get("github_url"),
        "home_page":    meta.get("home_page"),
        "cache_path":   str(cache_path),
        "cache_exists": cache_path.exists(),
        "cache_assets": [],
    }

    # Enumerate existing cache assets for delta computation
    if cache_path.exists():
        record["cache_assets"] = sorted(
            str(p.relative_to(cache_path))
            for p in cache_path.rglob("*")
            if p.is_file()
        )

    # Propagate optional VCS fields
    for opt_key in ("vcs", "commit", "editable"):
        if opt_key in direct_url:
            record[opt_key] = direct_url[opt_key]

    return record


# ── Batch inspection ──────────────────────────────────────────────────────────

def inspect_all_packages(site_packages: Path) -> list:
    """Inspect every installed package discovered via .dist-info directories."""
    records = []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        meta = parse_metadata(dist_info)
        name = meta.get("name")
        if name:
            records.append(inspect_package(site_packages, name))
    return records


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="package_inspector",
        description=(
            "DSC Stage 1 — scan a project's .venv and emit a JSON manifest "
            "of package name, version, source origin, and cache status."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Inspect specific packages
  python3 dsc/package_inspector.py --project ~/project/002_mlip_pipeline --target ase numpy

  # Inspect all installed packages
  python3 dsc/package_inspector.py --project ~/project/001_abm/test --all

  # Write manifest to file for downstream stages
  python3 dsc/package_inspector.py --project ~/project/002_mlip_pipeline \\
      --target ase --output /tmp/ase_manifest.json
        """,
    )
    p.add_argument(
        "--project",
        required=True,
        metavar="DIR",
        help="Absolute path to the target project (must contain .venv/)",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--target",
        nargs="+",
        metavar="PKG",
        help="One or more package names to inspect (e.g. ase mesa numpy)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Inspect all installed packages in the venv",
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        help="Write JSON manifest to FILE (default: stdout)",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact (non-pretty) JSON",
    )
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    venv    = project / ".venv"

    if not project.is_dir():
        print(f"ERROR: project path does not exist: {project}", file=sys.stderr)
        sys.exit(1)
    if not venv.exists():
        print(f"ERROR: no .venv found at {venv}", file=sys.stderr)
        sys.exit(1)

    site_packages = find_site_packages(venv)
    if site_packages is None:
        print(f"ERROR: cannot locate site-packages under {venv}", file=sys.stderr)
        sys.exit(1)

    manifest = {
        "project":         str(project),
        "venv":            str(venv),
        "site_packages":   str(site_packages),
        "python_version":  site_packages.parent.name,   # e.g. "python3.13"
        "knowledge_cache": str(KNOWLEDGE_CACHE),
        "packages":        [],
    }

    if args.all:
        manifest["packages"] = inspect_all_packages(site_packages)
    else:
        for pkg in args.target:
            manifest["packages"].append(inspect_package(site_packages, pkg))

    indent  = None if args.compact else 2
    payload = json.dumps(manifest, indent=indent, ensure_ascii=False)

    if args.output:
        out = Path(args.output)
        out.write_text(payload + "\n", encoding="utf-8")
        print(f"[Inspector] Manifest written → {out}", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    main()
