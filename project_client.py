"""Local operator/client bridge using an explicitly selected issued connection."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent


def descriptor(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("无效的连接名")
    return json.loads((ROOT / "runtime/credentials" / (name + ".json")).read_text(encoding="utf-8"))


async def run(desc, tool, args_file):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async with stdio_client(StdioServerParameters(**desc)) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            if tool is None:
                print(json.dumps([entry.name for entry in (await session.list_tools()).tools], ensure_ascii=False))
                return 0
            arguments = json.loads(args_file.read_text(encoding="utf-8-sig")) if args_file else {}
            result = await session.call_tool(tool, arguments)
            for part in result.content:
                if getattr(part, "type", None) == "text":
                    print(part.text)
            return int(bool(result.isError))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("serve", "list", "call"))
    parser.add_argument("connection")
    parser.add_argument("tool", nargs="?")
    parser.add_argument("--args-file", type=Path)
    args = parser.parse_args()
    if args.action == "call" and not args.tool:
        parser.error("call 需要工具名称")
    desc = descriptor(args.connection)
    if args.action == "serve":
        os.environ.update(desc["env"])
        from memory_service import serve
        asyncio.run(serve(ROOT, args.connection, os.environ["MEMORY_CONNECTION_SECRET"]))
        return 0
    return asyncio.run(run(desc, args.tool, args.args_file))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (OSError, ValueError, PermissionError) as exc:
        sys.exit(str(exc))
