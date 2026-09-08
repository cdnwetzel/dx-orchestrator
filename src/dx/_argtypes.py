"""Shared argparse typing alias.

``_SubParsersAction`` is private in the stdlib, but it is the only accurate type
for what ``ArgumentParser.add_subparsers()`` returns, and typeshed exposes it.
Naming it once here keeps every ``register_*_subcommand`` signature honest
without repeating the import or the generic parameter five times.
"""
from __future__ import annotations

import argparse
from typing import TypeAlias

SubParsers: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"

__all__ = ["SubParsers"]
