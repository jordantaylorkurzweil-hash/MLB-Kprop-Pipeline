"""
_paths.py — single source of truth for where the pipeline reads/writes files.

Replaces the old hardcoded `/home/user/workspace` (a path that only existed
inside Perplexity's sandboxed compute agent). Every other script imports
WORKSPACE from here instead of hardcoding a path.

Override with the KPROP_WORKSPACE env var if you ever want output to go
somewhere else (e.g. a mounted volume). Defaults to <repo_root>/workspace,
which is where GitHub Actions checks the repo out to.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.environ.get("KPROP_WORKSPACE", str(REPO_ROOT / "workspace")))
WORKSPACE.mkdir(parents=True, exist_ok=True)
