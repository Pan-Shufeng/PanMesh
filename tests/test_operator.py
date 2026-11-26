import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import manage
import memory_core
from memory_service import connection


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_connection_is_bound_and_revocable(self):
        path = manage.enroll(self.root, "capture", "项目甲", "writer", "capture")
        secret = json.loads(path.read_text(encoding="utf-8"))["env"]["MEMORY_CONNECTION_SECRET"]
        self.assertEqual(connection(self.root, "capture", secret), ("项目甲", "writer", "capture"))
        with self.assertRaises(PermissionError):
            connection(self.root, "capture", "incorrect")
        with self.assertRaises(sqlite3.IntegrityError):
            manage.enroll(self.root, "capture", "different", "owner", "collab")
        db = manage.registry(self.root)
        db.execute("DELETE FROM connections")
        db.commit()
        db.close()
        with self.assertRaises(PermissionError):
            connection(self.root, "capture", secret)

    def test_restore_contains_committed_state_and_excludes_secrets(self):
        manage.enroll(self.root, "test", "test", "reader", "collab")
        note = self.root / "vault/测试.md"
        note.parent.mkdir()
        note.write_text("已验收", encoding="utf-8")
        path = self.root / "runtime/example.db"
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE tasks(state)")
        db.execute("INSERT INTO tasks VALUES('open')")
        db.commit()
        snap = manage.backup(self.root)
        target = self.root / "recovered"
        manage.restore(snap, target)
        self.assertEqual(note.read_bytes(), (target / "vault/测试.md").read_bytes())
        self.assertFalse((target / "runtime/credentials").exists())
        self.assertFalse((target / "runtime/connections.sqlite3").exists())
        restored = sqlite3.connect(target / "runtime/example.db")
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute("SELECT state FROM tasks").fetchone()[0], "open")
        with self.assertRaises(ValueError):
            manage.restore(snap, target)
        (snap / "vault/测试.md").write_text("意外修改", encoding="utf-8")
        with self.assertRaises(ValueError):
            manage.restore(snap, self.root / "invalid")

    def test_selected_binary_copy_deduplicates_and_links_to_recovered_material(self):
        manage.enroll(self.root, "intake", "甲", "reader", "capture")
        source = self.root / "外部测试材料.zip"
        source.write_bytes(b"binary\x00fixture")
        capture = manage.import_file(self.root, "intake", source, True)
        self.assertTrue(manage.import_file(self.root,"intake",source,True)["duplicate"])
        backup = manage.backup(self.root)
        dest = manage.restore(backup,self.root/"new")
        copied = list((dest/"imports").rglob(source.name))
        self.assertEqual(len(copied),1)
        self.assertEqual(copied[0].read_bytes(),source.read_bytes())
        index = dest/"vault/来源"/(capture["capture_id"]+".md")
        self.assertIn(copied[0].as_uri(),index.read_text(encoding="utf-8"))
        restored = memory_core.call("memory_source", {"capture_id": capture["capture_id"]}, root=dest, project="甲")
        self.assertEqual(restored["material_uri"], copied[0].as_uri())

    def test_link_project_preserves_existing_settings_and_handles_repeated_calls(self):
        import tomllib
        manage.enroll(self.root, "tools-a", "甲", "assistant", "collab")
        manage.enroll(self.root, "tools-b", "乙", "assistant", "collab")
        workspace = self.root / "工作区"
        (workspace / ".codex").mkdir(parents=True)
        (workspace / ".codex/config.toml").write_text('model = "custom"\n', encoding="utf-8")
        (workspace / ".mcp.json").write_text('{"mcpServers":{"keep":{"command":"original"}}}',encoding="utf-8")
        for client in ("codex", "claude", "cursor"):
            config = manage.link_project(self.root, "tools-a", client, workspace)
            self.assertEqual(config, manage.link_project(self.root, "tools-a", client, workspace))
            with self.assertRaises(ValueError):
                manage.link_project(self.root, "tools-b", client, workspace)
            data = config.read_text(encoding="utf-8")
            self.assertIn("tools-a", data)
            self.assertNotIn("MEMORY_CONNECTION_SECRET", data)
        self.assertEqual(tomllib.loads((workspace/".codex/config.toml").read_text())["model"], "custom")
        self.assertIn("keep", json.loads((workspace/".mcp.json").read_text(encoding="utf-8"))["mcpServers"])
