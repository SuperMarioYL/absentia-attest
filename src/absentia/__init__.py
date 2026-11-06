"""absentia — 有界缺席凭证（coverage-bounded absence attestation）.

The package is organised around one artefact: a recomputable JSON file that
says *what was read, how it was searched, and how far below the threshold every
comparison landed*. Nothing here asks a model whether it "saw" anything.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:  # pragma: no cover - only missing when running from a source tree
    __version__ = version("absentia-attest")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.1.0"

DISTRIBUTION = "absentia-attest"

__all__ = ["__version__", "DISTRIBUTION"]
