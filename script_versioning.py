"""Version tracking for PowerShell scripts in Capacities.

When a script changes, a new version object is created and linked to
the previous version. The script object itself always reflects the
latest version, while version objects preserve history.
"""

from __future__ import annotations

import hashlib
import logging
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
    entity_property,
)

logger = logging.getLogger(__name__)

STRUCTURE_SCRIPT_VERSION = "PowerShell Script Version"


class ScriptVersioning:
    """Tracks versions of PowerShell scripts in Capacities."""

    def __init__(self, client: CapacitiesClient, manager: NotebookManager):
        self.client = client
        self.manager = manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def register_script(
        self,
        file_path: str,
        script_text: str,
        version: str = "1.0.0",
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register or update a PowerShell script.

        If a script object with the same file path + content hash already
        exists and the content is unchanged, it is returned unchanged.
        Otherwise a new version is created and the script object is
        updated to point to the latest version.
        """
        content_hash = hashlib.sha256(script_text.encode("utf-8")).hexdigest()[:16]
        script_title = title or f"PS Script: {file_path}"
        version_title = f"{script_title} v{version} [{content_hash}]"

        structures = self.manager.ensure_structures()
        script_struct = structures.get("script")
        version_struct = structures.get("version")

        if script_struct is None:
            raise CapacitiesError(
                "PowerShell Script structure is not available. "
                "Create it in the Capacities app."
            )

        # Idempotency: check if this exact content+version already exists.
        existing = self.manager.find_object_by_title(
            script_struct["id"], script_title
        )
        if existing is not None:
            current = self.client.get_object(existing["id"])
            current_hash = self._get_property_value(
                current, "contentHash"
            )
            if current_hash == content_hash:
                logger.info("Script already registered: %s", existing["id"])
                return existing

        # Parse functions from the script.
        from .ps1_parser import parse_file

        parse_result = parse_file(file_path)
        functions = [
            {
                "name": f.name,
                "line_start": f.line_start,
                "line_end": f.line_end,
                "body": f.body,
                "parameters": [
                    {"name": p.name, "type": p.type, "default": p.default}
                    for p in f.parameters
                ],
            }
            for f in parse_result.functions
        ]

        # Create or update the script object.
        from .templates import powershell_script_object

        template = powershell_script_object(
            title=script_title,
            file_path=file_path,
            version=version,
            script_text=script_text,
            functions=functions,
        )

        if existing is not None:
            script_obj = self.manager.update_object_from_template(
                friendly_structure="script",
                object_id=existing["id"],
                title=script_title,
                properties={
                    **template["properties"],
                    "contentHash": text_property(content_hash),
                    "version": text_property(version),
                },
                blocks=template["blocks"],
            )
        else:
            script_obj = self.manager.create_object_from_template(
                friendly_structure="script",
                title=script_title,
                properties={
                    **template["properties"],
                    "contentHash": text_property(content_hash),
                    "version": text_property(version),
                },
                blocks=template["blocks"],
            )
            # Ensure contentHash property exists on the structure.
            self._ensure_property(script_struct["id"], "contentHash", "text")

        script_id = script_obj["id"]

        # Create a version object.
        version_obj = self._create_version_object(
            script_id=script_id,
            version=version,
            content_hash=content_hash,
            script_text=script_text,
            functions=functions,
        )

        # Link the version to the script object (if the script structure
        # has a 'versions' object-select property).
        self._link_version(script_id, version_obj["id"])

        logger.info(
            "Registered script %s (version %s, id %s)", script_title, version, script_id
        )
        return script_obj

    def update_script(
        self,
        script_id: str,
        file_path: str,
        script_text: str,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing script with new content.

        A new version object is created for the change; the script object
        is updated to the latest content.
        """
        if version is None:
            version = self._next_version(script_id)

        return self.register_script(
            file_path=file_path,
            script_text=script_text,
            version=version,
            title=None,  # Let it keep the existing title by updating.
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _create_version_object(
        self,
        script_id: str,
        version: str,
        content_hash: str,
        script_text: str,
        functions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        structures = self.manager.ensure_structures()
        version_struct = structures.get("version")
        if version_struct is None:
            # Fall back to creating a generic object with version info.
            title = f"PS Script Version {version} [{content_hash}]"
            return self.client.create_object(
                structure_id="RootPage",
                properties={
                    "title": {"type": "title", "title": {"value": title}},
                    "description": {
                        "type": "text",
                        "text": {
                            "value": f"Version {version} of script {script_id}. "
                            f"Hash: {content_hash}"
                        },
                    },
                },
                blocks={
                    "content": [
                        heading_block(f"Version {version}", level=1),
                        text_block(f"Content hash: {content_hash}"),
                        text_block(f"Script id: {script_id}"),
                        code_block("powershell", script_text),
                    ]
                },
            )

        # Use the custom version structure.
        from .templates import powershell_function_object  # noqa: F401

        title = f"PS Script Version {version} [{content_hash}]"
        blocks: List[Dict[str, Any]] = []
        blocks.append(heading_block(f"Version {version}", level=1))
        blocks.append(text_block(f"Content hash: {content_hash}"))
        blocks.append(text_block(f"Script id: {script_id}"))
        blocks.append(text_block(f"Created: {datetime.now(timezone.utc).isoformat()}"))
        blocks.append(code_block("powershell", script_text))

        for fn in functions:
            blocks.append(heading_block(f"Function: {fn['name']}", level=2))
            blocks.append(code_block("powershell", fn["body"]))

        properties: Dict[str, Any] = {
            "title": {"type": "title", "title": {"value": title}},
            "version": {"type": "text", "text": {"value": version}},
            "contentHash": {"type": "text", "text": {"value": content_hash}},
            "scriptId": {"type": "entity", "entity": [{"id": script_id}]},
        }
        return self.client.create_object(
            structure_id=version_struct["id"],
            properties=properties,
            blocks={"content": blocks},
        )

    def _link_version(self, script_id: str, version_id: str) -> None:
        """Link a version object to its script object via object-select."""
        script_obj = self.client.get_object(script_id)
        script_struct_id = script_obj.get("structureId")
        versions_prop_id = self.manager.property_id(script_struct_id, "versions")
        if versions_prop_id is None:
            # Try 'relatedVersions' or similar.
            versions_prop_id = self.manager.property_id(script_struct_id, "relatedVersions")
        if versions_prop_id is None:
            logger.debug(
                "Script structure %s has no versions property; version linking skipped.",
                script_struct_id,
            )
            return

        # Read current links and add the new one.
        current = script_obj.get("properties", {}).get(versions_prop_id, {}).get("entity", [])
        current_ids = {e["id"] for e in current}
        current_ids.add(version_id)
        properties: Dict[str, Any] = {
            versions_prop_id: {
                "type": "entity",
                "entity": [{"id": i} for i in sorted(current_ids)],
            }
        }
        self.client.update_object(object_id=script_id, properties=properties)

    def _ensure_property(
        self, structure_id: str, property_name: str, property_type: str
    ) -> None:
        """Attempt to add a property definition to a structure (best effort)."""
        # Property definitions are managed in the Capacities app; the API
        # may not expose structure mutation. We log and skip.
        logger.debug(
            "Property %s on structure %s must be defined in the Capacities app.",
            property_name,
            structure_id,
        )

    def _get_property_value(
        self, obj: Dict[str, Any], property_name: str
    ) -> Optional[str]:
        prop = obj.get("properties", {}).get(property_name)
        if prop is None:
            return None
        if prop.get("type") == "text":
            return prop.get("text", {}).get("value")
        if prop.get("type") == "title":
            return prop.get("title", {}).get("value")
        return None

    def _next_version(self, script_id: str) -> str:
        """Determine the next version number for a script."""
        try:
            obj = self.client.get_object(script_id)
        except CapacitiesError:
            return "1.0.0"
        current = self._get_property_value(obj, "version")
        if not current:
            return "1.0.0"
        # Bump the minor version.
        parts = current.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            parts[1] = str(int(parts[1]) + 1)
            return ".".join(parts)
        return "1.0.0"

    def list_versions(self, script_id: str) -> List[Dict[str, Any]]:
        """List version objects linked to a script."""
        script_obj = self.client.get_object(script_id)
        script_struct_id = script_obj.get("structureId")
        versions_prop_id = self.manager.property_id(script_struct_id, "versions")
        if versions_prop_id is None:
            versions_prop_id = self.manager.property_id(script_struct_id, "relatedVersions")
        if versions_prop_id is None:
            return []

        entity = script_obj.get("properties", {}).get(versions_prop_id, {}).get("entity", [])
        version_ids = [e["id"] for e in entity]
        versions: List[Dict[str, Any]] = []
        for vid in version_ids:
            try:
                versions.append(self.client.get_object(vid))
            except CapacitiesError:
                continue
        return versions
