"""
_paths.py — single source of truth for where the pipeline reads/writes files.

Flat repo layout: this file sits at the repo root alongside every other
script, so REPO_ROOT is just this file's own directory.

Override with the KPROP_WORKSPACE env var if you ever want output to go
somewhere else. Defaults to <repo_root>/workspace, which is where GitHub
Actions checks the repo out to.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("KPROP_WORKSPACE", str(REPO_ROOT / "workspace")))
WORKSPACE.mkdir(parents=True, exist_ok=True)

