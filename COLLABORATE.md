# Coordinate work across AI clients

English · [简体中文](COLLABORATE.zh-CN.md)

Give this file to the lead chat together with the task, target workspace, desired model and acceptance criteria. The lead owns delivery, follow-up and review. Use the PanMesh installation path recorded by the installer.

## Check access

Inspect the selected workspace's project configuration: Codex uses `.codex/config.toml`, Cursor uses `.cursor/mcp.json`, and Claude Code uses `.mcp.json`. For Desktop Code, check the workspace actually open in the chat. Match the service connection name to its registered project, role and mode without revealing secrets. Check aliases and inherited entries where relevant.

Report project configuration and live connectivity separately. Listing the service tools in the target chat verifies what that chat can use. Capture mode exposes only `memory_capture`; collaboration mode also offers read tools such as `task_list`. A local `project_client.py list` check alone cannot establish live chat connectivity. A status request is read-only.

To change mode, enroll or reuse a matching project/role connection. Back up the configuration, replace only the verified service binding and recheck after reloading the client. To disable access in a project, remove its service entry. To revoke immediately, check whether the credential is shared before using `manage.py revoke CONNECTION`. If requested, stop separately enrolled capture hooks using `capture_hook.py status` and `unselect`. Preserve stored data and native memory settings.

## Dispatch and review

1. Verify the target workspace path and project identity. Use the [installation guide](INSTALL-AGENT.md) if setup is missing. For cross-project jobs, share only the authorized handoff through a dedicated collaboration project unless the user explicitly grants broader access.
2. Record the goal, authorized material, deliverable and acceptance criteria. Use `run_routes` to identify the worker and channel. Honor the requested model and verify the selection in the client or adapter. If unavailable, report the choices before substituting.
3. For a **pull** route, use `task_dispatch` and send a short notification to the correct project chat: “Claim task ID using SERVICE, complete the outstanding criteria and submit evidence with task_submit.” The lead delivers this reminder. Verify the message or claim. For a supported **push** route, register the round and call `run_start` directly; the coordinator manages the send reservation. Retain the run ID.
4. Check the ledger periodically while progressing independent work. If delivery is uncertain, check the original session and receipt before retrying. If the target is unreachable or approval is required, report the actual state.
5. Review the artifact or evidence yourself. Use `task_review` to accept, return or defer each criterion. Reassign only outstanding items. Report the result, the observed model selection and anything still unresolved.

## Save only what the user selects

The ledger holds operational handoffs and review evidence. Capture a personal conversation or file into long-term memory only when the user selects it. Preserve visible wording, speaker, sequence and any gaps. Keep interpretations, case notes and proposed methods separate, link them to their sources and let the user decide what to approve or share.
