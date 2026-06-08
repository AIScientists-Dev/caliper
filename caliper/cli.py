"""Caliper CLI. `caliper init` chooses the confined working directory."""
from __future__ import annotations

import argparse
import json
import os

from .config import CONFIG_PATH, get_workspace, load_config, save_config


def cmd_init(args):
    default = get_workspace() or os.path.join(os.getcwd(), "caliper_workspace")
    path = args.path
    if not path:
        try:
            path = input(f"Caliper working directory [{default}]: ").strip()
        except EOFError:
            path = ""
    path = os.path.abspath(os.path.expanduser(path or default))
    os.makedirs(path, exist_ok=True)
    cfg = load_config()
    cfg["workspace"] = path
    save_config(cfg)
    print(f"\nWorkspace set to: {path}")
    print(f"Saved to:         {CONFIG_PATH}")
    print("All Caliper writes, temp files, and outputs are confined to this directory.")
    print("Input data elsewhere is read-only — Caliper reads it in place, never modifies it.")


def cmd_config(args):
    cfg = load_config()
    print(json.dumps(cfg, indent=2) if cfg else "(no config yet — run `caliper init`)")
    print(f"active workspace: {get_workspace() or '(none → defaults to ./caliper_workspace)'}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="caliper",
                                description="Caliper — a calibrated AI research analyst.")
    sub = p.add_subparsers(dest="cmd")
    pi = sub.add_parser("init", help="choose the confined working directory")
    pi.add_argument("path", nargs="?", help="working directory (prompted if omitted)")
    pi.set_defaults(func=cmd_init)
    pc = sub.add_parser("config", help="show current configuration")
    pc.set_defaults(func=cmd_config)
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
