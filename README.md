# Capacities Automation Tool

A Python tool that integrates with the [Capacities REST API](https://developers.capacities.io/)
to automate notebook management for software repositories, with a focus on
PowerShell scripts, function extraction, version tracking, and conversation
ingestion.

## Overview

The tool provides:

1. **Capacities API client** (`api_client.py`) — authenticated REST client with
   rate limiting, retry, and error handling for all documented endpoints.
2. **Notebook manager** (`notebook_manager.py`) — discovers or creates custom
   object types (structures), and idempotently creates/updates objects using
   those structures as templates.
3. **PowerShell parser** (`ps1_parser.py`) — extracts function definitions
   from `.ps1` files and validates syntax (via `pwsh` when available, falling
   back to a lightweight Python check).
4. **Script versioning** (`script_versioning.py`) — tracks script versions as
   linked Capacities objects, preserving history on every change.
5. **Conversation ingestion** (`conversation_ingest.py`) — puts conversations
   into AI Conversation items with deduplication.
6. **Git sync** (`git_sync.py`) — synchronizes a repository's files to Capacities
   objects, with per-file-type templates and PowerShell function extraction.

## Prerequisites

- Python 3.10+
- A Capacities workspace with API access
- A Capacities API token (`cap-api-...`) from Settings → Capacities API in the
  desktop app, with `api:write` scope
- PowerShell (`pwsh`) is optional; syntax checking degrades gracefully without it

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure connection (token is required)
export CAPACITIES_API_TOKEN="cap-api-..."
export CAPACITIES_SPACE_ID="<your-space-id>"  # optional

# Or create a config file at ~/.capacities/config.ini:
# [capacities]
# api_token = cap-api-...
# base_url = https://api.capacities.io
# api_version = 1.0.0
# space_id = <your-space-id>
```

## Custom Object Types (Required)

Before using the tool, create these custom object types in the Capacities
desktop app (Settings → Object types), or the tool will attempt to create
them via the API:

| Friendly name | Structure title | Purpose |
|---|---|---|
| `script` | PowerShell Script | Represents a `.ps1` file |
| `function` | PowerShell Function | Represents a function within a script |
| `conversation` | AI Conversation | Represents an AI conversation |
| `repo_file` | Repository File | Represents any other repository file |

Each structure should have property definitions matching the templates in
`templates.py` (title, description, filePath, version, functionName,
parameters, source, timestamp, participants, etc.).

> **RootAIChat limitation:** The `AI Conversation` structure (title) is a
> built-in structure that Capacities uses for its own AI chat UI. Objects of
> this type **cannot be created via the API** — `POST /object` and
> `POST /object/markdown` both return 400
> `"This object type cannot be created via this endpoint"`. The tool works
> around this by falling back to the built-in `RootPage` structure (which
> *is* creatable via `POST /object/markdown`) when `RootAIChat` creation
> fails. Conversation content is stored as markdown blocks under RootPage.
> Deduplication searches both RootAIChat and RootPage objects by title.

## Usage

```bash
# Initialize a repository (scans all files, creates Capacities objects)
python -m capacities_tool main init-repo /path/to/repo

# Sync a single file
python -m capacities_tool main sync-file /path/to/file.ps1

# Parse a PowerShell file (report functions + syntax)
python -m capacities_tool main parse-ps1 /path/to/script.ps1

# Ingest a conversation from a file
python -m capacities_tool main ingest-conversation /path/to/conversation.txt \
  --source file --participants "user,assistant"

# Register a PowerShell script with version tracking
python -m capacities_tool main register-script /path/to/script.ps1 --version 1.0.0
```

## Architecture

```
capacities_tool/
├── config.py          # Configuration (env vars + INI config file)
├── api_client.py      # Capacities REST API client (retry, rate limiting)
├── notebook_manager.py # Notebook management (structures, objects, tags)
├── ps1_parser.py      # PowerShell parsing + syntax validation
├── templates.py       # Object/block templates
├── script_versioning.py # Script version tracking
├── conversation_ingest.py # Conversation ingestion
├── git_sync.py        # Git repository synchronization
└── main.py            # CLI entry point
```

## Key Design Decisions

- **Idempotency**: Re-running the tool on the same repository does not create
  duplicate objects. Objects are located by title and updated in place.
- **Rate limiting**: Token-bucket rate limiting per endpoint category, with
  exponential backoff and retry on 429/5xx responses.
- **Syntax validation**: PowerShell syntax is validated via `pwsh` when
  available; otherwise a lightweight brace/paren balance check is used.
- **Version tracking**: Each script change creates a new version object linked
  to the script object. The script object always reflects the latest content.
- **Templates**: Custom Capacities structures serve as templates; the tool
  discovers them by title and creates objects using their property definitions.

## Contributing

Contributions welcome. Please file issues or pull requests for bugs, new
file-type support, or improvements to the PowerShell parser.
