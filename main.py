"""Command-line interface for the Capacities automation tool."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import CapacitiesConfig
from .api_client import CapacitiesClient
from .notebook_manager import NotebookManager
from .ps1_parser import parse_file
from .script_versioning import ScriptVersioning
from .conversation_ingest import ConversationIngest
from .git_sync import GitSync


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def cmd_init_repository(args: argparse.Namespace) -> int:
    """Initialize a repository and sync it to Capacities."""
    config = CapacitiesConfig.load()
    client = CapacitiesClient(config)
    try:
        manager = NotebookManager(client)
        sync = GitSync(client, manager)
        result = sync.init_repository(args.repo_path)
        print(f"Processed {result['files_processed']} files.")
        print(f"Created {len(result['objects_created'])} objects.")
        if result["errors"]:
            error_count = len(result["errors"])
            print(f"Errors: {error_count}")
            for err in result["errors"]:
                print(f"  - {err['file']}: {err['error']}")
        return 0
    finally:
        client.close()


def cmd_sync_file(args: argparse.Namespace) -> int:
    """Sync a single file to Capacities."""
    config = CapacitiesConfig.load()
    client = CapacitiesClient(config)
    try:
        manager = NotebookManager(client)
        sync = GitSync(client, manager)
        obj = sync.sync_file(args.file_path)
        if obj is None:
            print(f"No object created for {args.file_path}")
            return 1
        print(f"Synced {args.file_path} -> object {obj.get('id')}")
        return 0
    finally:
        client.close()


def cmd_parse_ps1(args: argparse.Namespace) -> int:
    """Parse a PowerShell file and report functions."""
    result = parse_file(args.file_path)
    print(f"File: {result.file_path}")
    print(f"Syntax valid: {result.syntax_valid}")
    if result.syntax_errors:
        print(f"Syntax errors: {result.syntax_errors}")
    print(f"Functions found: {len(result.functions)}")
    for fn in result.functions:
        print(f"  - {fn.name} (lines {fn.line_start}-{fn.line_end})")
        for p in fn.parameters:
            print(f"    param: {p.name} ({p.type or 'any'})")
    return 0


def cmd_ingest_conversation(args: argparse.Namespace) -> int:
    """Ingest a conversation from a file."""
    config = CapacitiesConfig.load()
    client = CapacitiesClient(config)
    try:
        manager = NotebookManager(client)
        ingest = ConversationIngest(client, manager)
        obj = ingest.ingest_from_file(
            file_path=args.file_path,
            source=args.source,
            participants=args.participants,
        )
        if obj is None:
            print("No conversation object created.")
            return 1
        print(f"Ingested conversation -> object {obj.get('id')}")
        return 0
    finally:
        client.close()


def cmd_register_script(args: argparse.Namespace) -> int:
    """Register a PowerShell script with version tracking."""
    config = CapacitiesConfig.load()
    client = CapacitiesClient(config)
    try:
        manager = NotebookManager(client)
        versioning = ScriptVersioning(client, manager)
        obj = versioning.register_script(
            file_path=args.file_path,
            script_text=Path(args.file_path).read_text(encoding="utf-8"),
            version=args.version,
        )
        print(f"Registered script -> object {obj.get('id')}")
        return 0
    finally:
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capacities-tool",
        description="Automate Capacities notebook management for repositories.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-repo", help="Initialize a repository in Capacities.")
    p_init.add_argument("repo_path", help="Path to the git repository root.")
    p_init.set_defaults(func=cmd_init_repository)

    p_sync = sub.add_parser("sync-file", help="Sync a single file to Capacities.")
    p_sync.add_argument("file_path", help="Path to the file to sync.")
    p_sync.set_defaults(func=cmd_sync_file)

    p_parse = sub.add_parser("parse-ps1", help="Parse a PowerShell file.")
    p_parse.add_argument("file_path", help="Path to the .ps1 file.")
    p_parse.set_defaults(func=cmd_parse_ps1)

    p_ingest = sub.add_parser(
        "ingest-conversation", help="Ingest a conversation from a file."
    )
    p_ingest.add_argument("file_path", help="Path to the conversation file.")
    p_ingest.add_argument("--source", default="file", help="Conversation source.")
    p_ingest.add_argument(
        "--participants", default="unknown", help="Conversation participants."
    )
    p_ingest.set_defaults(func=cmd_ingest_conversation)

    p_register = sub.add_parser(
        "register-script", help="Register a PowerShell script with versioning."
    )
    p_register.add_argument("file_path", help="Path to the .ps1 file.")
    p_register.add_argument("--version", default="1.0.0", help="Script version.")
    p_register.set_defaults(func=cmd_register_script)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose if hasattr(args, "verbose") else False)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
