# PanMesh

English · [简体中文](README.zh-CN.md)

Give your AI workspaces a shared task ledger and a memory you control. Ask Codex to lead a report, have Cursor check its citations, and bring the findings back to Codex for review. Later, save the conversation where you corrected the citation rule and give the next project just the approved rule.

## Quick start

The documented setup uses **Windows, Git and Python 3.11**. Obsidian is optional.

1. Give the [installation guide](INSTALL-AGENT.md) to an assistant that can operate your machine. Try: “Install PanMesh for my Papers project. Connect Codex as lead and Cursor as reviewer. Verify both connections.”
2. Give the [collaboration guide](COLLABORATE.md) to your lead chat. Try: “Have Cursor in the Papers workspace check every citation using the model I selected. Review its evidence and send back only the entries that need fixing.”
3. Decide what to remember. Try: “Save the exchange starting with my correction about page numbers. Keep our exact wording. Propose a reusable rule separately.”

The lead needs access to the target window or a supported execution adapter to deliver a notification. Opening a task in the ledger alone does not wake a chat.

## Lead several AI clients from one conversation

If you already tell GPT to split work among chats and review the results, use the same approach across clients. Choose a GPT lead chat that can reach the local project and notify the target windows, such as Codex. Give it the collaboration guide and a prompt like this:

> Lead this report. Ask Cursor in the Papers workspace to check every citation using my selected model. Have Claude Code check the conclusions independently. Review both sets of evidence, send back only the items that need fixing, and give me the final result. I'll choose which messages to save later.

The lead records two tasks with separate acceptance criteria. It uses the configured routes to notify existing windows or start a supported background runner. Each worker submits evidence and open questions; the lead checks the sources and accepts or returns each item. With notification configured, you can stay in the lead chat. If a client requires approval or the requested model is unavailable, the lead reports the specific blocker.

## Client support

The project has tested **Codex, Claude Code, Cursor, Antigravity, DSH and WorkBuddy**. Available connection methods, model selection, window notification and background execution depend on each client's configuration and adapter. **Hermes** is among the clients planned for future testing.

## Advantages

- **Coordinate the AI windows you already use.** Choose the project and workers in your lead chat. The lead splits the work, notifies the workers and reviews their submissions. You can stay in one window without relaying task IDs or copying results between chats.
- **Skip repeated explanations.** A worker picking up a task can read the project's approved method summary alongside the current materials and acceptance criteria. A citation rule you clarified last week can carry straight into the next review.
- **Save premium model quota.** Give difficult planning decisions and disputed findings to a strong reasoning model. Delegate bulk extraction and formatting to a model with quota to spare.
- **Change the lineup when quotas or deals change.** If a subscription limit changes, a provider offers a discount or a new model handles one step better, reassign that step. The project's methods and review history carry over.
- **Draw on each client’s ecosystem.** Send a step to Claude Code when it has the right skill. If Cursor already has the MCP connection for checking a source, let it collect the evidence and return it to the lead for review.
- **Keep editable summaries and cases linked to original records.** Revise a method summary, explain a decision in the case notes and follow a link back to the selected messages in their original order. You can share the summary while keeping the source conversation within its project.
- **Study how you work with AI.** Compare exchanges you chose to save. Which requirements do you tend to add only after the first draft? Which questions got you verifiable evidence on the first try? Reviewing your own words alongside the results helps you spot habits and carry effective methods into your next project.

## How it works

| Component | Responsibility |
| --- | --- |
| Project connection | A local Python service exposes tools over MCP stdio. Each connection has a project, role and access mode. |
| Shared memory | Selected originals live in `imports/` and editable Markdown in `vault/`. SQLite tracks permissions, versions, source references and trials. A method summary can be shared independently of its source conversation. |
| Task ledger | SQLite records dispatch, claims, evidence, per-criterion review, retries and usage. Repeated requests are deduplicated. An uncertain delivery is reconciled before resending. |
| Execution | Existing Codex, Cursor and Claude Code project chats can claim work. Supported CLI adapters can start a background session and persist its result. Claude Desktop's Code tab can use a configured Gateway. |

**Parallel projects have isolated memory.** Multiple projects can use the same PanMesh installation at the same time. Each MCP connection is bound to a project and role. Even when two projects use the same role name and source identifier, their conversations and tasks remain separate. You can explicitly share a method summary with another project while keeping the underlying conversation private. The [three-project concurrency test](tests/test_project_mcp.py) checks that each project can retrieve its own records and cannot retrieve the other two projects’ records.

## Choose a mode for each workspace

| Mode | What the connection can do | When to use it |
| --- | --- | --- |
| Capture only (`capture`) | Append selected messages, excerpts or material references to the project and receive a receipt. This connection has no search or dispatch tools. | Keep a discussion for later review. |
| Collaboration (`collab`) | Retrieve approved summaries, follow authorized sources, propose revisions, and assign, claim, submit or review tasks. | Lead a project, contribute work or reuse an approved method. |

Choose per project and client. For example, Codex and Cursor can collaborate while an interview workspace only captures selected passages. Native memory follows the client's own settings. PanMesh saves personal material you select; automatic capture requires separate session enrollment. MCP grants govern service access. Direct filesystem access follows OS permissions.

## How does a conversation become a reusable method?

Suppose an assistant reports a number from an abstract as if it had checked the original data. Ask it to save the exchange from your correction through the revised answer. Later, use a collaboration chat to turn the first answer, your reasoning and the accepted result into a case with links back to those turns. Approve a separate method such as “Check the original table before reporting a number.” Give the next worker the approved summary and grant access to the exchange only if needed. Earlier tasks still point to the version they used.

To add a harness, start with project MCP or the claim-and-submit workflow. To add a model runner, adapt session startup, receipts and usage reporting in `connectors/`, then register the route in `routing.py`. Handoff prompts, review criteria, note templates and sharing boundaries can all be customized.

## Documentation

[Installation and configuration](INSTALL-AGENT.md) · [Guide for the lead chat](COLLABORATE.md) · [Memory roadmap](docs/MEMORY-ROADMAP.md) · [When to consider vector search and a dedicated database](docs/MEMORY-ROADMAP.md#when-to-consider-vector-search)

Some runtime messages, vault templates and the [detailed ledger reference (Chinese)](ledger_api.md) are currently in Chinese. The installation and collaboration guides are available in both languages.

[MIT license](LICENSE). The failure classification module adapts work from [multi-agent-collaboration](https://github.com/Gambler134201/multi-agent-collaboration) under its [upstream license](ledger_vendor/LICENSE).

Created and maintained by [Pan-Shufeng](https://github.com/Pan-Shufeng). [Releases](https://github.com/Pan-Shufeng/PanMesh/releases) · [Report a problem](https://github.com/Pan-Shufeng/PanMesh/issues/new/choose)
