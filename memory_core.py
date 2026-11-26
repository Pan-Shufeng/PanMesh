"""Project-scoped memory tools. Run this file for trusted local approvals and grants.

The MCP adapter supplies the authenticated project and enforces TOOLS[name].mode.
The local management interface is deliberately separate from the MCP tool registry.
"""

import argparse
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from urllib.parse import quote
import uuid


class MemoryAccessError(ValueError):
    """Invalid input, unavailable record, or a version conflict."""


class _CurrentMarkdownMissing(MemoryAccessError):
    pass


def _string(description=""):
    return {"type": "string", "description": description}


def _enum(*values):
    return {"type": "string", "enum": list(values)}


def _object(properties, *required):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def _array(items):
    return {"type": "array", "items": items}


def _integer(minimum=1, maximum=None):
    schema = {"type": "integer", "minimum": minimum}
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _tool(description, schema, mode="collab"):
    return {"description": description, "inputSchema": schema, "mode": mode}


_NOTE_FOLDERS = {"case": "案例", "playbook": "做法", "skill": "技能"}
_WIKI_TARGET = re.compile(r"\[\[(?:[^\[\]|#]*/)?((?:turn|capture|source)-[0-9a-f]{64}|note-[0-9a-f]{32})"
                          r"(?:\.md)?(?:[|#][^\[\]]*)?\]\]")
_REFERENCE = _object({"id": _string(), "version": _integer()}, "id")
_TURN = _object({
    "turn_id": _string("Stable identifier from the source"),
    "ordinal": _integer(0), "role": _string(), "text": _string(),
    "locator": _string("Original turn permalink or location, if available"),
    "char_start": _integer(0, 9007199254740991),
}, "turn_id", "ordinal", "role", "text")

TOOLS = {
    "memory_capture": _tool("Append an authorized selection and return its receipt.", _object({
        "client": _string(), "source_key": _string("Stable conversation or material identifier"),
        "kind": _enum("conversation", "material"), "scope": _enum("complete", "excerpt", "reference"),
        "selection": _string("Excerpt boundaries or material location"), "locator": _string(),
        "gaps": _array(_string("Missing visible turns, files, or attachments")),
        "turns": _array(_TURN), "text": _string("Selected material text"),
    }, "client", "source_key", "kind", "scope"), "capture"),
    "memory_search": _tool("Search accessible current note titles and summaries.", _object({
        "query": _string(), "kind": _enum(*_NOTE_FOLDERS), "limit": _integer(1, 100),
    }, "query")),
    "memory_get": _tool("Read an accessible current or pinned note. Body access is explicit.", _object({
        "note_id": _string(), "version": _integer(), "view": _enum("summary", "body"),
    }, "note_id")),
    "memory_versions": _tool("List accessible revisions of an approved note.", _object({
        "note_id": _string(),
    }, "note_id")),
    "memory_source": _tool("Read an authorized source, selection, or turn by stable ID.", _object({
        "source_id": _string(), "turn_ref": _string(), "capture_id": _string(),
    })),
    "memory_propose": _tool("Submit a draft for local review. The note owner receives its content.", _object({
        "note_id": _string(), "base_version": _integer(), "kind": _enum(*_NOTE_FOLDERS),
        "title": _string(), "summary": _string("Shareable summary kept separate from the body"),
        "body": _string("Markdown; paste stable Obsidian links where useful"), "reason": _string(),
        "references": _array(_REFERENCE),
    }, "title", "summary", "body", "reason")),
    "memory_proposal": _tool("Read a proposal you submitted or one against a note you own.", _object({
        "proposal_id": _string(),
    }, "proposal_id")),
    "memory_record_trial": _tool("Record a trial against an exact approved version you can read.", _object({
        "note_id": _string(), "version": _integer(), "task_id": _string(),
        "outcome": _enum("effective", "partial", "ineffective", "inconclusive"),
        "details": _string(), "case_id": _string(), "case_version": _integer(),
    }, "note_id", "version", "task_id", "outcome", "details")),
    "memory_trials": _tool("List your project's trials of a note you can read.", _object({
        "note_id": _string(), "version": _integer(),
    }, "note_id")),
}

_MANAGEMENT = {
    "pending": _object({}),
    "approve": _object({"proposal_id": _string(), "expected_current": _integer(0)},
                       "proposal_id", "expected_current"),
    "reject": _object({"proposal_id": _string()}, "proposal_id"),
    "grant": _object({"id": _string(), "to_project": _string(),
                      "scope": _enum("summary", "body", "raw")}, "id", "to_project", "scope"),
    "revoke": _object({"id": _string(), "to_project": _string(),
                       "scope": _enum("summary", "body", "raw", "all")}, "id", "to_project", "scope"),
    "dependencies": _object({"source_id": _string()}, "source_id"),
    "withdraw-source": _object({"source_id": _string()}, "source_id"),
    "restore-source": _object({"source_id": _string()}, "source_id"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_withdrawals (
    source_id TEXT PRIMARY KEY REFERENCES sources(id), withdrawn_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    payload TEXT NOT NULL, created TEXT NOT NULL, projected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turns (
    ref TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    capture_id TEXT NOT NULL REFERENCES captures(id), turn_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL, role TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_source ON turns(source_id, ordinal);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0, pending_base INTEGER
);
CREATE TABLE IF NOT EXISTS versions (
    note_id TEXT NOT NULL REFERENCES notes(id), version INTEGER NOT NULL,
    parent_version INTEGER NOT NULL, title TEXT NOT NULL, markdown TEXT NOT NULL,
    reason TEXT NOT NULL, refs TEXT NOT NULL, author TEXT NOT NULL, created TEXT NOT NULL,
    PRIMARY KEY (note_id, version)
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(id),
    author TEXT NOT NULL, base_version INTEGER NOT NULL, title TEXT NOT NULL,
    markdown TEXT NOT NULL, reason TEXT NOT NULL, refs TEXT NOT NULL, created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', approved_version INTEGER
);
CREATE TABLE IF NOT EXISTS grants (
    id TEXT NOT NULL, project TEXT NOT NULL, scope TEXT NOT NULL,
    PRIMARY KEY (id, project, scope)
);
CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, note_id TEXT NOT NULL,
    version INTEGER NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL,
    FOREIGN KEY (note_id, version) REFERENCES versions(note_id, version)
);
CREATE INDEX IF NOT EXISTS trials_by_project ON trials(project, note_id, version);
"""


def _validate(value, schema, label):
    kind = schema["type"]
    if kind == "object" and isinstance(value, dict):
        properties = schema["properties"]
        if properties.keys() >= value.keys() >= set(schema["required"]):
            for key, child in value.items():
                _validate(child, properties[key], f"{label}.{key}")
            return
    elif kind == "array" and isinstance(value, list):
        for child in value:
            _validate(child, schema["items"], f"{label} item")
        return
    elif kind == "string" and isinstance(value, str):
        if value in schema.get("enum", [value]) and "\x00" not in value:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise MemoryAccessError(f"Invalid {label}: expected UTF-8 text") from error
            return
    elif kind == "integer" and type(value) is int:
        if schema["minimum"] <= value <= schema.get("maximum", 2**63 - 1):
            return
    raise MemoryAccessError(f"Invalid {label}: expected {kind} matching the tool schema")


def _required_text(value, label):
    if not value.strip():
        raise MemoryAccessError(f"{label} is required")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(prefix, *parts):
    return prefix + hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content.encode("utf-8"))
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _literal(text):
    fence = "`" * max(3, 1 + max(map(len, re.findall(r"`+", text)), default=0))
    return f"{fence}\n{text}\n{fence}\n"


def _markdown(title, summary, body):
    return f"---\ntitle: {_json(title)}\nsummary: {_json(summary)}\n---\n\n{body}"


def _property(markdown, key):
    lines = markdown.splitlines()
    prefix = key + ":"
    if lines and lines[0] == "---":
        for index, line in enumerate(lines[1:], 1):
            if line == "---":
                break
            if line.startswith(prefix):
                value = line.removeprefix(prefix).strip()
                if value in ("|", "|-", "|+", ">", ">-", ">+"):
                    block = []
                    for continuation in lines[index + 1:]:
                        if continuation and not continuation.startswith(" "):
                            break
                        block.append(continuation.strip())
                    separator = "\n" if value.startswith("|") else " "
                    return separator.join(block).rstrip()
                if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
                    return value[1:-1].replace("''", "'")
                try:
                    decoded = json.loads(value)
                except ValueError:
                    return value
                return decoded if isinstance(decoded, str) else value
    return ""


class _Store:
    def __init__(self, root, project, db):
        self.root = root
        self.project = project
        self.db = db

    def path(self, *parts):
        path = self.root
        for part in parts:
            path /= part
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise MemoryAccessError("Managed memory paths must stay in their configured locations")
        return path

    def get_scope(self, item, scope):
        if scope == "raw" and self.withdrawn(item["id"]):
            return False
        if item["owner"] == self.project:
            return True
        scopes = ("summary", "body") if scope == "summary" else (scope, scope)
        return self.db.execute(
            "SELECT 1 FROM grants WHERE id = ? AND project = ? AND scope IN (?, ?)",
            (item["id"], self.project, *scopes),
        ).fetchone() is not None

    def withdrawn(self, source_id):
        return self.db.execute("SELECT 1 FROM source_withdrawals WHERE source_id = ?",
                               (source_id,)).fetchone() is not None

    def note(self, note_id, scope="summary", *, pinned=False):
        note = self.db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if note is None or not note["current_version"] or not self.get_scope(note, scope):
            raise MemoryAccessError("Note or requested view unavailable")
        try:
            return self.sync(note)
        except _CurrentMarkdownMissing:
            if pinned:
                return note
            raise

    def version(self, note_id, version):
        row = self.db.execute("SELECT * FROM versions WHERE note_id = ? AND version = ?",
                              (note_id, version)).fetchone()
        if row is None:
            raise MemoryAccessError("Note version unavailable")
        return row

    def note_location(self, note):
        return f"{_NOTE_FOLDERS[note['kind']]}/{note['id']}.md"

    def add_version(self, note, parent, title, markdown, reason, refs, author, *, publish):
        number = note["current_version"] + 1
        self.db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (note["id"], number, parent, title, markdown, reason, refs, author, _now()))
        self.db.execute("UPDATE notes SET current_version = ?, pending_base = ? WHERE id = ?",
                        (number, note["current_version"] if publish else None, note["id"]))
        return number

    def sync(self, note):
        location = self.path("vault", *self.note_location(note).split("/"))
        current = self.version(note["id"], note["current_version"])
        base = note["pending_base"]
        try:
            actual = location.read_bytes().decode("utf-8-sig")
        except FileNotFoundError:
            if base is None:
                raise _CurrentMarkdownMissing("Current Markdown is missing. Restore it to its registered path.")
            actual = None
        if actual == current["markdown"]:
            if base is not None:
                self.db.execute("UPDATE notes SET pending_base = NULL WHERE id = ?", (note["id"],))
            return note

        previous = self.version(note["id"], base) if base else None
        if base is not None and (actual is None or (previous and actual == previous["markdown"])):
            _atomic_text(location, current["markdown"])
            self.db.execute("UPDATE notes SET pending_base = NULL WHERE id = ?", (note["id"],))
            return note

        # If a local edit raced with publication, keep both versions and prefer the live file.
        edited = previous or current
        title = _property(actual, "title") or edited["title"]
        self.add_version(note, edited["version"], title, actual,
                         "Local Markdown edit", edited["refs"], note["owner"], publish=False)
        return self.db.execute("SELECT * FROM notes WHERE id = ?", (note["id"],)).fetchone()

    def source(self, source_id):
        row = self.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None or not self.get_scope(row, "raw"):
            raise MemoryAccessError("Source unavailable")
        return row

    def reference(self, reference):
        identifier = reference["id"]
        if identifier.startswith("note-"):
            note = self.note(identifier, pinned="version" in reference)
            return {"id": identifier, "version": self.version(
                identifier, reference.get("version", note["current_version"]))["version"]}
        if "version" in reference:
            raise MemoryAccessError("Only note references take a version")
        if identifier.startswith("turn-"):
            turn = self.db.execute("SELECT source_id FROM turns WHERE ref = ?", (identifier,)).fetchone()
            if turn is None:
                raise MemoryAccessError("Source unavailable")
            self.source(turn["source_id"])
        elif identifier.startswith("capture-"):
            capture = self.db.execute("SELECT source_id FROM captures WHERE id = ?", (identifier,)).fetchone()
            if capture is None:
                raise MemoryAccessError("Source unavailable")
            self.source(capture["source_id"])
        else:
            self.source(identifier)
        return {"id": identifier}

    def display_refs(self, refs):
        visible = []
        for reference in refs:
            try:
                self.reference(reference)
            except MemoryAccessError:
                continue
            visible.append(reference)
        return visible


@contextmanager
def _store(root, project):
    _validate(project, _string(), "project")
    _required_text(project, "project")
    root = Path(root).resolve()
    path = _Store(root, project, None).path("runtime", "memory.sqlite3")
    for suffix in ("-journal", "-wal", "-shm"):
        _Store(root, project, None).path("runtime", path.name + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        with db:
            yield _Store(root, project, db)
    finally:
        db.close()


def _capture_path(capture_id):
    return f"来源/{capture_id}.md"


def _wiki(path, block=""):
    return "[[" + path.removesuffix(".md") + block + "]]"


def _links(store, path, block=""):
    absolute = (store.root / "vault" / path).as_posix() + block
    return {"link": _wiki(path, block), "obsidian_uri": "obsidian://open?path=" + quote(absolute, safe="")}


def _raw_path(store, source_id, filename):
    return store.path("imports", source_id, filename)


def _capture_links(store, source_id, capture_id, has_text):
    extension = ".txt" if has_text else ".json"
    return dict(_links(store, _capture_path(capture_id)),
                raw_uri=_raw_path(store, source_id, capture_id + extension).as_uri())


def _turn_links(store, source_id, capture_id, ref):
    return dict(_links(store, _capture_path(capture_id), "#^" + ref),
                raw_uri=_raw_path(store, source_id, ref + ".txt").as_uri())


def _turn_ref(source_id, turn):
    start = turn.get("char_start")
    end = None if start is None else start + len(turn["text"])
    return _identity("turn-", source_id, turn["turn_id"], start, end)


def _overlap_agrees(first, second):
    left = first.get("char_start", 0)
    right = second.get("char_start", 0)
    start = max(left, right)
    end = min(left + len(first["text"]), right + len(second["text"]))
    if "char_start" not in first and right + len(second["text"]) > len(first["text"]):
        return False
    if "char_start" not in second and left + len(first["text"]) > len(second["text"]):
        return False
    return end <= start or first["text"][start-left:end-left] == second["text"][start-right:end-right]


def _capture(store, args):
    kind = args["kind"]
    scope = args["scope"]
    client = _required_text(args["client"], "client")
    source_key = _required_text(args["source_key"], "source_key")
    turns = args.get("turns", [])
    if scope == "excerpt":
        _required_text(args.get("selection", ""), "Excerpt selection")
    if scope == "reference":
        _required_text(args.get("locator", ""), "Reference locator")
        if turns or "text" in args:
            raise MemoryAccessError("A reference receipt only accepts location metadata")
    elif kind == "conversation":
        if not turns or "text" in args:
            raise MemoryAccessError("Conversation capture requires visible turns")
    elif "text" not in args or turns:
        raise MemoryAccessError("Material capture requires selected text")

    source_id = _identity("source-", store.project, client, source_key)
    turns = sorted(turns, key=lambda turn: (turn["ordinal"], turn["turn_id"], turn.get("char_start", -1),
                                           len(turn["text"])))
    unique_turns = {}
    for turn in turns:
        _required_text(turn["turn_id"], "turn_id")
        _required_text(turn["role"], "role")
        if "char_start" in turn and (scope != "excerpt" or not turn["text"]):
            raise MemoryAccessError("Character offsets require a nonempty excerpt")
        ref = _turn_ref(source_id, turn)
        if ref in unique_turns and unique_turns[ref] != turn:
            raise MemoryAccessError("Inconsistent turn in submitted capture")
        unique_turns[ref] = turn

    payload = dict(args, turns=list(unique_turns.values()))
    capture_id = _identity("capture-", source_id, payload)
    source = store.db.execute("SELECT kind FROM sources WHERE id = ?", (source_id,)).fetchone()
    if source and store.withdrawn(source_id):
        raise MemoryAccessError("Source unavailable for new capture")
    if source and source["kind"] != kind:
        raise MemoryAccessError("Capture conflicts with the registered source kind")
    duplicate = store.db.execute("SELECT 1 FROM captures WHERE id = ?", (capture_id,)).fetchone() is not None
    if not duplicate:
        store.db.execute("INSERT OR IGNORE INTO sources VALUES (?, ?, ?)", (source_id, store.project, kind))
        store.db.execute("INSERT INTO captures (id, source_id, payload, created) VALUES (?, ?, ?, ?)",
                         (capture_id, source_id, _json(payload), _now()))
        _register_turns(store, source_id, capture_id, unique_turns)
    turn_links = []
    for ref, turn in unique_turns.items():
        first = store.db.execute("SELECT capture_id FROM turns WHERE ref = ?", (ref,)).fetchone()
        turn_links.append(dict(turn_id=turn["turn_id"], turn_ref=ref,
                               **_turn_links(store, source_id, first["capture_id"], ref)))
    return dict(source_id=source_id, capture_id=capture_id, duplicate=duplicate, turns=turn_links,
                **_capture_links(store, source_id, capture_id, "text" in payload))


def _register_turns(store, source_id, capture_id, turns):
    for ref, turn in turns.items():
        prior = store.db.execute(
            "SELECT * FROM turns WHERE source_id = ? AND (ordinal = ? OR turn_id = ?)",
            (source_id, turn["ordinal"], turn["turn_id"]),
        ).fetchall()
        for row in prior:
            original = json.loads(row["payload"])
            same_identity = all(row[key] == turn[key] for key in ("ordinal", "turn_id", "role"))
            if not same_identity or not _overlap_agrees(original, turn):
                raise MemoryAccessError("Capture conflicts with an existing turn identity or text")
        if any(row["ref"] == ref for row in prior):
            continue
        store.db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (ref, source_id, capture_id, turn["turn_id"], turn["ordinal"], turn["role"], _json(turn)))


def _publish_capture(store, capture_id):
    row = store.db.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if row["projected"]:
        return
    payload = json.loads(row["payload"])
    source_id = row["source_id"]
    header = {key: value for key, value in payload.items() if key not in ("turns", "text")}
    manifest = dict(header, turns=[])
    manifest_path = _raw_path(store, source_id, capture_id + ".json")
    lines = ["# 来源采集", "", f"[打开采集记录]({manifest_path.as_uri()})", "",
             _literal(json.dumps(header, ensure_ascii=False, indent=2)).rstrip()]
    # Local file imports come from the operator's file picker/CLI. Generate a
    # clickable reference for ordinary files and existing handover directories.
    location = payload.get("locator", "")
    if payload.get("kind") == "material" and payload.get("scope") == "reference" and location.startswith("file:///"):
        lines.extend(["", f"[打开选定材料](<{quote(location, safe=':/%')}>)"])
    first = store.db.execute("SELECT id FROM captures WHERE source_id = ? ORDER BY rowid LIMIT 1",
                             (source_id,)).fetchone()["id"]
    if first != capture_id:
        lines.extend(["", f"同一来源的首次采集 · {_wiki(_capture_path(first))}"])
    if "text" in payload:
        text_path = _raw_path(store, source_id, capture_id + ".txt")
        _immutable_text(text_path, payload["text"])
        manifest["text_file"] = text_path.name
        lines.extend(["", f"[打开原文]({text_path.as_uri()})"])
    if payload["turns"]:
        lines.extend(["", "## 对话轮次", ""])
    roles = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}
    for turn in payload["turns"]:
        ref = _turn_ref(source_id, turn)
        text_path = _raw_path(store, source_id, ref + ".txt")
        _immutable_text(text_path, turn["text"])
        metadata = {key: value for key, value in turn.items() if key != "text"}
        manifest["turns"].append(dict(metadata, turn_ref=ref, text_file=text_path.name))
        label = f"第 {turn['ordinal']} 轮 · {roles.get(turn['role'], '其他角色')}"
        if "char_start" in turn:
            label += f" · 从字符位置 {turn['char_start']} 起"
        lines.append(f"- {label} · [打开原话]({text_path.as_uri()}) ^{ref}")
    _immutable_text(manifest_path, _json(manifest) + "\n")
    _immutable_text(store.path("vault", "来源", f"{capture_id}.md"), "\n".join(lines) + "\n")
    store.db.execute("UPDATE captures SET projected = 1 WHERE id = ?", (capture_id,))


def _immutable_text(path, value):
    try:
        actual = path.read_bytes()
    except FileNotFoundError:
        _atomic_text(path, value)
    else:
        if actual != value.encode("utf-8"):
            raise MemoryAccessError("Existing source file differs from the capture. Restore it before retrying.")


def _material_location(store, owner, payload):
    """A copied file moves with the vault while its capture keeps the original locator."""
    locator = payload.get("locator", "")
    if payload.get("kind") != "material" or payload.get("scope") != "reference":
        return {}
    if payload.get("client") == "local-file":
        name, marker, digest = payload.get("selection", "").rpartition(" · SHA-256 ")
        if marker and name and Path(name).name == name and re.fullmatch("[a-f0-9]{64}", digest):
            folder = hashlib.sha256(owner.encode()).hexdigest()[:20]
            copy = store.path("imports", "材料", folder, digest, name)
            if copy.is_file():
                return {"material_uri": copy.as_uri()}
    return {"material_uri": locator}


def _source(store, args):
    source_id = args.get("source_id")
    turn_ref = args.get("turn_ref")
    capture_id = args.get("capture_id")
    if turn_ref is not None and capture_id is not None:
        raise MemoryAccessError("Specify either capture_id or turn_ref")
    if source_id is None:
        if turn_ref is not None:
            match = store.db.execute("SELECT source_id FROM turns WHERE ref = ?", (turn_ref,)).fetchone()
        elif capture_id is not None:
            match = store.db.execute("SELECT source_id FROM captures WHERE id = ?", (capture_id,)).fetchone()
        else:
            raise MemoryAccessError("A source_id, capture_id, or turn_ref is required")
        if match is None:
            raise MemoryAccessError("Source unavailable")
        source_id = match["source_id"]
    owner = store.source(source_id)["owner"]
    _publish_pending_captures(store, source_id)
    if capture_id is not None:
        row = store.db.execute("SELECT * FROM captures WHERE source_id = ? AND id = ?",
                               (source_id, capture_id)).fetchone()
        if row is None:
            raise MemoryAccessError("Capture unavailable")
        payload = json.loads(row["payload"])
        return {"source_id": source_id, "capture_id": row["id"], "captured_at": row["created"],
                "selection": payload, **_material_location(store, owner, payload),
                **_capture_links(store, source_id, row["id"], "text" in payload)}
    if turn_ref is not None:
        row = store.db.execute("SELECT * FROM turns WHERE source_id = ? AND ref = ?",
                               (source_id, turn_ref)).fetchone()
        if row is None:
            raise MemoryAccessError("Turn unavailable")
        return {"source_id": source_id, "turn": _turn_result(store, row)}
    turns = store.db.execute("SELECT * FROM turns WHERE source_id = ? ORDER BY ordinal, ref",
                             (source_id,)).fetchall()
    ordered = sorted((_turn_result(store, row) for row in turns),
                     key=lambda turn: (turn["ordinal"], turn.get("char_start", -1)))
    captures = []
    for row in store.db.execute("SELECT * FROM captures WHERE source_id = ? ORDER BY created", (source_id,)):
        payload = json.loads(row["payload"])
        turn_refs = [_turn_ref(source_id, turn) for turn in payload.pop("turns")]
        captures.append(dict(payload, capture_id=row["id"], turn_refs=turn_refs, captured_at=row["created"],
                             **_material_location(store, owner, payload),
                             **_capture_links(store, source_id, row["id"], "text" in payload)))
    return {"source_id": source_id, "turns": ordered, "captures": captures}


def _turn_result(store, row):
    return dict(json.loads(row["payload"]), turn_ref=row["ref"], capture_id=row["capture_id"],
                **_turn_links(store, row["source_id"], row["capture_id"], row["ref"]))


def _note_result(store, note, version):
    return {"note_id": note["id"], "kind": note["kind"], "title": version["title"],
            "version": version["version"], "current_version": note["current_version"],
            "summary": _property(version["markdown"], "summary")}


def _get(store, args):
    scope = args.get("view", "summary")
    note = store.note(args["note_id"], scope, pinned="version" in args)
    version = store.version(note["id"], args.get("version", note["current_version"]))
    result = _note_result(store, note, version)
    if scope == "body":
        result.update(markdown=version["markdown"], **_links(store, store.note_location(note)),
                      link_version=note["current_version"], reason=version["reason"],
                      references=store.display_refs(json.loads(version["refs"])))
    return result


def _search(store, args):
    words = args["query"].casefold().split()
    results = []
    rows = store.db.execute("""
        SELECT * FROM notes WHERE current_version > 0 AND (? IS NULL OR kind = ?)
        AND (owner = ? OR EXISTS (SELECT 1 FROM grants WHERE grants.id = notes.id
             AND project = ? AND scope IN ('summary', 'body'))) ORDER BY id
    """, (args.get("kind"), args.get("kind"), store.project, store.project)).fetchall()
    unavailable = []
    for note in rows:
        try:
            note = store.sync(note)
        except (MemoryAccessError, UnicodeError):
            unavailable.append(note["id"])
            continue
        result = _note_result(store, note, store.version(note["id"], note["current_version"]))
        haystack = f"{result['title']}\n{result['summary']}".casefold()
        if all(word in haystack for word in words):
            results.append(result)
    results.sort(key=lambda item: (item["title"].casefold(), item["note_id"]))
    return {"results": results[:args.get("limit", 20)], "unavailable_note_ids": unavailable}


def _versions(store, args):
    note = store.note(args["note_id"], pinned=True)
    rows = store.db.execute("""
        SELECT version, parent_version, created FROM versions WHERE note_id = ? ORDER BY version DESC
    """, (note["id"],))
    return {"note_id": note["id"], "current_version": note["current_version"],
            "versions": [dict(row) for row in rows]}


def _propose(store, args):
    refs = _json([store.reference(ref) for ref in args["references"]]) if "references" in args else None
    title = _required_text(args["title"], "title")
    summary = _required_text(args["summary"], "summary")
    body = _required_text(args["body"], "body")
    reason = _required_text(args["reason"], "reason")
    if "note_id" in args:
        if "base_version" not in args:
            raise MemoryAccessError("Updating a note requires an explicit base_version")
        note = store.note(args["note_id"])
        if args.get("kind", note["kind"]) != note["kind"]:
            raise MemoryAccessError("A revision keeps its note kind")
        base_row = store.version(note["id"], args["base_version"])
        base = base_row["version"]
        if refs is None:
            refs = base_row["refs"]
        note_id, current = note["id"], note["current_version"]
    else:
        if "base_version" in args or "kind" not in args:
            raise MemoryAccessError("A new note requires a kind and has no base_version")
        note_id = "note-" + uuid.uuid4().hex
        base = current = 0
        store.db.execute("INSERT INTO notes (id, owner, kind) VALUES (?, ?, ?)",
                         (note_id, store.project, args["kind"]))
    proposal_id = "proposal-" + uuid.uuid4().hex
    store.db.execute("""INSERT INTO proposals
        (id, note_id, author, base_version, title, markdown, reason, refs, created)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (proposal_id, note_id, store.project, base, title, _markdown(title, summary, body), reason, refs or "[]", _now()),
    )
    return {"proposal_id": proposal_id, "note_id": note_id, "base_version": base,
            "current_version": current, "conflict": base != current, "status": "pending"}


def _proposal_rows(store, proposal_id):
    proposal = store.db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if proposal is None:
        raise MemoryAccessError("Proposal unavailable")
    note = store.db.execute("SELECT * FROM notes WHERE id = ?", (proposal["note_id"],)).fetchone()
    return proposal, note


def _proposal_result(store, proposal, note):
    result = {key: proposal[key] for key in (
        "note_id", "status", "base_version", "title", "markdown", "reason", "approved_version", "created",
    )}
    return dict(result, proposal_id=proposal["id"], kind=note["kind"], current_version=note["current_version"],
                conflict=proposal["base_version"] != note["current_version"] and proposal["status"] == "pending",
                references=store.display_refs(json.loads(proposal["refs"])))


def _proposal(store, args):
    proposal, note = _proposal_rows(store, args["proposal_id"])
    if note["owner"] != store.project:
        if proposal["author"] != store.project or not store.get_scope(note, "summary"):
            raise MemoryAccessError("Proposal unavailable")
    if note["current_version"]:
        note = store.sync(note)
    return _proposal_result(store, proposal, note)


def _record_trial(store, args):
    note = store.note(args["note_id"])
    if note["kind"] == "case":
        raise MemoryAccessError("Trials are recorded against playbook or skill versions")
    version = store.version(note["id"], args["version"])["version"]
    _required_text(args["task_id"], "task_id")
    _required_text(args["details"], "details")
    payload = dict(args)
    if "case_id" in args:
        case = store.note(args["case_id"])
        if case["kind"] != "case":
            raise MemoryAccessError("case_id must identify a case")
        payload["case_version"] = store.version(case["id"], args.get("case_version", case["current_version"]))["version"]
    elif "case_version" in args:
        raise MemoryAccessError("case_version requires a case_id")
    trial_id = _identity("trial-", store.project, payload)
    store.db.execute("INSERT OR IGNORE INTO trials VALUES (?, ?, ?, ?, ?, ?)",
                     (trial_id, store.project, note["id"], version, _json(payload), _now()))
    return {"trial_id": trial_id, "note_id": note["id"], "version": version}


def _trials(store, args):
    note = store.note(args["note_id"])
    version = args.get("version")
    if version is not None:
        store.version(note["id"], version)
    rows = store.db.execute("""SELECT * FROM trials WHERE project = ? AND note_id = ?
        AND (? IS NULL OR version = ?) ORDER BY created, id""",
        (store.project, note["id"], version, version)).fetchall()
    results = []
    for row in rows:
        trial = json.loads(row["payload"])
        if "case_id" in trial and not store.display_refs([{"id": trial["case_id"], "version": trial["case_version"]}]):
            trial.pop("case_id")
            trial.pop("case_version")
        results.append(dict(trial, trial_id=row["id"], created=row["created"]))
    return {"trials": results}


def _publish_pending_captures(store, source_id):
    rows = store.db.execute("""
        SELECT id FROM captures WHERE source_id = ? AND projected = 0 ORDER BY created, id
    """, (source_id,)).fetchall()
    for row in rows:
        _publish_capture(store, row["id"])


def _pending(store, args):
    rows = store.db.execute("""SELECT p.id FROM proposals p JOIN notes n ON n.id = p.note_id
        WHERE n.owner = ? AND p.status = 'pending' ORDER BY p.created, p.id""", (store.project,)).fetchall()
    return {"proposals": [_proposal(store, {"proposal_id": row["id"]}) for row in rows]}


def _decide(store, args, *, approve):
    proposal, note = _proposal_rows(store, args["proposal_id"])
    if note["owner"] != store.project:
        raise MemoryAccessError("Proposal unavailable")
    if note["current_version"]:
        note = store.sync(note)
    target = "approved" if approve else "rejected"
    if proposal["status"] == target:
        return _proposal_result(store, proposal, note)
    if proposal["status"] != "pending":
        raise MemoryAccessError("This proposal already has a decision")
    version = None
    if approve:
        current = note["current_version"]
        if current != args["expected_current"]:
            return dict(_proposal_result(store, proposal, note), conflict=True,
                        expected_current=args["expected_current"])
        version = store.add_version(note, proposal["base_version"], proposal["title"], proposal["markdown"],
                                    proposal["reason"], proposal["refs"], proposal["author"], publish=True)
    store.db.execute("UPDATE proposals SET status = ?, approved_version = ? WHERE id = ?",
                     (target, version, proposal["id"]))
    return {"proposal_id": proposal["id"], "note_id": note["id"], "status": target,
            "approved_version": version}


def _dependencies(store, args):
    source_id = args["source_id"]
    source = store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        raise MemoryAccessError("Source unavailable")
    source_refs = {source_id}
    source_refs.update(row["ref"] for row in store.db.execute(
        "SELECT ref FROM turns WHERE source_id = ?", (source_id,)))
    source_refs.update(row["id"] for row in store.db.execute(
        "SELECT id FROM captures WHERE source_id = ?", (source_id,)))

    notes = store.db.execute("SELECT * FROM notes WHERE owner = ? AND current_version > 0",
                             (store.project,)).fetchall()
    current_versions = {}
    unavailable = []
    for note in notes:
        try:
            note = store.sync(note)
        except (MemoryAccessError, UnicodeError, OSError):
            unavailable.append(note["id"])
        current_versions[note["id"]] = note["current_version"]

    dependents = defaultdict(set)
    matched = {}
    direct = set()
    rows = store.db.execute("""SELECT v.*, n.kind FROM versions v JOIN notes n ON n.id = v.note_id
        WHERE n.owner = ? ORDER BY v.note_id, v.version""", (store.project,)).fetchall()
    for row in rows:
        key = (row["note_id"], row["version"])
        refs = json.loads(row["refs"])
        # A bare vault link has no pinned version. Include its possible dependencies for review.
        refs.extend({"id": target} for target in _WIKI_TARGET.findall(row["markdown"]))
        matched[key] = row
        for ref in refs:
            target = ref["id"]
            if target in source_refs:
                direct.add(key)
            elif target in current_versions:
                dependents[(target, ref.get("version"))].add(key)

    affected = set(direct)
    pending = deque(direct)
    while pending:
        identifier, version = pending.popleft()
        new = (dependents[(identifier, version)] | dependents[(identifier, None)]) - affected
        affected.update(new)
        pending.extend(new)

    accessible = store.get_scope(source, "raw")
    if not accessible and not affected and source["owner"] != store.project:
        raise MemoryAccessError("Source unavailable")
    results = [
        {"note_id": identifier, "version": version, "kind": matched[(identifier, version)]["kind"],
         "title": matched[(identifier, version)]["title"], "direct": (identifier, version) in direct,
         "current": version == current_versions[identifier]}
        for identifier, version in sorted(affected)
    ]
    return {"source_id": source_id, "raw_accessible": accessible, "affected_versions": results,
            "unavailable_current_notes": sorted(unavailable)}


def _change_grant(store, args, *, revoke):
    identifier = args["id"]
    recipient = _required_text(args["to_project"], "to_project")
    if recipient == store.project:
        raise MemoryAccessError("A grant requires another project")
    scope = args["scope"]
    source = store.db.execute("SELECT id FROM sources WHERE id = ? AND owner = ?",
                              (identifier, store.project)).fetchone()
    note = store.db.execute("SELECT id FROM notes WHERE id = ? AND owner = ? AND current_version > 0",
                            (identifier, store.project)).fetchone()
    if source is None and note is None:
        raise MemoryAccessError("Record unavailable for sharing")
    if scope != "all" and ((scope == "raw") != (source is not None)):
        raise MemoryAccessError("Use raw for a source and summary or body for a note")
    if not revoke and source is not None and store.withdrawn(identifier):
        raise MemoryAccessError("Restore the source before sharing it")
    if revoke:
        count = store.db.execute("""DELETE FROM grants WHERE id = ? AND project = ?
            AND (? = 'all' OR scope = ?)""", (identifier, recipient, scope, scope)).rowcount
    else:
        count = store.db.execute("INSERT OR IGNORE INTO grants VALUES (?, ?, ?)",
                                 (identifier, recipient, scope)).rowcount
    result = {"id": identifier, "to_project": recipient, "scope": scope, "changed": bool(count)}
    if revoke:
        result["restriction"] = "Only future service access changes. Previously delivered text remains with recipients."
        if source is not None:
            result["dependency_review"] = _dependencies(store, {"source_id": identifier})
    return result


def _set_source_withdrawal(store, args, *, withdraw):
    source_id = args["source_id"]
    source = store.db.execute("SELECT 1 FROM sources WHERE id = ? AND owner = ?",
                              (source_id, store.project)).fetchone()
    if source is None:
        raise MemoryAccessError("Source unavailable")
    if withdraw:
        store.db.execute("INSERT OR IGNORE INTO source_withdrawals VALUES (?, ?)", (source_id, _now()))
        store.db.execute("DELETE FROM grants WHERE id = ? AND scope = 'raw'", (source_id,))
    else:
        store.db.execute("DELETE FROM source_withdrawals WHERE source_id = ?", (source_id,))
    return {"source_id": source_id, "withdrawn": withdraw, "dependency_review": _dependencies(store, args),
            "restriction": "Only future service access changes. Previously delivered text remains with recipients."}


_HANDLERS = {
    "memory_capture": _capture,
    "memory_search": _search,
    "memory_get": _get,
    "memory_versions": _versions,
    "memory_source": _source,
    "memory_propose": _propose,
    "memory_proposal": _proposal,
    "memory_record_trial": _record_trial,
    "memory_trials": _trials,
}


def call(name, args, *, root: Path, project: str):
    """Execute an MCP tool after the adapter checks the authenticated connection mode."""
    if name not in TOOLS:
        raise MemoryAccessError("Unknown memory tool")
    _validate(args, TOOLS[name]["inputSchema"], "arguments")
    with _store(root, project) as store:
        result = _HANDLERS[name](store, args)
    if name == "memory_capture":
        with _store(root, project) as store:
            _publish_pending_captures(store, result["source_id"])
    return result


def manage(action, args, *, root: Path, project: str):
    """Trusted local management: grants apply to the full history and future revisions."""
    if action not in _MANAGEMENT:
        raise MemoryAccessError("Unknown memory management action")
    _validate(args, _MANAGEMENT[action], "arguments")
    with _store(root, project) as store:
        if action == "pending":
            return _pending(store, args)
        if action == "dependencies":
            return _dependencies(store, args)
        if action in ("withdraw-source", "restore-source"):
            return _set_source_withdrawal(store, args, withdraw=action == "withdraw-source")
        if action in ("grant", "revoke"):
            return _change_grant(store, args, revoke=action == "revoke")
        result = _decide(store, args, approve=action == "approve")
    if result["status"] == "approved":
        with _store(root, project) as store:
            note = store.note(result["note_id"])
            result.update(current_version=note["current_version"], **_links(store, store.note_location(note)))
    return result


def _main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Grants cover a record's history and future revisions or captures. Revocation affects future access. "
               "Use dependencies SOURCE_ID to review local case and playbook versions before or after revocation. "
               "Withdrawing a source blocks service access and removes its raw grants. Restoring reopens it to its owner.",
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--project", required=True, help="Owning project as registered in the local service")
    actions = parser.add_subparsers(dest="action", required=True)
    for action, schema in _MANAGEMENT.items():
        command = actions.add_parser(action)
        for key in schema["required"]:
            field = schema["properties"][key]
            command.add_argument(key, type=int if field["type"] == "integer" else str, choices=field.get("enum"))
    options = vars(parser.parse_args())
    root, project, action = (options.pop(key) for key in ("root", "project", "action"))
    try:
        result = manage(action, options, root=root, project=project)
    except (MemoryAccessError, OSError, UnicodeError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if action == "approve" and result["status"] == "pending" else 0


if __name__ == "__main__":
    raise SystemExit(_main())
