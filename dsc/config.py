import os
from pathlib import Path

# EKP_KNOWLEDGE_CACHE environment variable overrides default ~/.knowledge-cache path.
# This simplifies unit testing/E2E environments without monkeypatching internal module variables.
_env_path = os.environ.get("EKP_KNOWLEDGE_CACHE")
if _env_path:
    KNOWLEDGE_CACHE = Path(_env_path).expanduser().resolve()
else:
    KNOWLEDGE_CACHE = Path.home() / ".knowledge-cache"
