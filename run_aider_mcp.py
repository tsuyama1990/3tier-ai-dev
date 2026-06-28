#!/usr/bin/env python3
"""
Wrapper for aider-mcp that fixes logging to use stderr instead of stdout.

The aider-mcp package configures logging.StreamHandler(sys.stdout), which
corrupts the MCP stdio JSONRPC protocol (stdout is the MCP transport channel).
This wrapper patches logging.basicConfig BEFORE importing aider_mcp so that
all log output goes to stderr instead.
"""

import logging
import sys
from typing import Any


def _patch_logging() -> None:
    """Patch logging.basicConfig to redirect StreamHandler from stdout to stderr."""
    original_basicConfig = logging.basicConfig

    def patched_basicConfig(**kwargs: Any) -> None:
        handlers = kwargs.get("handlers")
        if handlers:
            new_handlers = []
            for h in handlers:
                if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout:
                    new_handlers.append(logging.StreamHandler(sys.stderr))
                else:
                    new_handlers.append(h)
            kwargs["handlers"] = new_handlers
        original_basicConfig(**kwargs)

    logging.basicConfig = patched_basicConfig  # type: ignore[method-assign]


if __name__ == "__main__":
    # 1. Patch logging BEFORE importing aider_mcp (module-level basicConfig call)
    _patch_logging()

    # 2. Now import and run aider_mcp
    from aider_mcp import main

    sys.exit(main())
