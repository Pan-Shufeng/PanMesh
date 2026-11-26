"""A restored task must still resolve the exact method version it received."""
from pathlib import Path
import tempfile
import unittest

import ledger_core
import manage
import memory_core


class RestoreWorkflow(unittest.TestCase):
    def test_inflight_job_keeps_method_version_across_edit_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bind = {"root": root, "project": "甲"}
            capture = memory_core.call("memory_capture",dict(client="test",source_key="test-dialog",
                kind="conversation",scope="complete",turns=[dict(turn_id="1",ordinal=1,role="user",text="验收原话")]),**bind)
            draft = memory_core.call("memory_propose", dict(kind="playbook", title="抽样",
                                     summary="先看编号", body="首版", reason="试验"), **bind)
            note = memory_core.manage("approve", dict(proposal_id=draft["proposal_id"], expected_current=0), **bind)
            method = dict(method_id=note["note_id"], version="1", summary="先看编号")
            job = ledger_core.call("task_create", dict(request_id="make",title="校对",goal="查编号",
                                   deliverable="校对结果",criteria=[{"id":"number","requirement":"找到编号"}],
                                   methods=[method]), actor="owner", **bind)
            tid = job["task_id"]
            dispatch = ledger_core.call("task_dispatch", dict(request_id="send",task_id=tid,previous_dispatch_id=None,
                                        executor="worker",channel="pull",client="codex"), actor="owner", **bind)
            ledger_core.call("task_claim", dict(request_id="claim",task_id=tid,dispatch_id=dispatch["dispatch_id"],
                             session_id="previous-session"), actor="worker", **bind)
            file = root / "vault" / (note["link"][2:-2] + ".md")
            file.write_text(file.read_text(encoding="utf-8").replace("先看编号","先对照原文编号"),encoding="utf-8")
            self.assertEqual(memory_core.call("memory_get",dict(note_id=note["note_id"]),**bind)["version"],2)
            other = root / "restore"
            manage.restore(manage.backup(root), other)
            restored_source = other / "vault/来源" / (capture["capture_id"] + ".md")
            self.assertIn((other / "imports").as_uri(), restored_source.read_text(encoding="utf-8"))
            view = ledger_core.call("task_get", dict(task_id=tid),root=other,project="甲",actor="owner")
            self.assertEqual(view["status"],"running")
            self.assertEqual(view["dispatch"]["dispatch_id"],dispatch["dispatch_id"])
            snapshot = memory_core.call("memory_get",dict(note_id=method["method_id"],version=1),root=other,project="甲")
            self.assertEqual(snapshot["summary"],"先看编号")
            current = memory_core.call("memory_get",dict(note_id=method["method_id"]),root=other,project="甲")
            self.assertEqual(current["summary"],"先对照原文编号")
