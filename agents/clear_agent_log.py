#!/usr/bin/env python
"""
Utility script to clear the agent_log stored in Letta for a given scope.

Usage examples (from the agents/ directory):

    # Clear the project-scoped global log for a specific project
    LETTA_API_KEY=... uv run python clear_agent_log.py --project-id <project_uuid>

    # Clear the legacy shared global_agent_log block
    LETTA_API_KEY=... uv run python clear_agent_log.py --global
"""

import argparse
import json
import os

from letta_client import Letta


def find_block_by_label(client: Letta, label: str):
    """Return the first Letta block matching the given label, or None."""
    for block in client.blocks.list():
        if block.label == label:
            return block
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Clear agent_log in a Letta block. By default, operates on the "
            "project-scoped log when --project-id is provided, or the legacy "
            "global_agent_log when --global is set."
        )
    )
    parser.add_argument(
        "--project-id",
        help=(
            "Project ID whose project-scoped agent log should be cleared. "
            "Targets block label 'project:{project_id}:agent_log'."
        ),
    )
    parser.add_argument(
        "--global",
        dest="clear_global",
        action="store_true",
        help="Clear the legacy shared 'global_agent_log' block instead of a project log.",
    )
    args = parser.parse_args()

    api_key = os.getenv("LETTA_API_KEY")
    if not api_key:
        raise SystemExit("LETTA_API_KEY must be set in the environment.")

    client = Letta(api_key=api_key)

    if args.clear_global:
        label = "global_agent_log"
    elif args.project_id:
        label = f"project:{args.project_id}:agent_log"
    else:
        raise SystemExit(
            "You must provide either --project-id <uuid> or --global to select a log to clear."
        )

    block = find_block_by_label(client, label)
    if not block:
        raise SystemExit(f"No Letta block found with label '{label}'. Nothing to clear.")

    # Minimal structure: just an empty agent_log array.
    new_value = json.dumps({"agent_log": []}, indent=2)
    client.blocks.update(block.id, value=new_value)
    print(f"Cleared agent_log for block '{label}' (id={block.id}).")


if __name__ == "__main__":
    main()

