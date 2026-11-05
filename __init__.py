"""Capacities automation tool package."""

from .config import CapacitiesConfig
from .api_client import CapacitiesClient, CapacitiesError, RateLimitExceeded
from .notebook_manager import NotebookManager
from .ps1_parser import parse_file, parse_text, FunctionDefinition, Parameter, ParseResult
from .script_versioning import ScriptVersioning
from .conversation_ingest import ConversationIngest
from .git_sync import GitSync

__all__ = [
    "CapacitiesConfig",
    "CapacitiesClient",
    "CapacitiesError",
    "RateLimitExceeded",
    "NotebookManager",
    "parse_file",
    "parse_text",
    "FunctionDefinition",
    "Parameter",
    "ParseResult",
    "ScriptVersioning",
    "ConversationIngest",
    "GitSync",
]

__version__ = "0.1.0"
