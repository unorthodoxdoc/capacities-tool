"""Session Ingestion Bot

Extracts conversations from Hermes session storage and ingests them into
Capacities using the existing ConversationIngest pipeline.

Usage:
    python session_ingest_bot.py --session-id <id> --title <title>
    python session_ingest_bot.py --export-file <path> --title <title>
    python session_ingest_bot.py --list-sessions

Environment variables:
    CAPACITIES_API_TOKEN    Capacities API token (required)
    CAPACITIES_BASE_URL     Capacities base URL (default: https://api.capacities.io)
    CAPACITIES_API_VERSION  Capacities API version header (default: 1.0.0)
    CAPACITIES_SPACE_ID     Capacities space ID (required)
    CAPACITIES_CONFIG_FILE  Optional INI config file path
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from capacities_tool.config import CapacitiesConfig
from capacities_tool.api_client import CapacitiesClient
from capacities_tool.notebook_manager import NotebookManager
from capacities_tool.conversation_ingest import ConversationIngest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hermes session export
# ---------------------------------------------------------------------------

def export_session_to_jsonl(
    session_id: str,
    output_path: Path,
    hermes_bin: Optional[str] = None,
) -> Path:
    """Export a Hermes session to JSONL format using the Hermes CLI.

    Args:
        session_id: The Hermes session ID to export.
        output_path: Path to write the exported JSONL file.
        hermes_bin: Optional path to the hermes executable.

    Returns:
        The path to the exported file.

    Raises:
        RuntimeError: If the export command fails.
    """
    if hermes_bin is None:
        # Default Hermes binary location
        hermes_bin = "C:/Users/unort/AppData/Local/hermes/hermes-agent/.hermes/bin/hermes.exe"

    cmd = [
        hermes_bin,
        "sessions",
        "export",
        "--session-id",
        session_id,
        "--format",
        "jsonl",
        str(output_path),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Hermes export failed (exit {result.returncode}): {error_msg}"
        )

    if not output_path.exists():
        raise RuntimeError(
            f"Export succeeded but output file not found: {output_path}"
        )

    logger.info("Exported session %s to %s", session_id, output_path)
    return output_path


def extract_messages_from_export(
    export_path: Path,
    include_tool_messages: bool = False,
    min_content_length: int = 0,
) -> List[Dict[str, Any]]:
    """Extract user and assistant messages from a Hermes session export.

    Args:
        export_path: Path to the JSONL export file.
        include_tool_messages: Whether to include tool role messages.
        min_content_length: Minimum content length for a message to be included.

    Returns:
        A list of message dicts with 'role' and 'content' keys.
    """
    messages: List[Dict[str, Any]] = []

    with open(export_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                session_obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # The session object contains a 'messages' list
            raw_messages = session_obj.get("messages", [])
            if not isinstance(raw_messages, list):
                continue

            for msg in raw_messages:
                role = msg.get("role", "")
                content = msg.get("content", "")

                # Skip messages with insufficient content
                if len(content) < min_content_length:
                    continue

                # Filter to relevant roles
                if role not in ("user", "assistant") and not include_tool_messages:
                    if role == "tool" and include_tool_messages:
                        pass
                    else:
                        continue

                messages.append({
                    "role": role,
                    "content": content,
                    "timestamp": msg.get("timestamp", ""),
                })

    logger.info("Extracted %d messages from %s", len(messages), export_path)
    return messages


def messages_to_conversation_dict(
    title: str,
    messages: List[Dict[str, Any]],
    source: str = "hermes-session",
    participants: str = "user,assistant",
) -> Dict[str, Any]:
    """Build a conversation dict for the Capacities ingestion API.

    Args:
        title: Title for the conversation object.
        messages: List of message dicts with 'role', 'content', 'timestamp'.
        source: Source identifier for the conversation.
        participants: Participants string.

    Returns:
        A dict suitable for ConversationIngest.ingest_from_dict().
    """
    # Find the earliest timestamp for the conversation
    timestamps = [
        m.get("timestamp", "")
        for m in messages
        if m.get("timestamp")
    ]
    conversation_timestamp = min(timestamps) if timestamps else ""

    return {
        "title": title,
        "participants": participants,
        "source": source,
        "timestamp": conversation_timestamp,
        "messages": [
            {
                "role": m["role"],
                "content": m["content"],
            }
            for m in messages
        ],
    }


# ---------------------------------------------------------------------------
# Capacities ingestion
# ---------------------------------------------------------------------------

def ingest_conversation(
    conversation: Dict[str, Any],
    config: CapacitiesConfig,
    space_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Ingest a conversation into Capacities.

    Args:
        conversation: Conversation dict with title, participants, messages.
        config: CapacitiesConfig instance.
        space_id: Optional space ID override.

    Returns:
        The created object dict, or None if deduplicated/skipped.
    """
    client = CapacitiesClient(config)
    try:
        manager = NotebookManager(client)
        ingest = ConversationIngest(client, manager)

        result = ingest.ingest_from_dict(
            conversation=conversation,
            source=conversation.get("source", "hermes-session"),
        )

        if result is None:
            logger.info("Conversation was deduplicated (already exists).")
            return None

        logger.info("Ingested conversation -> object %s", result.get("id"))
        return result
    finally:
        client.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_list_sessions(args: argparse.Namespace) -> int:
    """List recent Hermes sessions."""
    hermes_bin = args.hermes_bin or "C:/Users/unort/AppData/Local/hermes/hermes-agent/.hermes/bin/hermes.exe"
    result = subprocess.run(
        [hermes_bin, "sessions", "list"],
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode
    return 0


def cmd_ingest_session(args: argparse.Namespace) -> int:
    """Ingest a Hermes session into Capacities."""
    config = CapacitiesConfig.load()

    # Export the session
    with tempfile.TemporaryDirectory() as tmpdir:
        export_path = Path(tmpdir) / f"{args.session_id}.jsonl"

        try:
            export_session_to_jsonl(args.session_id, export_path)
        except RuntimeError as exc:
            logger.error("Failed to export session: %s", exc)
            return 1

        # Extract messages
        messages = extract_messages_from_export(
            export_path,
            include_tool_messages=args.include_tools,
            min_content_length=args.min_length,
        )

        if not messages:
            logger.error("No messages extracted from session %s", args.session_id)
            return 1

        # Build conversation dict
        conversation = messages_to_conversation_dict(
            title=args.title,
            messages=messages,
            source=args.source,
            participants=args.participants,
        )

        # Ingest into Capacities
        result = ingest_conversation(conversation, config)

        if result is None:
            print("Conversation already exists (deduplicated).")
            return 0

        print(f"Ingested conversation -> object {result.get('id')}")
        return 0


def cmd_ingest_file(args: argparse.Namespace) -> int:
    """Ingest a conversation from a pre-exported file."""
    config = CapacitiesConfig.load()

    messages = extract_messages_from_export(
        Path(args.file),
        include_tool_messages=args.include_tools,
        min_content_length=args.min_length,
    )

    if not messages:
        logger.error("No messages extracted from %s", args.file)
        return 1

    conversation = messages_to_conversation_dict(
        title=args.title,
        messages=messages,
        source=args.source,
        participants=args.participants,
    )

    result = ingest_conversation(conversation, config)

    if result is None:
        print("Conversation already exists (deduplicated).")
        return 0

    print(f"Ingested conversation -> object {result.get('id')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session-ingest-bot",
        description="Extract Hermes sessions and ingest into Capacities.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-sessions", help="List Hermes sessions")
    p_list.add_argument(
        "--hermes-bin",
        default="C:/Users/unort/AppData/Local/hermes/hermes-agent/.hermes/bin/hermes.exe",
        help="Path to the hermes executable",
    )
    p_list.set_defaults(func=cmd_list_sessions)

    p_ingest = sub.add_parser(
        "ingest-session",
        help="Export a Hermes session and ingest into Capacities",
    )
    p_ingest.add_argument("session_id", help="Hermes session ID to export")
    p_ingest.add_argument(
        "--title",
        required=True,
        help="Title for the Capacities conversation object",
    )
    p_ingest.add_argument(
        "--source",
        default="hermes-session",
        help="Conversation source identifier",
    )
    p_ingest.add_argument(
        "--participants",
        default="user,assistant",
        help="Conversation participants",
    )
    p_ingest.add_argument(
        "--include-tools",
        action="store_true",
        help="Include tool-role messages in the conversation",
    )
    p_ingest.add_argument(
        "--min-length",
        type=int,
        default=0,
        help="Minimum content length for a message",
    )
    p_ingest.add_argument(
        "--hermes-bin",
        default="C:/Users/unort/AppData/Local/hermes/hermes-agent/.hermes/bin/hermes.exe",
        help="Path to the hermes executable",
    )
    p_ingest.set_defaults(func=cmd_ingest_session)

    p_file = sub.add_parser(
        "ingest-file",
        help="Ingest a conversation from a pre-exported JSONL file",
    )
    p_file.add_argument("file", help="Path to the JSONL export file")
    p_file.add_argument(
        "--title",
        required=True,
        help="Title for the Capacities conversation object",
    )
    p_file.add_argument(
        "--source",
        default="hermes-session",
        help="Conversation source identifier",
    )
    p_file.add_argument(
        "--participants",
        default="user,assistant",
        help="Conversation participants",
    )
    p_file.add_argument(
        "--include-tools",
        action="store_true",
        help="Include tool-role messages in the conversation",
    )
    p_file.add_argument(
        "--min-length",
        type=int,
        default=0,
        help="Minimum content length for a message",
    )
    p_file.set_defaults(func=cmd_ingest_file)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
