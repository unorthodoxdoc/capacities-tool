"""PowerShell script parsing and syntax validation.

Extracts function definitions from .ps1 files and validates syntax.
Since PowerShell may not be installed on the host, syntax checking
attempts ``pwsh`` first and falls back to a lightweight Python-based
check.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Regex to match a PowerShell function definition:
#   function <Name> [param(...) ] { <body> }
FUNCTION_RE = re.compile(
    r"\bfunction\s+([A-Za-z_][\w-]*)\s*"
    r"(?:\([^)]*\))?"
    r"\s*\{",
    re.IGNORECASE,
)

# Match parameter declarations inside a param block or function signature.
PARAM_RE = re.compile(
    r"\bparam\s*\(([^)]*)\)",
    re.IGNORECASE,
)


@dataclass
class Parameter:
    name: str
    type: Optional[str] = None
    default: Optional[str] = None
    mandatory: bool = False
    pipeline: bool = False


@dataclass
class FunctionDefinition:
    name: str
    line_start: int
    line_end: int
    parameters: List[Parameter] = field(default_factory=list)
    body: str = ""
    raw_text: str = ""


@dataclass
class ParseResult:
    file_path: str
    functions: List[FunctionDefinition] = field(default_factory=list)
    syntax_valid: bool = False
    syntax_errors: List[str] = field(default_factory=list)
    raw_text: str = ""


def _pwsh_available() -> bool:
    return shutil.which("pwsh") is not None or shutil.which("powershell") is not None


def _powershell_syntax_check(text: str, file_path: str) -> Tuple[bool, List[str]]:
    """Validate PowerShell syntax using pwsh if available."""
    if not _pwsh_available():
        return _basic_syntax_check(text), []

    # Write the script to a temp file and ask pwsh to parse it without executing.
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(text)
        tmp_path = tmp.name

    try:
        # Use the parser to check for syntax errors. The -ErrorAction Stop
        # ensures parse errors are surfaced. We do not execute the script.
        cmd = [
            "pwsh",
            "-NoProfile",
            "-Command",
            f"[System.Management.Automation.Language.Parser]::ParseFile('{tmp_path}') | "
            f"Select-Object -ExpandProperty Errors | "
            f"Select-Object -ExpandProperty Message",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        errors = [line for line in result.stdout.splitlines() if line.strip()]
        return (result.returncode == 0 and not errors), errors
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("PowerShell syntax check failed: %s", exc)
        # Infrastructure failure (e.g. pwsh not executable) must not
        # pollute syntax_errors; fall back to the lightweight check.
        return _basic_syntax_check(text), []
    finally:
        try:
            import os

            os.unlink(tmp_path)
        except OSError:
            pass


def _basic_syntax_check(text: str) -> bool:
    """Lightweight syntax validation without PowerShell.

    Checks balanced braces, parentheses, and brackets, and that comments
    are handled. This is a heuristic and may produce false positives/
    negatives; it is only a fallback when pwsh is unavailable.
    """
    # Strip comments (single-line and block).
    text = re.sub(r"#.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\/\*.*?\*\/\s*", "", text, flags=re.DOTALL)

    # Check balanced braces, parens, brackets.
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: List[str] = []
    for ch in text:
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in pairs.values():
            if not stack or stack.pop() != ch:
                return False
    return len(stack) == 0


def parse_file(file_path: str) -> ParseResult:
    """Parse a PowerShell script file.

    Returns a ParseResult with extracted function definitions and
    syntax validation status.
    """
    path = Path(file_path) if isinstance(file_path, str) else file_path
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read %s: %s", file_path, exc)
        return ParseResult(
            file_path=str(file_path),
            syntax_valid=False,
            syntax_errors=[str(exc)],
        )

    # Syntax validation.
    syntax_valid, syntax_errors = _powershell_syntax_check(raw_text, str(file_path))

    # Extract function definitions.
    functions: List[FunctionDefinition] = []
    lines = raw_text.splitlines()
    for match in FUNCTION_RE.finditer(raw_text):
        func_name = match.group(1)
        start_line = raw_text[: match.start()].count("\n") + 1

        # Find the matching closing brace by brace counting.
        body_start = match.end()
        depth = 1
        end_pos = body_start
        for i in range(body_start, len(raw_text)):
            ch = raw_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_pos = i + 1
                    break
        if depth != 0:
            # Unbalanced; skip this function.
            continue

        body_text = raw_text[body_start:end_pos - 1]
        end_line = raw_text[:end_pos].count("\n") + 1

        # Extract parameters from a param block if present.
        parameters: List[Parameter] = []
        param_match = PARAM_RE.search(body_text)
        if param_match:
            param_text = param_match.group(1)
            for p in re.finditer(
                r"\b([A-Za-z_][\w]*)\s*(?:\[([^\]]*)\])?\s*(?:=\s*([^,]+))?",
                param_text,
            ):
                pname = p.group(1)
                ptype = p.group(2)
                pdefault = p.group(3)
                parameters.append(
                    Parameter(
                        name=pname,
                        type=ptype,
                        default=pdefault.strip() if pdefault else None,
                    )
                )

        functions.append(
            FunctionDefinition(
                name=func_name,
                line_start=start_line,
                line_end=end_line,
                parameters=parameters,
                body=body_text.strip(),
                raw_text=raw_text[match.start():end_pos],
            )
        )

    return ParseResult(
        file_path=str(file_path),
        functions=functions,
        syntax_valid=syntax_valid,
        syntax_errors=syntax_errors,
        raw_text=raw_text,
    )


def parse_text(text: str, file_path: str = "<stdin>") -> ParseResult:
    """Parse PowerShell text (convenience wrapper)."""
    syntax_valid = _basic_syntax_check(text)
    syntax_errors: List[str] = []
    if not syntax_valid:
        syntax_errors.append("Unbalanced braces or parentheses detected")
    return ParseResult(
        file_path=file_path,
        functions=[],
        syntax_valid=syntax_valid,
        syntax_errors=syntax_errors,
        raw_text=text,
    )


# Backwards-compatible alias.
parse_ps1 = parse_file
