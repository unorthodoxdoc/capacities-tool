"""High-level notebook management for Capacities.

Discovers or ensures custom object types (structures) exist, provides
idempotent create/read/update/delete of objects, and applies templates
to objects. All object identifiers are Capacities UUIDs.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

from .api_client import CapacitiesClient, CapacitiesError

logger = logging.getLogger(__name__)

# Structure identifiers we manage. Custom structures are created in the
# Capacities app; we discover them by title. If they do not exist, we
# attempt to create them via the API (support varies by server version).
STRUCTURE_PS_SCRIPT = "PowerShell Script"
STRUCTURE_PS_FUNCTION = "PowerShell Function"
STRUCTURE_AI_CONVERSATION = "AI Conversation"
STRUCTURE_REPO_FILE = "Repository File"

# Property definition ids (discovered from structures at runtime).
# We do not hard-code UUIDs because they are per-space.


class NotebookManager:
    """Manages Capacities notebook objects, structures, and templates."""

    def __init__(self, client: CapacitiesClient):
        self.client = client
        self._structure_cache: Dict[str, Dict[str, Any]] = {}
        self._property_cache: Dict[str, Dict[str, Any]] = {}
        self._structures_override: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Structure discovery
    # ------------------------------------------------------------------
    def ensure_structures(self) -> Dict[str, Dict[str, Any]]:
        """Discover (and optionally create) the custom structures we need.

        Returns a mapping of friendly name -> structure dict.
        """
        structures = self.client.structures().get("structures", [])
        # Build a lookup by title.
        by_title: Dict[str, Dict[str, Any]] = {}
        for s in structures:
            by_title[s.get("title", "")] = s
            self._structure_cache[s["id"]] = s
            # Cache property definitions keyed by structure id.
            for prop in s.get("propertyDefinitions", []):
                self._property_cache[(s["id"], prop["id"])] = prop

        result: Dict[str, Dict[str, Any]] = {}
        for friendly, title in [
            ("script", STRUCTURE_PS_SCRIPT),
            ("function", STRUCTURE_PS_FUNCTION),
            ("conversation", STRUCTURE_AI_CONVERSATION),
            ("repo_file", STRUCTURE_REPO_FILE),
        ]:
            struct = by_title.get(title)
            if struct is None:
                logger.warning(
                    "Structure '%s' not found in space. Create it manually "
                    "in the Capacities app under Settings -> Object types.",
                    title,
                )
                continue
            result[friendly] = struct
        return result

    def register_structure(self, friendly_name: str, struct: Dict[str, Any]) -> None:
        """Register a structure under a friendly name for create/update methods."""
        self._structures_override[friendly_name] = struct

    def _create_structure(self, title: str) -> Optional[Dict[str, Any]]:
        """Attempt to create a custom structure via the API.

        The Capacities API may or may not expose structure creation.
        If it fails, the user must create the structure in the desktop app.
        """
        # Try to find an endpoint to create structures. The OpenAPI spec
        # and docs focus on reading structures; creation is typically an
        # app-level action. We attempt a POST to /space/structures and
        # degrade gracefully.
        try:
            body = {
                "title": title,
                "pluralName": f"{title}s",
                "propertyDefinitions": self._default_properties(title),
            }
            return self.client._request(
                "POST", "/space/structures", data=body
            )
        except CapacitiesError as exc:
            logger.debug("Could not create structure '%s': %s", title, exc)
            return None

    def _default_properties(self, title: str) -> List[Dict[str, Any]]:
        """Default property definitions for a custom structure."""
        base = [
            {"id": "title", "name": "Title", "type": "title", "writable": True},
            {"id": "description", "name": "Description", "type": "text", "writable": True},
        ]
        if "Script" in title:
            base += [
                {"id": "filePath", "name": "File Path", "type": "text", "writable": True},
                {"id": "version", "name": "Version", "type": "text", "writable": True},
                {"id": "language", "name": "Language", "type": "text", "writable": True},
            ]
        if "Function" in title:
            base += [
                {"id": "functionName", "name": "Function Name", "type": "text", "writable": True},
                {"id": "parameters", "name": "Parameters", "type": "text", "writable": True},
                {"id": "description", "name": "Description", "type": "text", "writable": True},
            ]
        if "Conversation" in title:
            base += [
                {"id": "source", "name": "Source", "type": "text", "writable": True},
                {"id": "timestamp", "name": "Timestamp", "type": "date", "writable": True},
                {"id": "participants", "name": "Participants", "type": "text", "writable": True},
            ]
        return base

    def structure_id(self, friendly_name: str) -> Optional[str]:
        """Return the structure id for a friendly name, or None."""
        struct = self._structure_cache.get(
            next((s for s in self._structure_cache.values() if s.get("title") == friendly_name), None)
        )
        # Better: search by the mapping we stored. We stored by id, so do a reverse lookup.
        for s in self._structure_cache.values():
            if s.get("title") in (
                STRUCTURE_PS_SCRIPT,
                STRUCTURE_PS_FUNCTION,
                STRUCTURE_AI_CONVERSATION,
                STRUCTURE_REPO_FILE,
            ):
                pass
        # Simple linear search for the title.
        for s in self._structure_cache.values():
            if s.get("title") == friendly_name:
                return s["id"]
        return None

    # ------------------------------------------------------------------
    # Object lookup (idempotency)
    # ------------------------------------------------------------------
    def find_object_by_title(
        self, structure_id: str, title: str
    ) -> Optional[Dict[str, Any]]:
        """Find an existing object of a structure by exact title."""
        try:
            results = self.client.search_objects(query=f"structure:{structure_id} {title}")
        except CapacitiesError:
            results = {}
        for item in results.get("results", []):
            if item.get("structureId") == structure_id and item.get("title") == title:
                return item
        # Fall back to listing all objects of the structure.
        cursor = None
        while True:
            page = self.client.list_objects_by_structure(
                structure_id, page_size=100, cursor=cursor
            )
            for item in page.get("results", []):
                if item.get("title") == title:
                    return item
            cursor = page.get("nextCursor")
            if not cursor or not page.get("hasMore"):
                break
        return None

    def get_object(self, object_id: str) -> Optional[Dict[str, Any]]:
        """Read an object, returning None if it does not exist."""
        try:
            return self.client.get_object(object_id)
        except CapacitiesError as exc:
            if exc.status_code == 404:
                return None
            raise

    # ------------------------------------------------------------------
    # Object creation with templates
    # ------------------------------------------------------------------
    def create_object_from_template(
        self,
        friendly_structure: str,
        title: str,
        properties: Optional[Dict[str, Any]] = None,
        blocks: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        collections: Optional[List[str]] = None,
        structures: Optional[Dict[str, Dict[str, Any]]] = None,
        markdown: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create an object using a discovered structure as a template.

        ``friendly_structure`` is one of 'script', 'function',
        'conversation', 'repo_file'. Properties are typed value objects
        as described in the Capacities API docs.

        If the structure cannot be created via the generic POST /object
        endpoint (e.g. RootAIChat), and a ``markdown`` string is supplied,
        the method falls back to POST /object/markdown which supports
        built-in structure types such as RootAIChat.
        """
        if structures is None:
            structures = self.ensure_structures()
        # Merge in any registered overrides (e.g. RootAIChat fallback).
        merged = dict(structures)
        merged.update(self._structures_override)
        struct = merged.get(friendly_structure)
        if struct is None:
            raise CapacitiesError(
                f"Structure '{friendly_structure}' is not available. "
                "Create it in the Capacities app first."
            )

        # Filter properties to only those defined on the structure.
        prop_defs = {
            prop["id"]: prop for prop in struct.get("propertyDefinitions", [])
        }
        body_properties: Dict[str, Any] = {}
        if properties is not None:
            for prop_id, prop_value in properties.items():
                if prop_id in prop_defs:
                    body_properties[prop_id] = prop_value
        # Always set the title.
        body_properties["title"] = {"type": "title", "title": {"value": title}}

        # Idempotency: if an object with this title already exists, update it.
        existing = self.find_object_by_title(struct["id"], title)
        if existing is not None:
            return self.update_object_from_template(
                friendly_structure,
                existing["id"],
                title,
                properties,
                blocks,
                collections,
                structures=merged,
            )

        try:
            return self.client.create_object(
                structure_id=struct["id"],
                properties=body_properties,
                collections=collections,
                blocks=blocks,
            )
        except CapacitiesError as exc:
            if "cannot be created via this endpoint" in str(exc) and markdown is not None:
                logger.warning(
                    "Structure %s cannot be created via POST /object; "
                    "falling back to POST /object/markdown",
                    struct["id"],
                )
                return self.client.create_from_markdown(
                    structure_id=struct["id"],
                    markdown=markdown,
                    collections=collections,
                )
            raise

    def update_object_from_template(
        self,
        friendly_structure: str,
        object_id: str,
        title: str,
        properties: Optional[Dict[str, Any]] = None,
        blocks: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        collections: Optional[List[str]] = None,
        structures: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Update an existing object, preserving its id."""
        if structures is None:
            structures = self.ensure_structures()
        merged = dict(structures)
        merged.update(self._structures_override)
        struct = merged.get(friendly_structure)
        if struct is None:
            raise CapacitiesError(
                f"Structure '{friendly_structure}' is not available."
            )

        # Filter properties to only those defined on the structure.
        prop_defs = {
            prop["id"]: prop for prop in struct.get("propertyDefinitions", [])
        }
        body_properties: Dict[str, Any] = {}
        if properties is not None:
            for prop_id, prop_value in properties.items():
                if prop_id in prop_defs:
                    body_properties[prop_id] = prop_value
        body_properties["title"] = {"type": "title", "title": {"value": title}}

        return self.client.update_object(
            object_id=object_id,
            properties=body_properties,
            collections=collections,
            blocks=blocks,
        )

    def delete_object(self, object_id: str, hard_delete: bool = False) -> Dict[str, Any]:
        """Delete an object."""
        return self.client.delete_object(object_id, hard_delete=hard_delete)

    # ------------------------------------------------------------------
    # Block helpers
    # ------------------------------------------------------------------
    def code_block(self, lang: str, text: str) -> Dict[str, Any]:
        """Build a CodeBlock."""
        return {
            "type": "CodeBlock",
            "lang": lang,
            "text": text,
        }

    def text_block(self, text: str, style: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build a TextBlock with a single TextToken."""
        return {
            "type": "TextBlock",
            "tokens": [{"type": "TextToken", "text": text, "style": style or {}}],
            "blocks": [],
            "hierarchy": {"key": "Base", "val": 0},
        }

    def append_blocks(
        self,
        object_id: str,
        property_id: str,
        blocks: List[Dict[str, Any]],
        position: Optional[Dict[str, Any]] = None,
        parent_block_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append blocks to an object's block property."""
        return self.client.append_blocks(
            object_id=object_id,
            property_id=property_id,
            blocks=blocks,
            position=position,
            parent_block_id=parent_block_id,
        )

    def property_id(self, structure_id: str, property_name: str) -> Optional[str]:
        """Return the property definition id for a property name on a structure."""
        for s in self._structure_cache.values():
            if s["id"] == structure_id:
                for prop in s.get("propertyDefinitions", []):
                    if prop["name"].lower() == property_name.lower():
                        return prop["id"]
        return None

    def root_page_id(self) -> Optional[str]:
        """Discover the built-in RootPage structure id.

        RootPage objects can be created via POST /object/markdown, unlike
        RootAIChat which is server-side restricted. This method searches
        the cached structures for a built-in structure whose id is
        ``RootPage``.
        """
        for s in self._structure_cache.values():
            if s.get("id") == "RootPage":
                return s["id"]
        # Also search by title in case the id differs.
        for s in self._structure_cache.values():
            if s.get("title") == "RootPage":
                return s["id"]
        return None

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------
    def find_or_create_tag(self, tag_name: str) -> Optional[str]:
        """Find a tag object by title, creating it if missing."""
        # Tags are objects of structure RootTag.
        tag_struct = None
        for s in self._structure_cache.values():
            if s.get("title") == "Tag" or s.get("id") == "RootTag":
                tag_struct = s
                break
        if tag_struct is None:
            # Search all structures for the Tag type.
            all_structs = self.client.structures().get("structures", [])
            for s in all_structs:
                if s.get("title") == "Tag" or s.get("id") == "RootTag":
                    tag_struct = s
                    break
        if tag_struct is None:
            logger.warning("Tag structure (RootTag) not found in space.")
            return None

        existing = self.find_object_by_title(tag_struct["id"], tag_name)
        if existing is not None:
            return existing["id"]

        # Create the tag.
        return self.client.create_object(
            structure_id=tag_struct["id"],
            properties={"title": {"type": "title", "title": {"value": tag_name}}},
        ).get("id")

    def tag_property_id(self, structure_id: str) -> Optional[str]:
        """Return the 'tags' property id for a structure, if it has one."""
        for s in self._structure_cache.values():
            if s["id"] == structure_id:
                for prop in s.get("propertyDefinitions", []):
                    if prop["id"] == "tags" or prop["name"].lower() == "tags":
                        return prop["id"]
        return None

    def add_tags_to_object(
        self, object_id: str, structure_id: str, tag_names: List[str]
    ) -> None:
        """Add tag objects to an object's tags property."""
        tag_prop_id = self.tag_property_id(structure_id)
        if tag_prop_id is None:
            return
        tag_ids: List[str] = []
        for name in tag_names:
            tag_id = self.find_or_create_tag(name)
            if tag_id:
                tag_ids.append(tag_id)
        if not tag_ids:
            return
        properties: Dict[str, Any] = {
            tag_prop_id: {"type": "entity", "entity": [{"id": tid} for tid in tag_ids]}
        }
        # Merge with existing tags: read current object first.
        current = self.client.get_object(object_id)
        existing_tags = current.get("properties", {}).get(tag_prop_id, {}).get("entity", [])
        existing_ids = {e["id"] for e in existing_tags}
        all_ids = list(existing_ids | set(tag_ids))
        properties[tag_prop_id] = {
            "type": "entity",
            "entity": [{"id": tid} for tid in all_ids],
        }
        self.client.update_object(object_id=object_id, properties=properties)

    # ------------------------------------------------------------------
    # Versioning helpers
    # ------------------------------------------------------------------
    def object_id_for_hash(
        self, structure_id: str, title_prefix: str, hash_value: str
    ) -> Optional[str]:
        """Find an object by title containing a content hash (for idempotency)."""
        title = f"{title_prefix} [{hash_value}]"
        return self.find_object_by_title(structure_id, title)
