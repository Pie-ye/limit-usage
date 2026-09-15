"""Expose the routing policy loader as an isolated service package.

Keeping this package independent from the existing limit-usage application lets
the declarative policy evolve without coupling its loader to quota collection.
"""

from __future__ import annotations
