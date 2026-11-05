"""Git repository synchronization with Capacities notebook.

When a git repository is initialized or modified, this module ensures
the Capacities notebook reflects the repository structure: one object
type per file type, with templates and blocks for each file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api_client import CapacitiesClient, CapacitiesError
from .notebook_manager import NotebookManager
from .ps1_parser import parse_file
from .script_versioning import ScriptVersioning
from .templates import (
    code_block,
    heading_block,
    text_block,
    repo_file_object,
    powershell_script_object,
    powershell_function_object,
    title_property,
    text_property,
    entity_property,
)

logger = logging.getLogger(__name__)

# File type -> Capacities structure friendly name mapping.
FILE_TYPE_MAP: Dict[str, str] = {
    ".ps1": "script",
    ".py": "repo_file",
    ".js": "repo_file",
    ".ts": "repo_file",
    ".md": "repo_file",
    ".txt": "repo_file",
    ".json": "repo_file",
    ".yaml": "repo_file",
    ".yml": "repo_file",
    ".toml": "repo_file",
    ".cfg": "repo_file",
    ".ini": "repo_file",
    ".html": "repo_file",
    ".css": "repo_file",
}


class GitSync:
    """Synchronizes a git repository with a Capacities notebook."""

    def __init__(self, client: CapacitiesClient, manager: NotebookManager):
        self.client = client
        self.manager = manager
        self.versioning = ScriptVersioning(client, manager)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def init_repository(self, repo_path: str) -> Dict[str, Any]:
        """Initialize a repository: scan files and create Capacities objects.

        Each file is represented as an object using the appropriate
        structure. PowerShell files additionally get function sub-objects
        and version tracking.
        """
        path = Path(repo_path)
        if not path.is_dir():
            raise ValueError(f"Not a directory: {repo_path}")

        structures = self.manager.ensure_structures()

        # Collect files (ignore .git, __pycache__, etc.).
        files: List[Path] = []
        for p in path.rglob("*"):
            if p.is_file() and not self._is_ignored(p):
                files.append(p)

        results: Dict[str, Any] = {
            "files_processed": 0,
            "objects_created": [],
            "errors": [],
        }

        for file_path in files:
            try:
                obj = self._process_file(file_path, structures)
                if obj is not None:
                    results["objects_created"].append(obj)
                results["files_processed"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to process %s: %s", file_path, exc)
                results["errors"].append({"file": str(file_path), "error": str(exc)})

        logger.info(
            "Repository sync complete: %d files processed, %d objects created.",
            results["files_processed"],
            len(results["objects_created"]),
        )
        return results

    def sync_file(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Sync a single file (create or update its Capacities object)."""
        path = Path(file_path)
        if not path.exists():
            return None
        structures = self.manager.ensure_structures()
        return self._process_file(path, structures)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _is_ignored(self, path: Path) -> bool:
        """Return True if the path should be ignored."""
        parts = path.parts
        if ".git" in parts or "__pycache__" in parts:
            return True
        if path.name.startswith("."):
            return True
        return False

    def _process_file(
        self, file_path: Path, structures: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Process a single file into a Capacities object."""
        suffix = file_path.suffix.lower()
        friendly = FILE_TYPE_MAP.get(suffix, "repo_file")

        # Read file content.
        try:
            raw = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary file: skip or represent as reference.
            logger.debug("Skipping binary file: %s", file_path)
            return None
        except OSError as exc:
            logger.error("Cannot read %s: %s", file_path, exc)
            return None

        rel_path = str(file_path.relative_to(file_path.parent.parent))
        title = file_path.name

        if suffix == ".ps1":
            return self._process_powershell_file(file_path, raw, structures)

        # Generic file object.
        struct = structures.get(friendly)
        if struct is None:
            logger.warning("No structure for file type %s", suffix)
            return None

        existing = self.manager.find_object_by_title(struct["id"], title)
        template = repo_file_object(
            title=title,
            file_path=str(file_path),
            file_type=suffix.lstrip("."),
            content=raw,
            version="1.0.0",
        )

        if existing is not None:
            return self.manager.update_object_from_template(
                friendly_structure=friendly,
                object_id=existing["id"],
                title=title,
                properties=template["properties"],
                blocks=template["blocks"],
            )
        return self.manager.create_object_from_template(
            friendly_structure=friendly,
            title=title,
            properties=template["properties"],
            blocks=template["blocks"],
        )

    def _process_powershell_file(
        self, file_path: Path, raw: str, structures: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Process a PowerShell file: script object + function objects."""
        # Parse functions.
        from .ps1_parser import parse_file

        parse_result = parse_file(str(file_path))

        # Register the script (creates script object + version object).
        script_obj = self.versioning.register_script(
            file_path=str(file_path),
            script_text=raw,
            version="1.0.0",
        )

        # Create function sub-objects.
        function_objects: List[Dict[str, Any]] = []
        for fn in parse_result.functions:
            fn_obj = self._create_function_object(
                file_path, fn, script_obj["id"], structures
            )
            if fn_obj is not None:
                function_objects.append(fn_obj)

        # Tag the script object with function names.
        self._tag_script_with_functions(script_obj["id"], parse_result.functions)

        return {
            "script": script_obj,
            "functions": function_objects,
            "parse_result": parse_result,
        }

    def _create_function_object(
        self,
        file_path: Path,
        fn,
        script_id: str,
        structures: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Create a Capacities object for a PowerShell function."""
        func_struct = structures.get("function")
        if func_struct is None:
            logger.warning("PowerShell Function structure not available.")
            return None

        title = f"Function: {fn.name} (from {file_path.name})"
        existing = self.manager.find_object_by_title(func_struct["id"], title)

        # Parameters as text.
        param_lines = []
        for p in fn.parameters:
            line = f"{p.name}"
            if p.type:
                line += f": {p.type}"
            if p.default is not None:
                line += f" = {p.default}"
            param_lines.append(line)
        params_text = "\n".join(param_lines) if param_lines else "No parameters."

        from .templates import powershell_function_object

        template = powershell_function_object(
            title=title,
            function_name=fn.name,
            parameters=fn.parameters,
            description=fn.body[:200] + "..." if len(fn.body) > 200 else fn.body,
            function_code=fn.body,
            parent_script_id=script_id,
        )

        if existing is not None:
            return self.manager.update_object_from_template(
                friendly_structure="function",
                object_id=existing["id"],
                title=title,
                properties=template["properties"],
                blocks=template["blocks"],
            )
        return self.manager.create_object_from_template(
            friendly_structure="function",
            title=title,
            properties=template["properties"],
            blocks=template["blocks"],
        )

    def _tag_script_with_functions(
        self, script_id: str, functions
    ) -> None:
        """Add function names as tags on the script object."""
        script_obj = self.client.get_object(script_id)
        script_struct_id = script_obj.get("structureId")
        tag_prop_id = self.manager.tag_property_id(script_struct_id)
        if tag_prop_id is None:
            return

        tag_names = [f.name for f in functions]
        self.manager.add_tags_to_object(script_id, script_struct_id, tag_names)
