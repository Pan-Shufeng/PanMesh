"""Command construction and event decoding for persistent CLI sessions."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]


def executable(client):
    if client == "claude":
        packaged = (
            ROOT / "runtime/connector-tools/claude/node_modules/@anthropic-ai"
            / "claude-code-win32-x64/claude.exe"
        )
        found = shutil.which("claude.exe" if os.name == "nt" else "claude")
        candidates = [packaged] + ([Path(found)] if found else [])
        for path in candidates:
            if path.is_file() and "AnthropicClaude" not in path.parts:
                return str(path.resolve())
    elif client == "codex":
        path = shutil.which("codex.exe" if os.name == "nt" else "codex")
        if path:
            return str(Path(path).resolve())
    raise FileNotFoundError(f"找不到 {client} 的独立命令行程序")


def environment():
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PSMODULEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES", "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH",
        "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TMP", "TEMP", "TMPDIR",
        "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "TERM",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def check_account(client, binary, cwd):
    args = (
        ["login", "status"] if client == "codex"
        else ["--setting-sources", "", "auth", "status", "--json"]
    )
    checked = subprocess.run(
        [binary, *args], cwd=cwd, env=environment(), capture_output=True,
        encoding="utf-8", errors="replace", timeout=20, check=False,
    )
    if client == "codex":
        valid = checked.returncode == 0 and "Logged in using ChatGPT" in (
            checked.stdout + checked.stderr
        )
        return {"ready": valid, "account": "ChatGPT" if valid else "未核实 ChatGPT 登录"}
    try:
        auth = json.loads(checked.stdout)
    except (ValueError, TypeError):
        auth = {}
    if not isinstance(auth, dict):
        auth = {}
    plan = auth.get("subscriptionType")
    valid = (
        checked.returncode == 0
        and auth.get("loggedIn") is True
        and auth.get("authMethod") == "claude.ai"
        and auth.get("apiProvider") == "firstParty"
        and plan in ("pro", "max", "team", "enterprise")
    )
    return {
        "ready": valid,
        "account": f"Claude {plan}" if valid else "未核实可用的 Claude 订阅登录",
    }


def session_id(value):
    if not isinstance(value, str):
        raise ValueError("会话编号必须为 UUID")
    return str(UUID(value))


def command(client, binary, previous=None):
    if previous is not None:
        previous = session_id(previous)
    if client == "codex":
        args = [binary, "exec"]
        if previous:
            args.append("resume")
        args.extend([
            "--ignore-user-config", "--skip-git-repo-check", "--json",
            "-c", 'model_provider="openai"', "-c", 'sandbox_mode="read-only"',
            "-c", 'approval_policy="never"', "-c", 'model_reasoning_effort="low"',
            "-c", 'web_search="disabled"', "--disable", "shell_tool",
            "-c", "memories.use_memories=false", "-c", "memories.generate_memories=false",
        ])
        if previous:
            args.append(previous)
        return [*args, "-"]
    if client == "claude":
        args = [
            binary, "--print", "--verbose", "--output-format", "stream-json",
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--permission-mode", "plan", "--tools", "", "--disable-slash-commands", "--effort", "low",
        ]
        return [*args, "--resume", previous] if previous else args
    raise ValueError("执行客户端必须是 codex 或 claude")


@dataclass
class Turn:
    session: str | None = None
    result: str | None = None
    usage: dict | None = None
    model: str | None = None
    completed: bool = False
    failed: bool = False


def read_turn(client, stream, requested=None):
    turn = Turn()
    for line in stream:
        # A detached worker might still be writing the last line.
        if not line.endswith(b"\n"):
            continue
        try:
            event = json.loads(line)
        except (UnicodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if client == "codex":
            if kind == "thread.started":
                _record_session(turn, event.get("thread_id"), requested)
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    turn.result = item.get("text")
            elif kind == "turn.completed":
                turn.usage = event.get("usage")
                turn.completed = True
            elif kind == "turn.failed":
                turn.failed = True
        else:
            if kind == "system" and event.get("subtype") == "init":
                _record_session(turn, event.get("session_id"), requested)
                model = event.get("model")
                if isinstance(model, str) and 0 < len(model) <= 128:
                    turn.model = model
            elif kind == "result":
                _record_session(turn, event.get("session_id"), requested)
                turn.failed |= event.get("is_error") is not False or event.get("subtype") != "success"
                turn.result = event.get("result")
                turn.usage = event.get("usage")
                turn.completed = True
    if turn.failed or not turn.completed or not turn.session:
        turn.result = None
    if not isinstance(turn.result, str) or not turn.result.strip():
        turn.result = None
    if not isinstance(turn.usage, dict):
        turn.usage = None
    return turn


def token_counts(client, usage, *, continued=False, previous=None):
    counts = {
        key: usage[key] for key in ("input_tokens", "output_tokens")
        if type(usage.get(key)) is int and 0 <= usage[key] <= 10**12
    }
    if client == "codex" and continued:
        # Codex exec emits the whole session's cumulative counters.
        return {
            key: value - previous[key] for key, value in counts.items()
            if isinstance(previous, dict) and type(previous.get(key)) is int
            and 0 <= previous[key] <= value
        }
    if client == "claude" and "input_tokens" in counts:
        cache = sum(
            n for key, n in usage.items()
            if key in {"cache_read_input_tokens", "cache_creation_input_tokens"}
            and type(n) is int and 0 <= n <= 10**12
        )
        if counts["input_tokens"] + cache <= 10**12:
            counts["input_tokens"] += cache
    return counts


def _record_session(turn, value, requested):
    try:
        observed = session_id(value)
    except ValueError:
        turn.failed = True
        return
    if (requested and observed != requested) or (turn.session and turn.session != observed):
        turn.failed = True
        return
    turn.session = observed
