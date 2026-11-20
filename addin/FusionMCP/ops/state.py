"""Add-in state that must survive `reload` (never reloaded)."""

namespace: dict = {}          # persistent execute_python namespace
scratch_docs: list = []       # documents the add-in opened itself (only these may be closed)
last_crash: dict | None = None
started_at: str = ""
