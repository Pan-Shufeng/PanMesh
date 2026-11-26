"""Project-bound stdio MCP endpoint. No network listener or global settings."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent
READS = {"memory_search", "memory_get", "memory_versions", "memory_source", "memory_proposal",
         "memory_trials", "task_list", "task_get", "task_history", "task_handoff_item", "task_compare",
         "run_routes", "run_status"}


def connection(root, name, secret):
    path = root / "runtime" / "connections.sqlite3"
    if not path.is_file() or not secret:
        raise PermissionError("连接无效或已撤销")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT project, actor, mode, digest FROM connections WHERE name=?", (name,)
        ).fetchone()
    if not row or not hmac.compare_digest(row[3], hashlib.sha256(secret.encode()).hexdigest()):
        raise PermissionError("连接无效或已撤销")
    return row[:3]


def visible(mode):
    modules = ["memory_core"] if mode == "capture" else ["memory_core", "ledger_core", "routing"]
    tools = {}
    for module_name in modules:
        module = importlib.import_module(module_name)
        for name, spec in module.TOOLS.items():
            if mode == "capture" and spec["mode"] != "capture":
                continue
            tools[name] = (module, spec)
    return tools


async def serve(root, name, secret):
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    connection(root, name, secret)
    app = Server("project-memory")

    @app.list_tools()
    async def list_tools():
        _, _, mode = connection(root, name, secret)
        return [types.Tool(name=key, description=spec["description"], inputSchema=spec["inputSchema"],
                           annotations=types.ToolAnnotations(readOnlyHint=key in READS, openWorldHint=key=="run_start"))
                for key, (_, spec) in visible(mode).items()]

    @app.call_tool()
    async def call_tool(name_of_tool, arguments):
        project, actor, mode = connection(root, name, secret)
        entries = visible(mode)
        if name_of_tool not in entries:
            raise PermissionError("此连接无权执行该动作")
        module, _ = entries[name_of_tool]
        bound = {"root": root, "project": project}
        if module.__name__ == "ledger_core":
            bound["actor"] = actor
        if module.__name__ == "routing":
            bound = {"root":root,"connection_name":name}
        result = module.call(name_of_tool, arguments, **bound)
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", required=True)
    args = parser.parse_args()
    try:
        import mcp
    except ImportError:
        python = ROOT / "runtime/python/Scripts/python.exe"
        if python.exists() and Path(sys.executable).resolve() != python.resolve():
            os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
        raise
    try:
        asyncio.run(serve(ROOT, args.connection, os.environ.get("MEMORY_CONNECTION_SECRET", "")))
    except PermissionError as exc:
        sys.exit(str(exc))
