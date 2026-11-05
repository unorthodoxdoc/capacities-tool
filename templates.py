"""Jinja2-style templates for Capacities object types.

These templates define the shape of objects and blocks used when
creating PowerShell script objects, function objects, and AI
conversation objects. They are rendered by the notebook manager and
consumed by the versioning and conversation ingestion modules.
"""

from __future__ import annotations

from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Block templates
# ---------------------------------------------------------------------------

def code_block(lang: str, text: str) -> Dict[str, Any]:
    """Build a Capacities CodeBlock."""
    return {
        "type": "CodeBlock",
        "lang": lang,
        "text": text,
    }


def text_block(text: str) -> Dict[str, Any]:
    """Build a Capacities TextBlock with a single TextToken."""
    return {
        "type": "TextBlock",
        "tokens": [{"type": "TextToken", "text": text, "style": {}}],
        "blocks": [],
        "hierarchy": {"key": "Base", "val": 0},
    }


def heading_block(text: str, level: int = 1) -> Dict[str, Any]:
    """Build a heading TextBlock."""
    return {
        "type": "TextBlock",
        "tokens": [{"type": "TextToken", "text": text, "style": {}}],
        "blocks": [],
        "hierarchy": {"key": "Heading", "val": level},
    }


# ---------------------------------------------------------------------------
# Object property templates (typed property values)
# ---------------------------------------------------------------------------

def title_property(value: str) -> Dict[str, Any]:
    return {"type": "title", "title": {"value": value}}


def text_property(value: str) -> Dict[str, Any]:
    return {"type": "text", "text": {"value": value}}


def entity_property(ids: List[str]) -> Dict[str, Any]:
    return {"type": "entity", "entity": [{"id": i} for i in ids]}


def label_property(ids: List[str]) -> Dict[str, Any]:
    return {"type": "label", "label": [{"id": i} for i in ids]}


# ---------------------------------------------------------------------------
# Full object templates
# ---------------------------------------------------------------------------

def powershell_script_object(
    title: str,
    file_path: str,
    version: str,
    script_text: str,
    functions: List[Dict[str, Any]],
    language: str = "PowerShell",
) -> Dict[str, Any]:
    """Template for a PowerShell Script object.

    Properties:
      - title, description, filePath, version, language
    Blocks (property 'content'):
      - heading with script name
      - code block with the full script (lang=powershell)
      - per-function code blocks
    """
    blocks: List[Dict[str, Any]] = []
    blocks.append(heading_block(f"PowerShell Script: {title}", level=1))
    blocks.append(text_block(f"File: {file_path}"))
    blocks.append(text_block(f"Version: {version}"))
    blocks.append(text_block(f"Language: {language}"))
    blocks.append(code_block("powershell", script_text))

    for fn in functions:
        blocks.append(heading_block(f"Function: {fn['name']}", level=2))
        blocks.append(text_block(f"Line {fn['line_start']}-{fn['line_end']}"))
        blocks.append(code_block("powershell", fn["body"]))

    properties: Dict[str, Any] = {
        "title": title_property(title),
        "description": text_property(
            f"PowerShell script from {file_path} (version {version})."
        ),
        "filePath": text_property(file_path),
        "version": text_property(version),
        "language": text_property(language),
    }
    return {"properties": properties, "blocks": {"content": blocks}}


def _param_attr(p: Any, attr: str, default: Any = "") -> Any:
    """Read an attribute from a parameter dict or dataclass object."""
    if isinstance(p, dict):
        return p.get(attr, default)
    return getattr(p, attr, default)


def powershell_function_object(
    title: str,
    function_name: str,
    parameters: List[Any],
    description: str,
    function_code: str,
    parent_script_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Template for a PowerShell Function object.

    Properties:
      - title, functionName, parameters, description
    Blocks:
      - description
      - parameters list
      - function code block
    """
    blocks: List[Dict[str, Any]] = []
    blocks.append(heading_block(f"Function: {function_name}", level=1))
    blocks.append(text_block(description or "No description provided."))
    blocks.append(heading_block("Parameters", level=2))
    if parameters:
        for p in parameters:
            blocks.append(
                text_block(
                    f"- {_param_attr(p, 'name', '')} "
                    f"{_param_attr(p, 'type', '')} "
                    f"{'= ' + _param_attr(p, 'default', '') if _param_attr(p, 'default') else ''}"
                )
            )
    else:
        blocks.append(text_block("No parameters."))
    blocks.append(heading_block("Code", level=2))
    blocks.append(code_block("powershell", function_code))

    properties: Dict[str, Any] = {
        "title": title_property(title),
        "functionName": text_property(function_name),
        "parameters": text_property(
            "\n".join(
                f"{_param_attr(p, 'name', '')}: {_param_attr(p, 'type', '')}"
                for p in parameters
            )
        ),
        "description": text_property(description or ""),
    }
    if parent_script_id:
        properties["parentScript"] = entity_property([parent_script_id])

    return {"properties": properties, "blocks": {"content": blocks}}


def ai_conversation_object(
    title: str,
    source: str,
    timestamp: str,
    participants: str,
    conversation_text: str,
) -> Dict[str, Any]:
    """Template for an AI Conversation object.

    Uses the RootAIChat structure when available, otherwise a custom
    structure. Blocks contain the full conversation as text.
    """
    blocks: List[Dict[str, Any]] = []
    blocks.append(heading_block(f"Conversation: {title}", level=1))
    blocks.append(text_block(f"Source: {source}"))
    blocks.append(text_block(f"Timestamp: {timestamp}"))
    blocks.append(text_block(f"Participants: {participants}"))
    blocks.append(heading_block("Conversation", level=2))
    blocks.append(text_block(conversation_text))

    properties: Dict[str, Any] = {
        "title": title_property(title),
        "source": text_property(source),
        "timestamp": text_property(timestamp),
        "participants": text_property(participants),
    }
    return {"properties": properties, "blocks": {"content": blocks}}


def repo_file_object(
    title: str,
    file_path: str,
    file_type: str,
    content: str,
    version: str,
) -> Dict[str, Any]:
    """Template for a generic repository file object."""
    blocks: List[Dict[str, Any]] = []
    blocks.append(heading_block(f"File: {title}", level=1))
    blocks.append(text_block(f"Path: {file_path}"))
    blocks.append(text_block(f"Type: {file_type}"))
    blocks.append(text_block(f"Version: {version}"))
    if file_type.lower() == "powershell" or file_type.lower() == "ps1":
        blocks.append(code_block("powershell", content))
    else:
        blocks.append(code_block("text", content))

    properties: Dict[str, Any] = {
        "title": title_property(title),
        "filePath": text_property(file_path),
        "fileType": text_property(file_type),
        "version": text_property(version),
    }
    return {"properties": properties, "blocks": {"content": blocks}}
