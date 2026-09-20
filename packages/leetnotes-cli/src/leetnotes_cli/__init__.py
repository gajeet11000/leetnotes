"""
CLI command implementations for the LeetCode notes generator, split by area:
problems (data fetch + pending cache, db, render, recent), notes (the
everyday fetch -> render pipeline, plus standalone AI prefill generation).
Importing this package registers every subcommand onto the root `cli` group
defined in `root.py`.
"""

from . import (  # noqa: F401  (side effect: registers commands onto `cli`)
    notes,
    problems,
    problems_data,
    problems_db,
    problems_pin,
    problems_recent,
    problems_render,
)
from .root import cli

__all__ = ["cli"]
