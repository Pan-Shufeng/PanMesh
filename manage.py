"""Local operator commands for enrollment, revocation and safe data recovery."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
DATA_DIRS = ("imports", "vault", "runtime")
EXCLUDE = {"runtime/python", "runtime/credentials", "runtime/connections.sqlite3",
           "runtime/connector-tools", "runtime/connector-smoke", "runtime/acceptance"}


def registry(root):
    folder = root / "runtime"
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "connections.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS connections(name TEXT PRIMARY KEY, project TEXT NOT NULL,"
               "actor TEXT NOT NULL, mode TEXT NOT NULL, digest TEXT NOT NULL)")
    return db


def enroll(root, name, project, actor, mode):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("连接名须为小写英文、数字、下划线或短横线")
    if mode not in ("collab", "capture") or not project.strip() or not actor.strip():
        raise ValueError("请输入有效的项目、角色和模式")
    secret = secrets.token_urlsafe(32)
    folder = root / "runtime/credentials"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + ".json")
    with closing(registry(root)) as db, db:
        db.execute("INSERT INTO connections VALUES(?,?,?,?,?)",
                   (name, project, actor, mode, hashlib.sha256(secret.encode()).hexdigest()))
        with tempfile.NamedTemporaryFile(dir=folder,mode="w",encoding="utf-8",delete=False) as stream:
            staged = Path(stream.name)
            json.dump({"command": str(root / "runtime/python/Scripts/python.exe"),
                       "args": [str(root / "memory_service.py"), "--connection", name],
                       "env": {"MEMORY_CONNECTION_SECRET": secret}}, stream, indent=2, ensure_ascii=False)
        try:
            os.replace(staged,path)
        finally:
            staged.unlink(missing_ok=True)
    return path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def link_project(root, connection_name, client, workspace):
    """Install a registered connection only in the chosen client's project file."""
    import tomllib
    from connectors.coordinator import authenticate

    authenticate(root, connection_name)
    folder = Path(workspace).resolve(strict=True)
    if not folder.is_dir() or folder in (Path(folder.anchor), Path.home().resolve()):
        raise ValueError("请选择具体项目文件夹")
    locations = {"codex": ".codex/config.toml", "claude": ".mcp.json", "cursor": ".cursor/mcp.json"}
    config = folder / locations[client]
    if config.resolve() != config:
        raise ValueError("项目配置链接到了其他路径，请在实际项目中接入")
    config.parent.mkdir(parents=True, exist_ok=True)
    original = config.read_bytes() if config.exists() else None
    content = (original or b"").decode("utf-8-sig")
    python = (root / "runtime/python/Scripts/python.exe").as_posix()
    args = [(root / "project_client.py").as_posix(), "serve", connection_name]
    wanted = {"command": python, "args": args}
    if client == "codex":
        previous = tomllib.loads(content).get("mcp_servers", {})
        output = (content.rstrip() + "\n\n" if content.strip() else "") + (
            "[mcp_servers.e_drive_memory]\ncommand = " + json.dumps(python, ensure_ascii=False)
            + "\nargs = " + json.dumps(args, ensure_ascii=False) + "\n")
    else:
        if client == "claude":
            wanted["type"] = "stdio"
        settings = json.loads(content) if content.strip() else {}
        previous = settings.setdefault("mcpServers", {})
    if "e_drive_memory" in previous:
        old = previous["e_drive_memory"]
        if (Path(old.get("command", "")) == Path(python) and
                len(old.get("args", [])) == len(args) and Path(old["args"][0]) == Path(args[0]) and
                old["args"][1:] == args[1:]):
            return config
        raise ValueError("这个项目已有其他共享记忆连接，请先核对项目身份")
    if client == "codex":
        tomllib.loads(output)
    else:
        previous["e_drive_memory"] = wanted
        output = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(dir=config.parent, delete=False) as stream:
        staged = Path(stream.name)
        stream.write(output.encode("utf-8"))
    try:
        if (config.read_bytes() if config.exists() else None) != original:
            raise ValueError("项目配置已被其他程序修改，请重新读取")
        os.replace(staged, config)
    finally:
        staged.unlink(missing_ok=True)
    return config


def import_file(root, connection_name, path, copy_file=False):
    """Only a local operator can select an arbitrary file/directory on disk."""
    from connectors.coordinator import authenticate
    import memory_core
    project, _, _ = authenticate(root, connection_name)
    source = path.resolve(strict=True)
    location = source
    selection = source.name
    if copy_file:
        if not source.is_file():
            raise ValueError("文件夹只建立位置索引")
        project_key = hashlib.sha256(project.encode()).hexdigest()[:20]
        folder = root / "imports" / "材料" / project_key
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as spool:
            staged = Path(spool.name)
        try:
            shutil.copyfile(source, staged)
            checksum = digest(staged)
            location = folder / checksum / source.name
            location.parent.mkdir(exist_ok=True)
            if location.exists() and digest(location) != checksum:
                raise ValueError("已保存材料的校验值发生变化")
            if not location.exists():
                os.replace(staged, location)
            selection += " · SHA-256 " + checksum
        finally:
            staged.unlink(missing_ok=True)
    return memory_core.call("memory_capture", dict(client="local-file", source_key=source.as_uri(),
                            kind="material", scope="reference", selection=selection,
                            locator=location.as_uri()), root=root, project=project)


def entries(root, folder):
    for path in sorted(folder.iterdir()):
        relative = path.relative_to(root).as_posix()
        if relative in EXCLUDE or path.name in {"__pycache__", ".trash"}:
            continue
        if getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or path.is_symlink():
            raise ValueError(f"请单独处理链接目标：{relative}")
        if path.is_dir():
            yield from entries(root, path)
        else:
            yield path


def inventory(root):
    items = {}
    for top in DATA_DIRS:
        folder = root / top
        if folder.is_dir():
            for path in entries(root, folder):
                meta = path.stat()
                items[path] = (meta.st_size, meta.st_mtime_ns)
    return items


def backup(root):
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    final = root / "backups" / name
    staging = final.with_suffix(".partial")
    staging.mkdir(parents=True)
    manifest = {}
    initial = inventory(root)
    databases = set()
    for src in initial:
        with src.open("rb") as stream:
            if stream.read(16) == b"SQLite format 3\0":
                databases.add(src)
    sidecars = {Path(str(path) + suffix) for path in databases for suffix in ("-wal", "-shm", "-journal")}
    for src in initial:
        if src not in sidecars:
            rel = src.relative_to(root)
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src in databases:
                with closing(sqlite3.connect(src.as_uri()+"?mode=ro", uri=True)) as source, closing(sqlite3.connect(dst)) as target:
                    source.backup(target)
            else:
                before = src.stat()
                shutil.copy2(src, dst)
                if before.st_mtime_ns != src.stat().st_mtime_ns or digest(src) != digest(dst):
                    raise RuntimeError(f"备份期间文件被编辑：{rel}")
            manifest[rel.as_posix()] = digest(dst)
    # SQLite's own read connection can touch the shared-memory index. Committed
    # writes change the database or WAL; both participate in this comparison.
    volatile = {Path(str(path)+"-shm") for path in databases}
    def stable(items):
        return {p: state for p, state in items.items() if p not in volatile}
    if stable(initial) != stable(inventory(root)):
        raise RuntimeError("备份期间库发生变化，请在写入完成后重试。半成品不会成为正式快照。")
    (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (staging / "snapshot-info.json").write_text(json.dumps({"source_root":root.resolve().as_uri()}),encoding="utf-8")
    staging.rename(final)
    return final


def restore(snapshot, target):
    if target.exists() or snapshot.name.endswith(".partial"):
        raise ValueError("请选择完整备份和一个尚不存在的恢复目录")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    staging = target.with_name(target.name + "." + uuid4().hex + ".partial")
    staging.mkdir(parents=True)
    for folder in DATA_DIRS:
        (staging / folder).mkdir()
    for key, expected in manifest.items():
        rel = Path(key)
        if rel.is_absolute() or rel.drive or ".." in rel.parts or rel.parts[0] not in DATA_DIRS:
            raise ValueError("备份内路径无效")
        src = snapshot / rel
        if not src.resolve().is_relative_to(snapshot.resolve()):
            raise ValueError("文件不在备份目录")
        dst = staging / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if digest(dst) != expected:
            raise ValueError(f"文件校验失败：{rel}")
    info = snapshot / "snapshot-info.json"
    if info.exists():
        old = json.loads(info.read_text(encoding="utf-8"))["source_root"].rstrip("/") + "/imports/"
        new = target.resolve().as_uri().rstrip("/") + "/imports/"
        # Only service-generated source indexes contain relocatable local links.
        # Transcripts, quoted user messages and external project paths stay literal.
        for page in (staging / "vault/来源").glob("capture-*.md"):
            if re.fullmatch(r"capture-[0-9a-f]{64}\.md", page.name):
                data = page.read_bytes()
                for marker in ("](", "](<"):
                    data = data.replace((marker+old).encode(), (marker+new).encode())
                page.write_bytes(data)
    staging.rename(target)
    return target


def main():
    p = argparse.ArgumentParser()
    commands = p.add_subparsers(dest="command", required=True)
    add = commands.add_parser("enroll")
    for key in ("name", "project", "actor"):
        add.add_argument(key)
    add.add_argument("mode", choices=("collab", "capture"))
    commands.add_parser("backup")
    link = commands.add_parser("link-project")
    link.add_argument("connection")
    link.add_argument("client", choices=("codex", "claude", "cursor"))
    link.add_argument("workspace", type=Path)
    route = commands.add_parser("route")
    for key in ("owner","name","executor","client","workspace"):
        route.add_argument(key)
    route.add_argument("--channel", choices=("push","pull"))
    file = commands.add_parser("import-file")
    file.add_argument("connection")
    file.add_argument("path", type=Path)
    file.add_argument("--copy", action="store_true")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("name")
    rec = commands.add_parser("restore")
    rec.add_argument("snapshot", type=Path)
    rec.add_argument("target", type=Path)
    args = p.parse_args()
    if args.command == "enroll":
        print(enroll(ROOT, args.name, args.project, args.actor, args.mode))
    elif args.command == "revoke":
        with closing(registry(ROOT)) as db, db:
            changed = db.execute("DELETE FROM connections WHERE name=?", (args.name,)).rowcount
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='routes'").fetchone():
                db.execute("DELETE FROM routes WHERE owner=? OR executor=?",(args.name,args.name))
            if changed and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}",args.name):
                (ROOT/"runtime/credentials"/(args.name+".json")).unlink(missing_ok=True)
        print(f"已撤销 {changed} 个连接")
    elif args.command == "backup":
        print(backup(ROOT))
    elif args.command == "link-project":
        print(link_project(ROOT,args.connection,args.client,args.workspace))
    elif args.command == "route":
        import routing
        routing.register(ROOT,args.owner,args.name,args.executor,args.client,args.workspace,args.channel)
        print("已登记项目派工路径")
    elif args.command == "import-file":
        print(json.dumps(import_file(ROOT, args.connection, args.path, args.copy), ensure_ascii=False))
    else:
        print(restore(args.snapshot.resolve(), args.target.resolve()))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        sys.exit(str(exc))
