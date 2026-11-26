# Install and maintain PanMesh

English · [简体中文](INSTALL-AGENT.zh-CN.md)

For the assistant doing the installation: confirm the install location, existing project directory, participating clients, roles and requested access modes. Inspect existing bindings before editing a project. The user can designate this chat as maintainer and later hand that role to another chat.

## Install on Windows

With Git and Python 3.11 installed, clone into a new directory. Use the user's chosen location in place of the example.

```powershell
git clone https://github.com/Pan-Shufeng/PanMesh.git D:\PanMesh
cd D:\PanMesh
py -3.11 -m venv runtime/python
$py = ".\runtime\python\Scripts\python.exe"
& $py -m pip install -r requirements.txt
```

## Connect a project

The example assumes an existing D:\Papers workspace. Run the commands from the PanMesh install directory. Choose unique connection names for each project. Reuse matching bindings on subsequent runs.

```powershell
& $py manage.py enroll papers-main papers codex collab
& $py manage.py enroll papers-worker papers cursor collab
& $py manage.py link-project papers-main codex "D:\Papers"
& $py manage.py link-project papers-worker cursor "D:\Papers"
& $py manage.py route papers-main cursor papers-worker cursor "D:\Papers" --channel pull
& $py project_client.py list papers-main
```

Use client name `claude` for Claude Code project configuration and `claude-desktop` for a Desktop Code window's pull route. For capture-only access, enroll with `capture` and skip route setup. Configure only selected projects. Preserve unrelated MCP entries.

Reload the relevant project chats. Verify a collaboration connection with a read-only `task_list`; a capture-only connection should expose only `memory_capture`. Report local service availability, configuration and actual in-chat connectivity separately. If authorized, run a small synthetic handoff and verify the receipt.

Give the user the [lead-chat guide](COLLABORATE.md). Record install and workspace paths, connection names, roles, routes and unfinished task IDs for a future maintainer. Keep credentials and user data private. Open `vault/` in Obsidian if desired. Use `manage.py backup` for a data snapshot and `manage.py restore SNAPSHOT NEW_DIRECTORY` to restore; re-enroll connections afterward.
