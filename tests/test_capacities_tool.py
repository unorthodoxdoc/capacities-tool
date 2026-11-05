"""Tests for the Capacities automation tool.

Covers: config, API client, PowerShell parser, templates, notebook
manager (mocked), git sync, script versioning, and conversation
ingestion. All API-facing tests use mocked clients so no network is
required.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from capacities_tool.config import CapacitiesConfig
from capacities_tool.api_client import (
    CapacitiesClient,
    CapacitiesError,
    RateLimitExceeded,
)
from capacities_tool.notebook_manager import NotebookManager
from capacities_tool.ps1_parser import (
    parse_file,
    parse_text,
    FunctionDefinition,
    Parameter,
    ParseResult,
)
from capacities_tool.templates import (
    code_block,
    text_block,
    heading_block,
    title_property,
    text_property,
    entity_property,
    powershell_script_object,
    powershell_function_object,
    ai_conversation_object,
    repo_file_object,
)

# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------
def test_config_defaults():
    """Config defaults should use documented defaults when no env is set."""
    with patch.dict(
        os.environ,
        {"CAPACITIES_API_TOKEN": "test-token-123"},
        clear=True,
    ):
        cfg = CapacitiesConfig.load()
        assert cfg.api_token == "test-token-123"
        assert cfg.base_url == "https://api.capacities.io"
        assert cfg.api_version == "1.0.0"
        assert cfg.space_id is None


def test_config_from_env():
    """Config should read from environment variables."""
    with patch.dict(
        os.environ,
        {
            "CAPACITIES_API_TOKEN": "cap-api-test123",
            "CAPACITIES_BASE_URL": "https://custom.example.com",
            "CAPACITIES_API_VERSION": "2.0.0",
            "CAPACITIES_SPACE_ID": "space-123",
        },
        clear=True,
    ):
        cfg = CapacitiesConfig.load()
        assert cfg.api_token == "cap-api-test123"
        assert cfg.base_url == "https://custom.example.com"
        assert cfg.api_version == "2.0.0"
        assert cfg.space_id == "space-123"


def test_config_requires_token():
    """Config.load() must raise when no token is available."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="API token is required"):
            CapacitiesConfig.load()


# ---------------------------------------------------------------------------
# API client tests
# ---------------------------------------------------------------------------
def test_client_raises_on_http_error():
    """Client should raise CapacitiesError on non-2xx responses."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not found"
    mock_response.json = MagicMock(return_value={"error": "not found"})

    client = CapacitiesClient.__new__(CapacitiesClient)
    client._client = MagicMock()
    client._client.request.return_value = mock_response
    client.config = MagicMock()
    client.config.api_version = "1.0.0"
    client.config.base_url = "https://api.capacities.io"

    with pytest.raises(CapacitiesError) as exc:
        client._request("GET", "/objects/test")
    assert "404" in str(exc.value)


def test_rate_limit_exceeded():
    """429 responses should raise RateLimitExceeded."""
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "Too many requests"
    mock_response.headers = {"Retry-After": "5"}

    client = CapacitiesClient.__new__(CapacitiesClient)
    client._client = MagicMock()
    client._client.request.return_value = mock_response
    client.config = MagicMock()
    client.config.api_version = "1.0.0"
    client.config.base_url = "https://api.capacities.io"

    with pytest.raises(RateLimitExceeded):
        client._request("GET", "/objects/test")


# ---------------------------------------------------------------------------
# PowerShell parser tests
# ---------------------------------------------------------------------------
def test_parse_file_basic_function():
    """Basic function extraction from a file should work."""
    script = (
        "function Get-User {\n"
        "    param($Name)\n"
        "    Write-Host \"Hello $Name\"\n"
        "}\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(script)
        tmp_path = tmp.name

    try:
        result = parse_file(tmp_path)
        assert result.syntax_valid is True
        assert len(result.functions) == 1
        fn = result.functions[0]
        assert fn.name == "Get-User"
        assert len(fn.parameters) == 1
        assert fn.parameters[0].name == "Name"
    finally:
        os.unlink(tmp_path)


def test_parse_text_syntax_only():
    """parse_text validates syntax but does not extract functions."""
    script = "Write-Host 'Hello'\n$x = 1 + 1\n"
    result = parse_text(script)
    assert result.syntax_valid is True
    assert result.functions == []
    assert result.raw_text == script


def test_parse_text_no_functions():
    """Scripts without functions should return empty function list."""
    script = "function Get-User {\n    Write-Host 'Hello'\n}\n"
    result = parse_text(script)
    assert result.syntax_valid is True
    assert len(result.functions) == 0


def test_parse_text_multiple_functions():
    """Multiple functions should all be extracted by parse_file."""
    script = (
        "function Get-Item {\n"
        "    param($Id)\n"
        "    return $Id\n"
        "}\n"
        "function Set-Item {\n"
        "    param($Id, $Value)\n"
        "    return $Value\n"
        "}\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(script)
        tmp_path = tmp.name

    try:
        result = parse_file(tmp_path)
        assert len(result.functions) == 2
        names = [f.name for f in result.functions]
        assert "Get-Item" in names
        assert "Set-Item" in names
    finally:
        os.unlink(tmp_path)


def test_parse_text_syntax_error():
    """Unbalanced braces should be detected as syntax errors."""
    script = (
        "function Broken {\n"
        "    Write-Host 'hello'\n"
        "    # missing closing brace\n"
    )
    result = parse_text(script)
    assert result.syntax_valid is False
    assert len(result.syntax_errors) > 0


# ---------------------------------------------------------------------------
# Template tests
# ---------------------------------------------------------------------------
def test_code_block():
    """CodeBlock template should produce correct structure."""
    block = code_block("powershell", "Write-Host 'hello'")
    assert block["type"] == "CodeBlock"
    assert block["lang"] == "powershell"
    assert block["text"] == "Write-Host 'hello'"


def test_text_block():
    """TextBlock template should produce correct structure."""
    block = text_block("Hello world")
    assert block["type"] == "TextBlock"
    assert block["tokens"][0]["type"] == "TextToken"
    assert block["tokens"][0]["text"] == "Hello world"
    assert block["blocks"] == []
    assert block["hierarchy"]["key"] == "Base"
    assert block["hierarchy"]["val"] == 0


def test_heading_block():
    """HeadingBlock template should produce correct structure."""
    block = heading_block("Title", level=1)
    assert block["type"] == "TextBlock"
    assert block["tokens"][0]["text"] == "Title"
    assert block["hierarchy"]["key"] == "Heading"
    assert block["hierarchy"]["val"] == 1


def test_title_property():
    """Title property should produce correct structure."""
    prop = title_property("My Title")
    assert prop["type"] == "title"
    assert prop["title"]["value"] == "My Title"


def test_text_property():
    """Text property should produce correct structure."""
    prop = text_property("Some text")
    assert prop["type"] == "text"
    assert prop["text"]["value"] == "Some text"


def test_entity_property():
    """Entity property should produce correct structure from string IDs."""
    prop = entity_property(["uuid-1", "uuid-2"])
    assert prop["type"] == "entity"
    assert prop["entity"] == [{"id": "uuid-1"}, {"id": "uuid-2"}]


def test_powershell_script_object():
    """PowerShell script object template should be well-formed."""
    obj = powershell_script_object(
        title="Test Script",
        file_path="C:/scripts/test.ps1",
        version="1.0.0",
        script_text="Write-Host 'hello'",
        functions=[],
    )
    # Templates return {"properties": ..., "blocks": {"content": [...]}}.
    assert obj["properties"]["title"]["title"]["value"] == "Test Script"
    assert obj["properties"]["filePath"]["text"]["value"] == "C:/scripts/test.ps1"
    assert obj["properties"]["version"]["text"]["value"] == "1.0.0"
    assert len(obj["blocks"]["content"]) >= 2  # heading + code block


def test_powershell_function_object():
    """PowerShell function object template should be well-formed."""
    fn = FunctionDefinition(
        name="Get-Thing",
        line_start=1,
        line_end=5,
        body="param($x)\nWrite-Host $x",
        parameters=[Parameter(name="x", type="string", default=None)],
    )
    obj = powershell_function_object(
        title="Function: Get-Thing",
        function_name="Get-Thing",
        parameters=fn.parameters,
        description="A test function",
        function_code=fn.body,
        parent_script_id="script-uuid",
    )
    assert obj["properties"]["title"]["title"]["value"] == "Function: Get-Thing"
    assert obj["properties"]["functionName"]["text"]["value"] == "Get-Thing"
    assert "parameters" in obj["properties"]
    assert obj["properties"]["parentScript"]["entity"][0]["id"] == "script-uuid"


def test_ai_conversation_object():
    """AI Conversation object template should be well-formed."""
    obj = ai_conversation_object(
        title="Test Conversation",
        source="file",
        timestamp="2026-01-01T00:00:00Z",
        participants="user,assistant",
        conversation_text="User: hello\nAssistant: hi",
    )
    assert obj["properties"]["title"]["title"]["value"] == "Test Conversation"
    assert obj["properties"]["source"]["text"]["value"] == "file"
    assert obj["properties"]["participants"]["text"]["value"] == "user,assistant"


def test_repo_file_object():
    """Repository File object template should be well-formed."""
    obj = repo_file_object(
        title="test.py",
        file_path="/repo/test.py",
        file_type="py",
        content="print('hello')",
        version="1.0.0",
    )
    assert obj["properties"]["title"]["title"]["value"] == "test.py"
    assert obj["properties"]["filePath"]["text"]["value"] == "/repo/test.py"
    assert obj["properties"]["fileType"]["text"]["value"] == "py"


# ---------------------------------------------------------------------------
# Notebook manager tests (mocked)
# ---------------------------------------------------------------------------
def test_manager_discovers_structures():
    """Manager should discover structures by title via client.structures()."""
    mock_client = MagicMock(spec=CapacitiesClient)
    mock_client.structures.return_value = {
        "structures": [
            {"id": "struct-1", "title": "PowerShell Script", "propertyDefinitions": []},
            {"id": "struct-2", "title": "PowerShell Function", "propertyDefinitions": []},
            {"id": "struct-3", "title": "AI Conversation", "propertyDefinitions": []},
            {"id": "struct-4", "title": "Repository File", "propertyDefinitions": []},
        ]
    }

    manager = NotebookManager(mock_client)
    structures = manager.ensure_structures()
    assert "script" in structures
    assert "function" in structures
    assert "conversation" in structures
    assert "repo_file" in structures
    assert structures["script"]["id"] == "struct-1"
    assert structures["function"]["id"] == "struct-2"


def test_manager_create_object_from_template():
    """Manager should create objects from templates."""
    mock_client = MagicMock(spec=CapacitiesClient)
    mock_client.structures.return_value = {
        "structures": [
            {"id": "struct-1", "title": "PowerShell Script", "propertyDefinitions": []},
        ]
    }
    mock_client.create_object.return_value = {"id": "obj-123", "title": "Test"}
    # Avoid infinite loop in find_object_by_title when search/list are unmocked.
    mock_client.search_objects.return_value = {"results": []}
    mock_client.list_objects_by_structure.return_value = {
        "results": [],
        "hasMore": False,
    }

    manager = NotebookManager(mock_client)

    template = {
        "title": "Test Script",
        "properties": {"filePath": text_property("C:/test.ps1")},
        "blocks": {"content": [text_block("hello")]},
    }
    result = manager.create_object_from_template(
        friendly_structure="script",
        title="Test Script",
        properties=template["properties"],
        blocks=template["blocks"],
    )
    assert result["id"] == "obj-123"
    mock_client.create_object.assert_called_once()


def test_manager_create_object_from_template_updates_existing():
    """Manager should update an existing object with the same title."""
    mock_client = MagicMock(spec=CapacitiesClient)
    mock_client.structures.return_value = {
        "structures": [
            {"id": "struct-1", "title": "PowerShell Script", "propertyDefinitions": []},
        ]
    }
    # find_object_by_title -> search_objects returns an existing object.
    mock_client.search_objects.return_value = {
        "results": [
            {"id": "obj-existing", "title": "Test Script", "structureId": "struct-1"}
        ]
    }
    mock_client.update_object.return_value = {"id": "obj-existing", "title": "Test Script"}

    manager = NotebookManager(mock_client)

    template = {
        "title": "Test Script",
        "properties": {"filePath": text_property("C:/test.ps1")},
        "blocks": {"content": [text_block("hello")]},
    }
    result = manager.create_object_from_template(
        friendly_structure="script",
        title="Test Script",
        properties=template["properties"],
        blocks=template["blocks"],
    )
    assert result["id"] == "obj-existing"
    mock_client.update_object.assert_called_once()


def test_manager_find_object_by_title():
    """Manager should find objects by title via search."""
    mock_client = MagicMock(spec=CapacitiesClient)
    mock_client.search_objects.return_value = {
        "results": [
            {"id": "obj-1", "title": "Existing Object", "structureId": "struct-1"},
            {"id": "obj-2", "title": "Other Object", "structureId": "struct-1"},
        ]
    }

    manager = NotebookManager(mock_client)
    result = manager.find_object_by_title("struct-1", "Existing Object")
    assert result is not None
    assert result["id"] == "obj-1"

    # Not found: search returns empty results, list returns empty.
    mock_client.search_objects.return_value = {"results": []}
    mock_client.list_objects_by_structure.return_value = {
        "results": [],
        "hasMore": False,
    }
    result = manager.find_object_by_title("struct-1", "Non-existent")
    assert result is None


# ---------------------------------------------------------------------------
# Git sync tests
# ---------------------------------------------------------------------------
def test_git_sync_ignores_hidden_dirs():
    """GitSync should ignore .git and __pycache__."""
    from capacities_tool.git_sync import GitSync

    sync = GitSync(MagicMock(), MagicMock())
    assert sync._is_ignored(Path("/repo/.git/config")) is True
    assert sync._is_ignored(Path("/repo/__pycache__/foo.pyc")) is True
    assert sync._is_ignored(Path("/repo/script.ps1")) is False
    assert sync._is_ignored(Path("/repo/.hidden")) is True


# ---------------------------------------------------------------------------
# Script versioning tests
# ---------------------------------------------------------------------------
def test_versioning_next_version_default():
    """Next version should be 1.0.0 when no version exists."""
    from capacities_tool.script_versioning import ScriptVersioning

    mock_client = MagicMock(spec=CapacitiesClient)
    mock_manager = MagicMock(spec=NotebookManager)
    mock_client.get_object.return_value = {"properties": {}}

    v = ScriptVersioning(mock_client, mock_manager)
    assert v._next_version("dummy") == "1.0.0"


def test_versioning_next_version_bumps_minor():
    """Next version should bump the minor version."""
    from capacities_tool.script_versioning import ScriptVersioning

    mock_client = MagicMock(spec=CapacitiesClient)
    mock_manager = MagicMock(spec=NotebookManager)
    mock_client.get_object.return_value = {
        "properties": {"version": {"type": "text", "text": {"value": "1.0.0"}}}
    }

    v = ScriptVersioning(mock_client, mock_manager)
    assert v._next_version("dummy") == "1.1.0"


# ---------------------------------------------------------------------------
# Conversation ingest tests
# ---------------------------------------------------------------------------
def test_conversation_ingest_from_dict():
    """Conversation ingestion from dict should create an object."""
    from capacities_tool.conversation_ingest import ConversationIngest

    mock_client = MagicMock(spec=CapacitiesClient)
    mock_manager = MagicMock(spec=NotebookManager)
    mock_manager.ensure_structures.return_value = {
        "conversation": {"id": "conv-struct-1", "propertyDefinitions": []}
    }
    mock_manager.create_object_from_template.return_value = {
        "id": "conv-123",
        "title": "Test Chat",
    }

    ingest = ConversationIngest(mock_client, mock_manager)
    # No existing conversation, so ingestion should create a new object.
    mock_manager.find_object_by_title.return_value = None

    conv = {
        "title": "Test Chat",
        "participants": "user,assistant",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
    }
    result = ingest.ingest_from_dict(conv, source="test")
    assert result == {"id": "conv-123", "title": "Test Chat"}
    mock_manager.create_object_from_template.assert_called_once()


# ---------------------------------------------------------------------------
# RootPage fallback tests
# ---------------------------------------------------------------------------
def test_manager_root_page_id_found():
    """NotebookManager.root_page_id should return the RootPage structure id."""
    mock_client = MagicMock(spec=CapacitiesClient)
    mock_client.structures.return_value = {
        "structures": [
            {"id": "root-page-1", "title": "RootPage", "propertyDefinitions": []},
            {"id": "conv-1", "title": "AI Conversation", "propertyDefinitions": []},
        ]
    }

    manager = NotebookManager(mock_client)
    # Populate the structure cache via ensure_structures.
    manager.ensure_structures()
    assert manager.root_page_id() == "root-page-1"


def test_manager_root_page_id_not_found():
    """NotebookManager.root_page_id should return None when RootPage is absent."""
    mock_client = MagicMock(spec=CapacitiesClient)
    mock_client.structures.return_value = {
        "structures": [
            {"id": "conv-1", "title": "AI Conversation", "propertyDefinitions": []},
        ]
    }

    manager = NotebookManager(mock_client)
    manager.ensure_structures()
    assert manager.root_page_id() is None


def test_conversation_ingest_rootpage_fallback():
    """Conversation ingestion should fall back to RootPage when RootAIChat creation fails."""
    from capacities_tool.conversation_ingest import ConversationIngest

    mock_client = MagicMock(spec=CapacitiesClient)
    mock_manager = MagicMock(spec=NotebookManager)
    mock_manager.ensure_structures.return_value = {
        "conversation": {"id": "conv-struct-1", "propertyDefinitions": []}
    }
    mock_manager.root_page_id.return_value = "root-page-1"
    # RootAIChat creation fails (CapacitiesError), so we expect a fallback to RootPage.
    mock_manager.create_object_from_template.side_effect = CapacitiesError(
        "400 This object type cannot be created via this endpoint"
    )
    mock_client.create_from_markdown.return_value = {
        "id": "rootpage-123",
        "title": "Test Chat [hash]",
    }
    mock_manager.find_object_by_title.return_value = None

    ingest = ConversationIngest(mock_client, mock_manager)

    conv = {
        "title": "Test Chat",
        "participants": "user,assistant",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
    }
    result = ingest.ingest_from_dict(conv, source="test")
    assert result == {"id": "rootpage-123", "title": "Test Chat [hash]"}
    mock_manager.create_object_from_template.assert_called_once()
    mock_client.create_from_markdown.assert_called_once()
    # root_page_id is called by both _find_existing_conversation and
    # _ingest_via_rootpage.
    mock_manager.root_page_id.assert_called()
    assert mock_manager.root_page_id.call_count == 2


def test_conversation_ingest_rootpage_fallback_no_rootpage():
    """Conversation ingestion should return None when RootPage is unavailable."""
    from capacities_tool.conversation_ingest import ConversationIngest

    mock_client = MagicMock(spec=CapacitiesClient)
    mock_manager = MagicMock(spec=NotebookManager)
    mock_manager.ensure_structures.return_value = {
        "conversation": {"id": "conv-struct-1", "propertyDefinitions": []}
    }
    mock_manager.root_page_id.return_value = None
    mock_manager.create_object_from_template.side_effect = CapacitiesError(
        "400 This object type cannot be created via this endpoint"
    )
    mock_manager.find_object_by_title.return_value = None

    ingest = ConversationIngest(mock_client, mock_manager)

    conv = {
        "title": "Test Chat",
        "participants": "user,assistant",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
    }
    result = ingest.ingest_from_dict(conv, source="test")
    assert result is None
    mock_manager.create_object_from_template.assert_called_once()


def test_conversation_ingest_dedup_falls_back_to_rootpage():
    """_find_existing_conversation should fall back to RootPage for deduplication."""
    from capacities_tool.conversation_ingest import ConversationIngest

    mock_client = MagicMock(spec=CapacitiesClient)
    mock_manager = MagicMock(spec=NotebookManager)
    mock_manager.ensure_structures.return_value = {
        "conversation": {"id": "conv-struct-1", "propertyDefinitions": []}
    }
    mock_manager.root_page_id.return_value = "root-page-1"
    # RootAIChat search returns nothing, but RootPage has a match.
    mock_manager.find_object_by_title.side_effect = [None, {"id": "rootpage-456"}]

    ingest = ConversationIngest(mock_client, mock_manager)

    result = ingest._find_existing_conversation("hash-123", "Test Chat")
    assert result == {"id": "rootpage-456"}
    # Should have called find_object_by_title twice: once for RootAIChat, once for RootPage.
    assert mock_manager.find_object_by_title.call_count == 2
