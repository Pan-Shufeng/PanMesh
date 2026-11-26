"""Integration checks against separate stdio MCP processes and one shared store."""
from contextlib import asynccontextmanager, AsyncExitStack, closing
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import manage


@asynccontextmanager
async def connect(root, project, actor, mode, name=None):
    name = name or actor + "-" + mode
    path = manage.enroll(root, name, project, actor, mode)
    desc = json.loads(path.read_text(encoding="utf-8"))
    bootstrap = ("import asyncio,os,sys;from pathlib import Path;from memory_service import serve;"
                 "asyncio.run(serve(Path(sys.argv[1]),sys.argv[2],os.environ['MEMORY_CONNECTION_SECRET']))")
    params = StdioServerParameters(command=sys.executable, args=["-c", bootstrap, str(root), name],
                                  env=desc["env"], cwd=manage.ROOT)
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        await session.initialize()
        yield session


class ProjectIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_three_projects_can_run_same_case_and_actor_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with AsyncExitStack() as stack:
                clients = [
                    await stack.enter_async_context(connect(root, name, "lead", "collab", "lead-" + name))
                    for name in ("alpha", "beta", "gamma")
                ]
                captured = await asyncio.gather(*[
                    session.call_tool("memory_capture", dict(client="QA", source_key="shared-session-id",
                        kind="conversation", scope="complete", turns=[dict(turn_id="1", ordinal=1,
                        role="user", text="独立项目原文 " + str(index))]))
                    for index, session in enumerate(clients)
                ])
                jobs = await asyncio.gather(*[
                    session.call_tool("task_create", dict(request_id="same-request-id", title="同一标题",
                        goal="核对", deliverable="独立结果", criteria=[dict(id="check", requirement="查原话")]))
                    for session in clients
                ])
                for group, key, action in [(captured, "source_id", "memory_source"), (jobs, "task_id", "task_get")]:
                    self.assertFalse(any(response.isError for response in group))
                    ids = [json.loads(response.content[0].text)[key] for response in group]
                    self.assertEqual(len(set(ids)), 3)
                    for index, session in enumerate(clients):
                        for other, identifier in enumerate(ids):
                            result = await session.call_tool(action, {key: identifier})
                            self.assertEqual(result.isError, other != index)

    async def test_real_transport_and_project_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with AsyncExitStack() as stack:
                capture = await stack.enter_async_context(connect(root, "甲", "intake", "capture"))
                a = await stack.enter_async_context(connect(root, "甲", "lead", "collab"))
                b = await stack.enter_async_context(connect(root, "乙", "second", "collab"))
                self.assertEqual([t.name for t in (await capture.list_tools()).tools], ["memory_capture"])
                selected = {"client": "验收", "source_key": "test01", "kind": "conversation",
                            "scope": "excerpt", "selection": "用户的一轮", "turns": [
                                {"turn_id": "t1", "ordinal": 1, "role": "user", "text": "测试用原话"}]}
                result = await capture.call_tool("memory_capture", selected)
                self.assertFalse(result.isError, result.content)
                receipt = json.loads(result.content[0].text)
                source = {"source_id": receipt["source_id"]}
                self.assertFalse((await a.call_tool("memory_source", source)).isError)
                self.assertTrue((await b.call_tool("memory_source", source)).isError)
                self.assertTrue((await capture.call_tool("memory_source", source)).isError)
                self.assertTrue((await b.call_tool("task_list", {"project": "甲"})).isError)
                self.assertFalse((await b.call_tool("task_list", {})).isError)
                # Both projects may legitimately use the same client request id.
                job = dict(request_id="first-job",title="平行验收",goal="跨项目独立",
                           deliverable="回报",criteria=[dict(id="check",requirement="核对")])
                jobs = await asyncio.gather(a.call_tool("task_create",job),b.call_tool("task_create",job))
                self.assertFalse(any(v.isError for v in jobs))
                left,right = (json.loads(v.content[0].text)["task_id"] for v in jobs)
                self.assertNotEqual(left,right)
                self.assertTrue((await b.call_tool("task_get",{"task_id":left})).isError)
                self.assertTrue((await a.call_tool("task_get",{"task_id":right})).isError)
                worker = await stack.enter_async_context(connect(root,"甲","reviewer","collab"))
                disp = await a.call_tool("task_dispatch",dict(request_id="dispatch",task_id=left,
                    previous_dispatch_id=None,executor="reviewer",channel="pull",client="test"))
                self.assertFalse(disp.isError,disp.content)
                fields = dict(task_id=left,dispatch_id=json.loads(disp.content[0].text)["dispatch_id"],
                              session_id="original-worker")
                self.assertFalse((await worker.call_tool("task_claim",dict(request_id="claim",**fields))).isError)
                with closing(manage.registry(root)) as db, db:
                    db.execute("DELETE FROM connections WHERE name='lead-collab'")
                self.assertTrue((await a.call_tool("memory_source", source)).isError)
                delivered = await worker.call_tool("task_submit",dict(request_id="deliver",**fields,
                    self_check="已核查",results=[dict(criterion_id="check",summary="完成")]))
                self.assertFalse(delivered.isError,delivered.content)
                self.assertTrue((await b.call_tool("task_get",{"task_id":left})).isError)
                # Re-authorize the same owner in a fresh MCP process.
                fresh = await stack.enter_async_context(connect(root,"甲","lead","collab","replacement"))
                result=await fresh.call_tool("task_get",{"task_id":left})
                self.assertEqual(json.loads(result.content[0].text)["status"],"pending_review")
