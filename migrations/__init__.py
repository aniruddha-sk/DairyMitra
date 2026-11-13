"""
Versioned, re-runnable database migrations for DairyMitr.

Each migration module exposes VERSION (str), DESCRIPTION (str) and STEPS:
a list of Step objects. Every step carries a guard so it is skipped when
already applied. MySQL commits DDL implicitly, so per-step guards (not one
big transaction) are what make a partially-applied migration safe to re-run.

Run:  python -m migrations            (apply pending)
      python -m migrations --status   (show state, change nothing)
      python -m migrations --dry-run  (print steps that would run)
"""
