from pathlib import Path
import json
import tempfile
import unittest

import capture_hook
import manage


class SelectedHook(unittest.TestCase):
    def test_only_selected_events_and_only_new_visible_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manage.enroll(root, "intake", "甲", "writer", "capture")
            for client in ("codex", "claude"):
                path = root / (client + ".jsonl")
                if client == "codex":
                    records = [{"type":"session_meta","payload":{"id":"chosen"}},
                               {"type":"event_msg","payload":{"type":"user_message","message":"测试问题"}},
                               {"type":"response_item","payload":{"type":"reasoning","text":"exclude"}}]
                    more = {"type":"event_msg","payload":{"type":"item_completed","item":{
                        "type":"AgentMessage","content":[{"type":"Text","text":"测试回复"}]}}}
                else:
                    records = [{"sessionId":"chosen","type":"user","message":{"content":"测试问题"}},
                               {"sessionId":"chosen","type":"assistant","message":{"content":[
                                   {"type":"thinking","thinking":"exclude"}]}}]
                    more = {"sessionId":"chosen","type":"assistant","message":{"content":"测试回复"}}
                path.write_text("".join(json.dumps(v)+"\n" for v in records), encoding="utf-8")
                event = dict(session_id="chosen", hook_event_name="Stop", cwd=str(root), transcript_path=str(path))
                self.assertIsNone(capture_hook.on_stop(root, "intake", client, event))
                capture_hook.authorize(root, "intake", client, "chosen", root, path)
                self.assertEqual(len(capture_hook.on_stop(root, "intake", client, event)["turns"]), 1)
                self.assertIsNone(capture_hook.on_stop(root, "intake", client, event))
                with path.open("a",encoding="utf-8") as f:
                    f.write(json.dumps(more)+"\n")
                self.assertEqual(len(capture_hook.on_stop(root, "intake", client, event)["turns"]), 1)
