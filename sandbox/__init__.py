"""Sandbox package for Safe Factory.

The package provides isolated workspace handling, file‑system constraints,
verification helpers, and utility agents. Submodules are deliberately **not**
imported here to avoid circular‑import issues during test collection. Tests
import the required submodules directly (e.g. ``import sandbox.workspace``).
"""

# The package intentionally does not import submodules at import time.
# Consumers should import the needed components explicitly.
