from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import manage
import routing


class ProjectRoute(unittest.TestCase):
    def test_owner_cannot_choose_another_connection_or_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, project, actor in [("a","甲","owner"),("b","甲","worker"),("c","乙","owner")]:
                manage.enroll(root,name,project,actor,"collab")
            with self.assertRaises(ValueError):
                routing.register(root,"c","bad","b","codex",root)
            routing.register(root,"a","codex","b","codex",root)
            self.assertEqual(routing.call("run_routes",{},root=root,connection_name="c"),[])
            args={"task_id":"task","dispatch_id":"dispatch","route":"codex"}
            with self.assertRaises(PermissionError):
                routing.call("run_start",args,root=root,connection_name="c")
            with patch("connectors.coordinator.enqueue",return_value={"state":"queued"}) as enqueue:
                routing.call("run_start",args,root=root,connection_name="a")
                enqueue.assert_called_once_with(root,"a","b","task","dispatch","codex",str(root),None)
            routing.register(root,"a","desktop","b","claude-desktop",root)
            routes=routing.call("run_routes",{},root=root,connection_name="a")
            self.assertEqual(next(r for r in routes if r["route"]=="desktop")["channel"],"pull")
            with self.assertRaises(ValueError):
                routing.call("run_start",{**args,"route":"desktop"},root=root,connection_name="a")
