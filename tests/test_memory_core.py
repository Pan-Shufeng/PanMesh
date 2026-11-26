"""Memory-domain checks using isolated synthetic captures and standard library only."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import url2pathname

import memory_core as memory


class MemoryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def call(self, tool, project="甲", **args):
        return memory.call("memory_" + tool, args, root=self.root, project=project)

    def manage(self, action, project="甲", **args):
        return memory.manage(action, args, root=self.root, project=project)

    def capture(self, project="甲", **kwargs):
        args = dict(client="synthetic", source_key="test-conversation", kind="conversation",
                    scope="excerpt", selection="第 2 轮", gaps=["附件未提供"], turns=[
                        dict(turn_id="u2", ordinal=2, role="user", text="原话 \n`示例`  ", locator="turn:2"),
                    ])
        return self.call("capture", project, **(args | kwargs))

    def draft(self, project="甲", **kwargs):
        args = dict(title="工况核对", summary="核对同一工作点", body="# 方法\n\n私有工作笔记", reason="首次整理")
        if "note_id" not in kwargs:
            args["kind"] = "playbook"
        return self.call("propose", project, **(args | kwargs))

    def approve(self, draft, expected=0):
        return self.manage("approve", proposal_id=draft["proposal_id"], expected_current=expected)

    def link_path(self, result):
        return self.root / "vault" / (result["link"][2:-2].split("#", 1)[0] + ".md")

    def raw_path(self, result):
        uri = urlsplit(result["raw_uri"])
        self.assertEqual(uri.scheme, "file")
        return Path(url2pathname(uri.path))

    def test_capture_is_append_only_and_preserves_stable_links_and_order(self):
        first = self.capture()
        later = self.capture(scope="complete", selection="", gaps=[], turns=[
            dict(turn_id="u2", ordinal=2, role="user", text="原话 \n`示例`  "),
            dict(turn_id="a1", ordinal=1, role="assistant", text="初稿"),
            dict(turn_id="a3", ordinal=3, role="assistant", text="修订"),
        ])
        retry = self.capture()
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["capture_id"], first["capture_id"])
        self.assertEqual(later["source_id"], first["source_id"])
        self.assertEqual(later["turns"][1], first["turns"][0])

        source = self.call("source", source_id=first["source_id"])
        self.assertEqual([turn["ordinal"] for turn in source["turns"]], [1, 2, 3])
        self.assertEqual(source["turns"][1]["text"], "原话 \n`示例`  ")
        self.assertEqual(source["captures"][0]["gaps"], ["附件未提供"])
        self.assertEqual(len(source["captures"]), 2)
        original_turn = first["turns"][0]
        first_page = self.link_path(first).read_text(encoding="utf-8")
        later_page = self.link_path(later).read_text(encoding="utf-8")
        turn_block = "#^" + original_turn["turn_ref"]
        self.assertEqual(self.link_path(original_turn), self.link_path(first))
        self.assertIn(first["link"], later_page)
        for page in (first_page, later_page):
            self.assertIn(original_turn["raw_uri"], page)
            self.assertIn(turn_block[1:], page)
            self.assertNotIn("`示例`", page)
        self.assertLess(later_page.index("第 1 轮"), later_page.index("第 2 轮"))
        self.assertLess(later_page.index("第 2 轮"), later_page.index("第 3 轮"))
        self.assertEqual(set((self.root / "vault" / "来源").glob("*.md")),
                         {self.link_path(first), self.link_path(later)})
        uri = urlsplit(original_turn["obsidian_uri"])
        self.assertEqual((uri.scheme, uri.netloc), ("obsidian", "open"))
        path, block = parse_qs(uri.query)["path"][0].split("#", 1)
        self.assertEqual(Path(path), self.link_path(first))
        self.assertEqual("#" + block, turn_block)
        imports = list((self.root / "imports").rglob("*.json"))
        self.assertEqual(len(imports), 2)
        self.assertEqual(len(list((self.root / "imports").rglob("*.txt"))), 3)
        first_import = self.raw_path(first)
        manifest = json.loads(first_import.read_text(encoding="utf-8"))
        self.assertEqual(manifest["turns"][0]["locator"], "turn:2")
        self.assertEqual(first_import.parent / manifest["turns"][0]["text_file"], self.raw_path(original_turn))
        self.assertEqual(self.raw_path(original_turn).read_bytes(), "原话 \n`示例`  ".encode("utf-8"))

    def test_overlapping_excerpts_and_continuation_conflict(self):
        def turn(text, start=None):
            result = dict(turn_id="u2", ordinal=2, role="user", text=text)
            if start is not None:
                result["char_start"] = start
            return result

        first = self.capture(selection="第 2 轮字符 2–4", turns=[turn("丙丁", 2)])
        source_id = first["source_id"]
        with self.assertRaises(memory.MemoryAccessError):
            self.capture(turns=[turn("甲乙丙错")])
        self.capture(scope="complete", gaps=[], turns=[turn("甲乙丙丁戊")])
        with self.assertRaises(memory.MemoryAccessError):
            self.capture(turns=[turn("超出", 5)])
        exact = self.call("source", source_id=source_id, turn_ref=first["turns"][0]["turn_ref"])
        self.assertEqual(exact["turn"]["text"], "丙丁")
        capture = self.call("source", source_id=source_id, capture_id=first["capture_id"])
        self.assertEqual(capture["selection"]["turns"], [turn("丙丁", 2)])
        self.assertEqual(len(self.call("source", source_id=source_id)["captures"]), 2)

    def test_capture_retry_completes_publication_after_partial_failure(self):
        original = memory._atomic_text

        def fail_on_vault(path, content):
            if path.suffix == ".md":
                raise OSError("synthetic vault write failure")
            original(path, content)

        with patch.object(memory, "_atomic_text", side_effect=fail_on_vault):
            with self.assertRaises(OSError):
                self.capture()
        receipt = self.capture()
        self.assertTrue(receipt["duplicate"])
        self.assertTrue(self.link_path(receipt).is_file())
        self.assertTrue(self.link_path(receipt["turns"][0]).is_file())
        self.assertEqual(len(list((self.root / "imports").rglob("*.json"))), 1)
        self.assertEqual(len(list((self.root / "imports").rglob("*.txt"))), 1)

    def test_concurrent_capture_retries_have_one_copy(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: self.capture(), range(8)))
        self.assertEqual(len({receipt["capture_id"] for receipt in receipts}), 1)
        self.assertEqual(sum(not receipt["duplicate"] for receipt in receipts), 1)
        self.assertEqual(len(list((self.root / "imports").rglob("*.json"))), 1)
        self.assertEqual(len(list((self.root / "vault").rglob("*.md"))), 1)
        self.assertEqual(len(list((self.root / "imports").rglob("*.txt"))), 1)

    def test_material_verbatim_bytes_only_appear_in_imports(self):
        material = "\r\n# 原始段落 [说明](https://example.test)\r\n末行  "
        receipt = self.capture(kind="material", source_key="file-with-text", turns=[], text=material,
                               selection="文件选段")
        notes = list((self.root / "vault" / "来源").glob("*.md"))
        self.assertEqual(notes, [self.link_path(receipt)])
        note = notes[0].read_text(encoding="utf-8")
        self.assertNotIn("原始段落", note)
        self.assertIn(receipt["raw_uri"], note)
        self.assertEqual(self.raw_path(receipt).read_bytes(), material.encode("utf-8"))
        capture = self.call("source", capture_id=receipt["capture_id"])
        self.assertEqual(capture["selection"]["text"], material)
        self.assertEqual(capture["raw_uri"], receipt["raw_uri"])

    def test_proposal_conflict_preserves_local_edit_and_pinned_versions(self):
        first = self.draft()
        self.assertEqual(self.call("search", query="")["results"], [])
        self.assertEqual(self.approve(first)["approved_version"], 1)
        note = first["note_id"]
        previous = self.call("get", note_id=note, version=1, view="body")
        suggestion = self.draft(note_id=note, base_version=1, summary="AI 建议", body="提案正文")

        path = self.link_path(previous)
        edited = '---\ntitle: 工况复核新版\nsummary: >-\n  新摘要\n  同一样品\n---\n\nObsidian 改稿\n'
        path.write_bytes(edited.encode("utf-8"))
        conflict = self.approve(suggestion, expected=1)
        self.assertEqual((conflict["status"], conflict["current_version"]), ("pending", 2))
        self.assertEqual(self.manage("pending")["proposals"][0]["title"], "工况核对")
        self.assertEqual(self.call("search", query="复核新版 同一样品")["results"][0]["version"], 2)
        self.assertEqual(self.call("get", note_id=note, view="body")["markdown"], edited)
        self.assertEqual(self.approve(suggestion, expected=2)["approved_version"], 3)
        self.assertEqual(self.approve(suggestion, expected=2)["approved_version"], 3)
        self.assertEqual(self.call("get", note_id=note)["summary"], "AI 建议")
        self.assertEqual(self.call("get", note_id=note, version=2, view="body")["markdown"], edited)
        self.assertEqual(self.call("get", note_id=note, version=1, view="body")["markdown"], previous["markdown"])
        history = self.call("versions", note_id=note)["versions"]
        self.assertEqual([(item["version"], item["parent_version"]) for item in history], [(3, 1), (2, 1), (1, 0)])
        path.unlink()
        with self.assertRaises(memory.MemoryAccessError):
            self.call("get", note_id=note)
        self.assertEqual(self.call("get", note_id=note, version=1)["summary"], "核对同一工作点")

    def test_approved_snapshot_recovers_failed_markdown_write(self):
        first = self.draft()
        with patch.object(memory, "_atomic_text", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                self.approve(first)
        approval = self.approve(first)
        self.assertEqual(approval["approved_version"], 1)
        self.assertTrue(self.link_path(approval).is_file())
        self.assertEqual(len(self.call("versions", note_id=first["note_id"])["versions"]), 1)

    def test_summary_body_raw_and_revocation_are_independent(self):
        receipt = self.capture()
        reference = {"id": receipt["turns"][0]["turn_ref"]}
        case = self.draft(kind="case", references=[reference], body=receipt["turns"][0]["link"])
        self.approve(case)
        playbook = self.draft(references=[{"id": case["note_id"]}, reference])
        self.approve(playbook)
        note_id, source_id = playbook["note_id"], receipt["source_id"]
        self.manage("grant", id=note_id, to_project="乙", scope="summary")
        summary = self.call("get", "乙", note_id=note_id)
        self.assertEqual(summary["summary"], "核对同一工作点")
        self.assertNotIn("markdown", summary)
        self.assertNotIn("references", summary)
        self.assertEqual(self.call("search", "乙", query="私有工作笔记")["results"], [])
        for tool, args in (("get", {"note_id": note_id, "view": "body"}), ("source", {"source_id": source_id}),
                           ("get", {"note_id": case["note_id"]})):
            with self.assertRaises(memory.MemoryAccessError):
                self.call(tool, "乙", **args)
        with self.assertRaises(memory.MemoryAccessError):
            self.manage("grant", "乙", id=note_id, to_project="丙", scope="summary")
        self.manage("grant", id=note_id, to_project="乙", scope="body")
        self.assertEqual(self.call("get", "乙", note_id=note_id, view="body")["references"], [])
        self.manage("grant", id=source_id, to_project="乙", scope="raw")
        self.assertEqual(self.call("get", "乙", note_id=note_id, view="body")["references"], [reference])
        self.assertEqual(len(self.call("source", "乙", source_id=source_id)["turns"]), 1)
        self.assertEqual(self.call("source", "乙", turn_ref=reference["id"])["source_id"], source_id)
        self.manage("revoke", id=source_id, to_project="乙", scope="raw")
        self.assertEqual(self.call("get", "乙", note_id=note_id, view="body")["references"], [])
        with self.assertRaises(memory.MemoryAccessError):
            self.call("source", "乙", source_id=source_id)
        with self.assertRaises(memory.MemoryAccessError):
            self.call("source", "乙", turn_ref=reference["id"])
        self.manage("revoke", id=note_id, to_project="乙", scope="all")
        self.assertEqual(self.call("search", "乙", query="")["results"], [])
        with self.assertRaises(memory.MemoryAccessError):
            self.call("get", "乙", note_id=note_id, version=1)

    def test_trial_is_private_and_pinned_to_method_and_case(self):
        method = self.draft()
        case = self.draft(kind="case", title="新任务")
        self.approve(method)
        self.approve(case)
        note_id, case_id = method["note_id"], case["note_id"]
        trial = dict(note_id=note_id, version=1, task_id="task-1", case_id=case_id,
                     outcome="partial", details="复核时补充了边界")
        first = self.call("record_trial", **trial)
        self.assertEqual(self.call("record_trial", **trial), first)
        self.approve(self.draft(note_id=note_id, base_version=1), expected=1)
        self.assertEqual(self.call("trials", note_id=note_id, version=2)["trials"], [])
        saved = self.call("trials", note_id=note_id, version=1)["trials"]
        self.assertEqual([(item["version"], item["case_version"]) for item in saved], [(1, 1)])
        self.manage("grant", id=note_id, to_project="乙", scope="summary")
        self.assertEqual(self.call("trials", "乙", note_id=note_id)["trials"], [])

    def test_shared_project_can_propose_but_cannot_approve(self):
        first = self.draft()
        self.approve(first)
        note_id = first["note_id"]
        self.manage("grant", id=note_id, to_project="乙", scope="summary")
        draft = self.draft("乙", note_id=note_id, base_version=1, summary="共享方提议")
        with self.assertRaises(memory.MemoryAccessError):
            self.manage("approve", "乙", proposal_id=draft["proposal_id"], expected_current=1)
        self.assertEqual(self.call("get", "乙", note_id=note_id)["version"], 1)
        self.assertEqual(self.manage("reject", proposal_id=draft["proposal_id"])["status"], "rejected")
        self.assertEqual(self.call("proposal", "乙", proposal_id=draft["proposal_id"])["status"], "rejected")
        with self.assertRaises(memory.MemoryAccessError):
            self.call("approve", proposal_id=draft["proposal_id"])

    def test_source_dependency_review_survives_revocation(self):
        receipt = self.capture()
        source = receipt["source_id"]
        self.manage("grant", id=source, to_project="乙", scope="raw")
        case = self.draft("乙", kind="case", body=receipt["turns"][0]["link"])
        self.manage("approve", "乙", proposal_id=case["proposal_id"], expected_current=0)
        method = self.draft("乙", references=[{"id": case["note_id"], "version": 1}])
        self.manage("approve", "乙", proposal_id=method["proposal_id"], expected_current=0)
        revised = self.draft("乙", note_id=case["note_id"], base_version=1, body="移除来源")
        self.manage("approve", "乙", proposal_id=revised["proposal_id"], expected_current=1)

        before = self.manage("dependencies", "乙", source_id=source)
        self.assertTrue(before["raw_accessible"])
        versions = {(row["note_id"], row["version"], row["direct"], row["current"])
                    for row in before["affected_versions"]}
        self.assertEqual(versions, {(case["note_id"], 1, True, False), (method["note_id"], 1, False, True)})
        revoked = self.manage("revoke", id=source, to_project="乙", scope="raw")
        self.assertEqual(revoked["dependency_review"]["affected_versions"], [])
        self.assertIn("Previously delivered", revoked["restriction"])
        after = self.manage("dependencies", "乙", source_id=source)
        self.assertFalse(after["raw_accessible"])
        self.assertEqual(after["affected_versions"], before["affected_versions"])
        with self.assertRaises(memory.MemoryAccessError):
            self.call("source", "乙", source_id=source)

    def test_withdrawn_source_preserves_original_and_does_not_restore_prior_grants(self):
        receipt = self.capture()
        source = receipt["source_id"]
        case = self.draft(kind="case", references=[{"id": receipt["capture_id"]}])
        self.approve(case)
        self.manage("grant", id=source, to_project="乙", scope="raw")
        withdrawn = self.manage("withdraw-source", source_id=source)
        self.assertFalse(withdrawn["dependency_review"]["raw_accessible"])
        self.assertEqual(withdrawn["dependency_review"]["affected_versions"][0]["note_id"], case["note_id"])
        self.assertTrue(self.link_path(receipt).is_file())
        with self.assertRaises(memory.MemoryAccessError):
            self.call("source", source_id=source)
        with self.assertRaises(memory.MemoryAccessError):
            self.capture()
        self.manage("restore-source", source_id=source)
        self.assertEqual(len(self.call("source", source_id=source)["captures"]), 1)
        with self.assertRaises(memory.MemoryAccessError):
            self.call("source", "乙", source_id=source)

    def test_capture_registry_and_invalid_inputs(self):
        self.assertEqual([name for name, spec in memory.TOOLS.items() if spec["mode"] == "capture"],
                         ["memory_capture"])
        with self.assertRaises(memory.MemoryAccessError):
            self.capture(selection="")
        with self.assertRaises(memory.MemoryAccessError):
            self.call("source", source_id="nonexistent", project_id="甲")
        with self.assertRaises(memory.MemoryAccessError):
            self.call("get", note_id="nonexistent", version=True)
        reference = self.capture(kind="material", source_key="file-a", scope="reference", turns=[],
                                 locator="E:/existing/package.zip", selection="附录")
        source = self.call("source", source_id=reference["source_id"])
        self.assertEqual(source["captures"][0]["locator"], "E:/existing/package.zip")
        self.assertNotIn("text", source["captures"][0])


if __name__ == "__main__":
    unittest.main()
