"""Defaults and user configuration (incl. the confined working directory)."""
from __future__ import annotations

import json
import os

# --- LLM providers ---------------------------------------------------------------
DEFAULT_PROVIDER = "anthropic"  # overall default
PROVIDER_DEFAULT_MODEL = {
    "anthropic": "claude-opus-4-8",  # overall default model — most capable
    "openai": "gpt-5",
}

# --- Trust-gate defaults (one-sided risk control) --------------------------------
DEFAULT_ALPHA = 0.10   # max tolerated rate of confident-but-wrong (auto-accepted)
DEFAULT_DELTA = 0.05   # confidence with which that bound holds

# --- Executor --------------------------------------------------------------------
DEFAULT_TIMEOUT_SEC = 600

# --- User config file (workspace, etc.) ------------------------------------------
_CONFIG_DIR = os.environ.get("CALIPER_CONFIG_DIR") or os.path.expanduser("~/.config/caliper")
CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def get_workspace():
    """Confined working directory: env var > config file > None (caller defaults)."""
    return os.environ.get("CALIPER_WORKSPACE") or load_config().get("workspace")
