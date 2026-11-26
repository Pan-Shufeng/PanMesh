# Memory roadmap

[English](MEMORY-ROADMAP.md) · [简体中文](MEMORY-ROADMAP.zh-CN.md)

1. **Find the original exchange.** Add full-text search over authorized turns and filters for project, speaker and date.
2. **Understand a correction.** Compare the first answer, feedback, revision and accepted result side by side. Keep interpretations linked to evidence and exceptions.
3. **Measure how much context is enough.** Compare brief, versioned handoffs with full context on the same tasks. Track tokens, missed errors and rework.
4. **Evaluate reusable methods.** Compare outcomes across versions and models, retain failed trials and export proven methods as editable skill drafts.
5. **Make memory portable.** Preview what a recipient can see, flag dependent notes after revocation and verify source links after restoring a project.

## When to consider vector search

Start with real questions where keyword search misses a known exchange. Compare full-text search with a local hybrid index on those questions: does the correct source appear in the first five results, and how many misleading matches appear alongside it? Consider a dedicated vector database when measured latency, concurrent updates or multi-device access justify operating a service. Indexes should be rebuildable, respect grants before retrieval and reflect revoked access.
