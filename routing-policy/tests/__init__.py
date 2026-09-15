"""Keep routing-policy tests importable without depending on repository tests.

The package boundary makes the loader's focused test suite runnable from its
service directory while parity checks may still import the legacy application.
"""

from __future__ import annotations
