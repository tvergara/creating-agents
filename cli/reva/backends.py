"""Backend definitions: command templates and system-prompt filenames per backend."""

import json
from dataclasses import dataclass, field

# Paper Lantern MCP server config, inlined into the claude-code command template.
# The JSON is wrapped in single quotes at the shell level so its internal double
# quotes pass through unchanged; `\'` escapes the single quotes inside the Python
# string. Braces are doubled ({{ / }}) so that reva's str.format() call in
# cli.py (which substitutes {prompt} for other backends) does not interpret them
# as format fields — the doubling collapses back to single braces at format time.
_PAPER_LANTERN_MCP_CONFIG = (
    '\'{{"mcpServers":{{"paperlantern":'
    '{{"type":"http","url":"https://mcp.paperlantern.ai/chat/mcp?key=pl_cd1099cd5b35f6c193f9"}}'
    '}}}}\''
)

_PAPER_LANTERN_MCP_URL = "https://mcp.paperlantern.ai/chat/mcp?key=pl_cd1099cd5b35f6c193f9"

# Gemini CLI currently requires a newer Node runtime than the user's default
# shell PATH provides in this workspace. Prepending the Node 20 nvm bin keeps
# the backend working without changing the user's global shell config.
_GEMINI_NODE20_BIN = '$HOME/.nvm/versions/node/v20.20.2/bin'

# Gemini CLI reads .gemini/settings.json from the working directory for MCP config.
_GEMINI_SETTINGS = json.dumps({
    "mcpServers": {
        "paperlantern": {
            "httpUrl": _PAPER_LANTERN_MCP_URL,
        }
    }
}, indent=2)


@dataclass(frozen=True)
class Backend:
    name: str
    prompt_filename: str  # backend-specific system prompt file (e.g. CLAUDE.md)
    command_template: str  # shell command; {prompt} is replaced with initial prompt
    resume_command_template: str | None = None  # command to resume last session
    # Shell command run after each invocation whose stdout is written to
    # last_session_id. Only used when resume_command_template contains
    # $SESSION_ID. When None and $SESSION_ID is present, the session ID is
    # parsed from the stream-json agent.log (claude-code default).
    session_id_extractor: str | None = None
    # Files to write into the agent directory at creation time (relative path -> content).
    setup_files: dict[str, str] = field(default_factory=dict)


BACKENDS: dict[str, Backend] = {
    "claude-code": Backend(
        name="claude-code",
        prompt_filename="CLAUDE.md",
        command_template=(
            'claude -p "$(cat initial_prompt.txt)"'
            " --dangerously-skip-permissions"
            " --output-format stream-json --verbose"
            f" --mcp-config {_PAPER_LANTERN_MCP_CONFIG}"
            " 2>&1 | tee -a agent.log"
        ),
        # session_id parsed from stream-json log by tmux.py (_EXTRACT_SESSION_ID_FROM_LOG)
        resume_command_template='claude --resume "$SESSION_ID" --dangerously-skip-permissions --output-format stream-json --verbose 2>&1 | tee -a agent.log',
    ),
    "gemini-cli": Backend(
        name="gemini-cli",
        prompt_filename="GEMINI.md",
        command_template=f'env PATH={_GEMINI_NODE20_BIN}:$PATH gemini --yolo --prompt "{{prompt}}"',
        resume_command_template=f'env PATH={_GEMINI_NODE20_BIN}:$PATH gemini --yolo --resume',
        setup_files={".gemini/settings.json": _GEMINI_SETTINGS},
    ),
    "codex": Backend(
        name="codex",
        prompt_filename="AGENTS.md",
        command_template='codex --dangerously-bypass-approvals-and-sandbox "{prompt}"',
        # --last resumes the most recent session in the current working directory
        resume_command_template='codex resume --last --dangerously-bypass-approvals-and-sandbox',
    ),
    "aider": Backend(
        name="aider",
        prompt_filename="AIDER.md",
        # aider auto-persists chat history in .aider.chat.history.md; no
        # explicit resume needed — context is available on every restart.
        command_template='aider --yes --message "{prompt}"',
    ),
    "opencode": Backend(
        name="opencode",
        prompt_filename="OPENCODE.md",
        command_template='opencode run --dangerously-skip-permissions "{prompt}"',
        resume_command_template='opencode run --session "$SESSION_ID" --dangerously-skip-permissions',
        session_id_extractor=(
            'opencode session list --format json -n 1 2>/dev/null'
            ' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0][\'id\'] if d else \'\')"'
        ),
    ),
}

BACKEND_CHOICES = list(BACKENDS.keys())


def get_backend(name: str) -> Backend:
    if name not in BACKENDS:
        raise ValueError(f"Unknown backend: {name!r}. Choose from: {BACKEND_CHOICES}")
    return BACKENDS[name]


def write_setup_files(backend: Backend, agent_dir) -> None:
    """Write any backend-specific setup files into agent_dir."""
    from pathlib import Path
    for rel_path, content in backend.setup_files.items():
        dest = Path(agent_dir) / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
