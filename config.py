"""Configuration for the Capacities automation tool.

Reads connection settings from environment variables and an optional
INI-style config file. Secrets (API token) are never hard-coded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Default Capacities API base URL.
DEFAULT_BASE_URL = "https://api.capacities.io"
# API version header required on every request.
DEFAULT_API_VERSION = "1.0.0"

# Environment variable names.
ENV_API_TOKEN = "CAPACITIES_API_TOKEN"
ENV_BASE_URL = "CAPACITIES_BASE_URL"
ENV_API_VERSION = "CAPACITIES_API_VERSION"
ENV_SPACE_ID = "CAPACITIES_SPACE_ID"
ENV_CONFIG_FILE = "CAPACITIES_CONFIG_FILE"

# Default config file locations (checked in order).
DEFAULT_CONFIG_FILES = [
    Path.home() / ".capacities" / "config.ini",
    Path.cwd() / "capacities.ini",
    Path.cwd() / ".capacities.ini",
]


@dataclass
class CapacitiesConfig:
    """Connection configuration for the Capacities API."""

    api_token: str
    base_url: str = DEFAULT_BASE_URL
    api_version: str = DEFAULT_API_VERSION
    space_id: Optional[str] = None
    config_file: Optional[Path] = None

    @property
    def auth_header(self) -> str:
        """Bearer authorization header value."""
        return f"Bearer {self.api_token}"

    @property
    def version_header(self) -> str:
        """API version header value."""
        return self.api_version

    @classmethod
    def load(cls, config_file: Optional[Path] = None) -> "CapacitiesConfig":
        """Load configuration from environment and optional config file.

        Environment variables take precedence over the config file. The
        config file is an INI-style file with a ``[capacities]`` section.
        """
        # Resolve config file path.
        if config_file is None:
            config_file = os.environ.get(ENV_CONFIG_FILE)
        if config_file is None:
            for candidate in DEFAULT_CONFIG_FILES:
                if candidate.exists():
                    config_file = candidate
                    break
        if config_file is None:
            config_file = DEFAULT_CONFIG_FILES[0]

        # Start with environment values (highest precedence).
        api_token = os.environ.get(ENV_API_TOKEN, "")
        base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)
        api_version = os.environ.get(ENV_API_VERSION, DEFAULT_API_VERSION)
        space_id = os.environ.get(ENV_SPACE_ID)

        # Overlay config file values (lower precedence than env).
        if config_file and config_file.exists():
            file_token = _read_ini_value(config_file, "api_token")
            file_base_url = _read_ini_value(config_file, "base_url")
            file_api_version = _read_ini_value(config_file, "api_version")
            file_space_id = _read_ini_value(config_file, "space_id")

            if file_token:
                api_token = file_token
            if file_base_url:
                base_url = file_base_url
            if file_api_version:
                api_version = file_api_version
            if file_space_id:
                space_id = file_space_id

        if not api_token:
            raise ValueError(
                "Capacities API token is required. Set the "
                f"{ENV_API_TOKEN} environment variable or add it to the "
                "config file."
            )

        return cls(
            api_token=api_token,
            base_url=base_url,
            api_version=api_version,
            space_id=space_id,
            config_file=config_file,
        )


def _read_ini_value(config_file: Path, key: str) -> Optional[str]:
    """Read a single key from the [capacities] section of an INI file."""
    try:
        import configparser  # noqa: PLC0415

        parser = configparser.ConfigParser()
        parser.read(config_file)
        if parser.has_section("capacities") and parser.has_option("capacities", key):
            return parser.get("capacities", key)
    except Exception:
        return None
    return None
