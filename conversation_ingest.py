"""Conversation ingestion: put conversations into AI Conversation items.

Ingests conversation data (from files, memory, or other sources) into
Capacities AI Chat objects, with deduplication based on content hash.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .api_client import CapacitiesClient, CapacitiesError
from .notebook_manager import NotebookManager
from .templates import (
    code_block,
    heading_block,
    text_block,
    title_property,
    text_property,
    ai_conversation_object,
)

logger = logging.getLogger(__name__)


class ConversationIngest:
    """Ingests conversations into Capacities AI Conversation items."""

    def __init__(self, client: CapacitiesClient, manager: NotebookManager):
        self.client = client
        self.manager = manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def ingest_conversation(
        self,
        title: str,
        source: str,
        participants: str,
        conversation_text: str,
        timestamp: Optional[str] = None,
        dedupe: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Create or update an AI Conversation item.

        If ``dedupe`` is true and an identical conversation (by content
        hash) already exists, the existing object is updated in place
        with the new content.
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()

        content_hash = hashlib.sha256(
            f"{title}|{source}|{conversation_text}".encode("utf-8")
        ).hexdigest()[:16]

        # Deduplication check.
        if dedupe:
            existing = self._find_existing_conversation(content_hash, title)
            if existing is not None:
                logger.info(
                    "Duplicate conversation found: %s. Updating in place.",
                    existing["id"],
                )
                # Update the existing object with new content.
                return self.manager.update_object_from_template(
                    friendly_structure="conversation",
                    object_id=existing["id"],
                    title=display_title,
                    properties=template["properties"],
                    blocks=template["blocks"],
                    markdown=markdown,
                )

        structures = self.manager.ensure_structures()
        conv_struct = structures.get("conversation")
        if conv_struct is None:
            logger.warning(
                "AI Conversation structure is not available. Create it in the "
                "Capacities app under Settings -> Object types."
            )
            # Fall back to RootAIChat (built-in AI Chat structure).
            for s in self.manager._structure_cache.values():
                if s.get("id") == "RootAIChat":
                    conv_struct = s
                    break
            if conv_struct is None:
                raise CapacitiesError(
                    "No AI Conversation structure available. "
                    "Create one in the Capacities app."
                )
            # Register RootAIChat under "conversation" key so create/update
            # methods can find it without attempting custom structure creation.
            self.manager.register_structure("conversation", conv_struct)

        # Include content hash in title for idempotent dedup.
        display_title = f"{title} [{content_hash}]"

        template = ai_conversation_object(
            title=display_title,
            source=source,
            timestamp=timestamp,
            participants=participants,
            conversation_text=conversation_text,
        )
        template["properties"]["contentHash"] = {
            "type": "text",
            "text": {"value": content_hash},
        }

        # Generate Markdown for the /object/markdown fallback path.
        markdown = self._conversation_to_markdown(display_title, conversation_text)

        try:
            return self.manager.create_object_from_template(
                friendly_structure="conversation",
                title=display_title,
                properties=template["properties"],
                blocks=template["blocks"],
                markdown=markdown,
            )
        except CapacitiesError as exc:
            logger.warning(
                "Failed to create conversation via RootAIChat structure: %s",
                exc,
            )
            # Fall back to RootPage (built-in, creatable via /object/markdown).
            return self._ingest_via_rootpage(
                display_title, source, timestamp, participants, markdown, conversation_text
            )

    def _ingest_via_rootpage(
        self,
        title: str,
        source: str,
        timestamp: str,
        participants: str,
        markdown: str,
        conversation_text: str,
    ) -> Optional[Dict[str, Any]]:
        """Fallback: create a RootPage object to store the conversation.

        RootPage is a built-in structure creatable via POST /object/markdown.
        The conversation content is stored as markdown blocks under the
        RootPage 'content' property.
        """
        root_page_id = self.manager.root_page_id()
        if root_page_id is None:
            logger.error("RootPage structure not found in space.")
            return None

        # Idempotency: check if a RootPage with this title already exists.
        existing = self.manager.find_object_by_title(root_page_id, title)
        if existing is not None:
            logger.info("Duplicate RootPage conversation found: %s", existing["id"])
            return existing

        try:
            return self.client.create_from_markdown(
                structure_id=root_page_id,
                markdown=markdown,
            )
        except CapacitiesError as exc:
            logger.error(
                "Failed to create RootPage conversation object: %s", exc
            )
            return None

    def ingest_from_file(
        self,
        file_path: str,
        source: str = "file",
        participants: str = "unknown",
        dedupe: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Ingest a conversation from a text file.

        The file is expected to contain the conversation text. The title
        is derived from the file name.
        """
        from pathlib import Path

        path = Path(file_path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Cannot read conversation file %s: %s", file_path, exc)
            return None

        title = f"Conversation: {path.stem}"
        return self.ingest_conversation(
            title=title,
            source=source,
            participants=participants,
            conversation_text=text,
            dedupe=dedupe,
        )

    def ingest_from_dict(
        self,
        conversation: Dict[str, Any],
        source: str = "api",
        dedupe: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Ingest a conversation from a structured dict.

        Expected keys: ``title``, ``participants``, ``messages`` (list of
        ``{role, content}``), optional ``timestamp``.
        """
        title = conversation.get("title", "Untitled Conversation")
        participants = conversation.get("participants", "unknown")
        timestamp = conversation.get(
            "timestamp", datetime.now(timezone.utc).isoformat()
        )

        messages = conversation.get("messages", [])
        lines: List[str] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(f"[{role}] {content}")
        conversation_text = "\n".join(lines)

        return self.ingest_conversation(
            title=title,
            source=source,
            participants=participants,
            conversation_text=conversation_text,
            timestamp=timestamp,
            dedupe=dedupe,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _find_existing_conversation(
        self, content_hash: str, title: str
    ) -> Optional[Dict[str, Any]]:
        """Find an existing conversation by content hash.

        Searches the RootAIChat structure first, then falls back to
        RootPage (used when RootAIChat objects cannot be created via API).
        """
        structures = self.manager.ensure_structures()
        conv_struct = structures.get("conversation")
        if conv_struct is None:
            for s in self.manager._structure_cache.values():
                if s.get("id") == "RootAIChat":
                    conv_struct = s
                    break
        if conv_struct is not None:
            result = self.manager.find_object_by_title(
                conv_struct["id"], f"{title} [{content_hash}]"
            )
            if result is not None:
                return result

        # Fall back to RootPage for deduplication when RootAIChat is not
        # available or no match found.
        root_page_id = self.manager.root_page_id()
        if root_page_id is not None:
            return self.manager.find_object_by_title(
                root_page_id, f"{title} [{content_hash}]"
            )
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _conversation_to_markdown(title: str, conversation_text: str) -> str:
        """Convert conversation text to Markdown format.

        The input ``conversation_text`` is expected to use the
        ``[role] content`` line format produced by
        :meth:`ingest_from_dict`.  Each role line becomes a Markdown
        heading, which lets the Capacities server extract the title and
        store the conversation body when creating an object via
        POST /object/markdown.
        """
        lines: List[str] = [f"# {title}"]
        for line in conversation_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            match = re.match(r"\[(\w+)\]\s(.*)", line)
            if match:
                role = match.group(1).capitalize()
                content = match.group(2)
                lines.append(f"\n## {role}\n{content}")
            else:
                lines.append(line)
        return "\n".join(lines)
