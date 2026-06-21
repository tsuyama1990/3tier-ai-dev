import json
from pathlib import Path
from typing import Dict, Any

def load_manifest(manifest_path: str | Path) -> Dict[str, Any]:
    """
    Safely load and parse EKP manifest JSON file.
    """
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Manifest file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Failed to parse manifest JSON at {path}: {exc}")
