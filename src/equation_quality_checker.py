"""Equation-quality checks and report writers.

This is the public module named by the application specification.  The
implementation remains in :mod:`src.equation_quality` so existing imports and
older packaged builds continue to work.
"""

from .equation_quality import check_equations, warnings_from_report, write_reports

__all__ = ["check_equations", "warnings_from_report", "write_reports"]
