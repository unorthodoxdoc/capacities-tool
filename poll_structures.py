#!/usr/bin/env python3
"""
Poll Capacities API for available object types (structures) in a space,
and match them against the templates used by this tool.

Usage:
    CAPACITIES_API_TOKEN=<token> CAPACITIES_SPACE_ID=<uuid> \
    python poll_structures.py

Env vars (all optional except token + space):
    CAPACITIES_API_TOKEN    Capacities API bearer token
    CAPACITIES_BASE_URL     API base URL (default https://api.capacities.io)
    CAPACITIES_API_VERSION  API version header (default 1.0.0)
    CAPACITIES_SPACE_ID     Target space UUID
    CAPACITIES_CONFIG_FILE  Optional INI config file path
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

from capacities_tool.config import CapacitiesConfig
from capacities_tool.api_client import CapacitiesClient, CapacitiesError


def poll_structures(client: CapacitiesClient) -> List[Dict[str, Any]]:
    """Return all structures in the authenticated space, sorted by title."""
    result = client.structures()
    structures = result.get("structures", [])
    structures.sort(key=lambda s: s.get("title", ""))
    return structures


def print_structure(struct: Dict[str, Any]) -> None:
    """Print one structure's id, title, and property definitions."""
    sid = struct.get("id", "?")
    title = struct.get("title", "Untitled")
    props = struct.get("propertyDefinitions") or []
    print(f"  Structure: {title}  (id: {sid})")
    if props:
        for p in props:
            pname = p.get("name", p.get("id", "?"))
            ptype = p.get("type", "?")
            print(f"    - {pname} ({ptype})")
    else:
        print("    (no property definitions returned)")


def match_templates_to_structures(
    structures: List[Dict[str, Any]],
) -> None:
    """Compare known templates against discovered structures by
    property-name overlap (title alone is not reliable)."""
    from capacities_tool.templates import (
        ai_conversation_object,
        powershell_script_object,
        powershell_function_object,
        repo_file_object,
    )

    templates: Dict[str, Dict[str, Any]] = {
        "ai_conversation_object": ai_conversation_object(
            title="Probe",
            source="session-export",
            timestamp="2026-01-01T00:00:00Z",
            participants="user",
            conversation_text="hello",
        ),
        "powershell_script_object": powershell_script_object(
            title="Probe",
            file_path="C:/example/script.ps1",
            version="1.0",
            script_text="Write-Host 'hello'",
            functions=[],
        ),
        "powershell_function_object": powershell_function_object(
            title="Probe",
            function_name="TestFunc",
            parameters=[],
            description="test",
            function_code="Write-Host 'hello'",
        ),
        "repo_file_object": repo_file_object(
            title="Probe",
            file_path="C:/example/file.txt",
            file_type="text",
            content="hello",
            version="1.0",
        ),
    }

    discovered_titles = {s.get("title", "") for s in structures}

    print("\n--- Template vs. discovered structure match ---")
    for tname, tobj in templates.items():
        template_props = set(tobj.get("properties", {}).keys())
        # Score each discovered structure by property-name overlap.
        scores: List[tuple[int, str, str]] = []
        for s in structures:
            struct_props = {
                p.get("id") or p.get("name") for p in (s.get("propertyDefinitions") or [])
            }
            overlap = template_props & struct_props
            scores.append((len(overlap), s.get("title", "?"), s.get("id", "?")))
        scores.sort(reverse=True)
        best = scores[0] if scores else (0, "?", "?")
        status = "viable" if best[0] > 0 else "no property overlap"
        print(f"  {tname}: best match '{best[1]}' (id: {best[2]}) "
              f"[{best[0]} overlapping property] -> {status}")
        if best[0] == 0:
            print(f"    Template properties: {sorted(template_props)}")
            print(f"    Discovered titles: {sorted(discovered_titles)}")


def main() -> int:
    try:
        config = CapacitiesConfig.load()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Set CAPACITIES_API_TOKEN and CAPACITIES_SPACE_ID.", file=sys.stderr)
        return 1

    client = CapacitiesClient(config)

    space_id = config.space_id
    if not space_id:
        print("ERROR: CAPACITIES_SPACE_ID is required.", file=sys.stderr)
        return 1

    print(f"Connecting to {config.base_url} (api-version {config.api_version})")
    print(f"Space: {space_id}\n")

    try:
        structures = poll_structures(client)
    except CapacitiesError as exc:
        print(f"ERROR: API call failed: {exc}", file=sys.stderr)
        return 1

    print(f"Found {len(structures)} structure(s) in space {space_id}:\n")
    for s in structures:
        print_structure(s)
        print()

    match_templates_to_structures(structures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
