"""Code Studio Agent Engine — standalone service (no Django imports).

Implements the versioned engine contract from PROJECT_DESIGN.md §9 so the IDE
can swap in another engine (e.g. Orkestra, once audited) behind the same API.
"""
