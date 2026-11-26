# PanMesh

English · [简体中文](README.zh-CN.md)

Give your AI workspaces a shared task ledger and a memory you control. Ask Codex to lead a report, have Cursor check its citations, and bring the findings back to Codex for review. Later, save the conversation where you corrected the citation rule and give the next project just the approved rule.

## Get started

The documented setup uses **Windows, Git and Python 3.11**. Obsidian is optional.

1. Give the [installation guide](INSTALL-AGENT.md) to an assistant that can operate your machine. Try: “Install PanMesh for my Papers project. Connect Codex as lead and Cursor as reviewer. Verify both connections.”
2. Give the [collaboration guide](COLLABORATE.md) to your lead chat. Try: “Have Cursor in the Papers workspace check every citation using the model I selected. Review its evidence and send back only the entries that need fixing.”
3. Decide what to remember. Try: “Save the exchange starting with my correction about page numbers. Keep our exact wording. Propose a reusable rule separately.”

The lead needs access to the target window or a supported execution adapter to deliver a notification. Opening a task in the ledger alone does not wake a chat.

## Why use it?

- **Reserve premium model quota for hard decisions.** Delegate routine extraction and independent checks to another model. Use one window when the task is simple.
- **Spend less context repeating instructions.** A worker receives the goal, relevant material, method version and acceptance criteria. An interrupted job can resume from its recorded state.
- **Change providers as plans change.** Choose the client, model and delivery route per project. Each provider's billing and approval rules still apply.
- **Study how you work.** Follow a case back to the original exchange, compare corrections and keep track of which method worked on a new task.

## How it works

| Component | Responsibility |
| --- | --- |
| Project connection | A local Python service exposes tools over MCP stdio. Each connection has a project, role and access mode. |
| Shared memory | Selected originals live in `imports/` and editable Markdown in `vault/`. SQLite tracks permissions, versions, source references and trials. A method summary can be shared independently of its source conversation. |
| Task ledger | SQLite records dispatch, claims, evidence, per-criterion review, retries and usage. Repeated requests are deduplicated. An uncertain delivery is reconciled before resending. |
| Execution | Existing Codex, Cursor and Claude Code project chats can claim work. Supported CLI adapters can start a background session and persist its result. Claude Desktop's Code tab can use a configured Gateway. |

Choose **capture** for a window that may append selected material, or **collab** for a window that should also retrieve authorized memory and work on tasks. Enabling MCP does not enroll every conversation for capture. Direct filesystem access remains subject to OS permissions.

To add a harness, start with project MCP or the claim-and-submit workflow. To add a model runner, adapt session startup, receipts and usage reporting in `connectors/`, then register the route in `routing.py`. Handoff prompts, review criteria, note templates and sharing boundaries can all be customized.

## Memory roadmap

1. **Find the original exchange.** Add full-text search over authorized turns and filters for project, speaker and date.
2. **Understand a correction.** Compare the first answer, feedback, revision and accepted result side by side. Keep interpretations linked to evidence and exceptions.
3. **Measure how much context is enough.** Compare brief, versioned handoffs with full context on the same tasks. Track tokens, missed errors and rework.
4. **Evaluate reusable methods.** Compare outcomes across versions and models, retain failed trials and export proven methods as editable skill drafts.
5. **Make memory portable.** Preview what a recipient can see, flag dependent notes after revocation and verify source links after restoring a project.

### When to consider vector search

Start with real questions where keyword search misses a known exchange. Compare full-text search with a local hybrid index on those questions: does the correct source appear in the first five results, and how many misleading matches appear alongside it? Consider a dedicated vector database when measured latency, concurrent updates or multi-device access justify operating a service. Indexes should be rebuildable, respect grants before retrieval and reflect revoked access.

Some runtime messages, vault templates and the [detailed ledger reference (Chinese)](ledger_api.md) are currently in Chinese. The installation and collaboration guides are available in both languages.

[MIT license](LICENSE). The failure classification module adapts work from [multi-agent-collaboration](https://github.com/Gambler134201/multi-agent-collaboration) under its [upstream license](ledger_vendor/LICENSE).
