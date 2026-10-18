# Demo script (2:45)

Title card: Rationale Guard. One sentence on screen: "Before a change merges, which past decision does it break?"

**0:00 – 0:20. The harmless diff.**
Open the Guard screen. Health banner is green: `rationale__paperclipai__paperclip`. Click the sample "Risky: Treat unknown adapter cost as zero." The diff is `usage.cost ?? null` becoming `usage.cost ?? 0`. Say: "A coding agent will do this to satisfy a type. The file does not say why null was there."

**0:20 – 0:50. The comment the bot would post.**
Hit "Guard this change." In under a second the card says contradiction. Read the verbatim quote from PR #14991: "Preserve explicit zero prices and missing prices as distinct states." Premise holds, rule `R1_linked_issue_still_open`: issue #14548 is still open. Ask `@devinfoley`, PageRank, bus factor 1. Point at the status line: graph step, single-digit milliseconds, judge `offline` or `llm` labelled on the card.

**0:50 – 1:10. The path and the Cypher.**
Open Cypher. Show the query that matched `File ← TOUCHES ← PR ← DERIVED_FROM ← Decision` for `packages/adapter-utils/src/billing.ts`, and the R1 query that read the issue state. The reasoning-path chips are those nodes. Say: "The model did not retrieve a paragraph. The graph returned this path. The judge only labelled it."

**1:10 – 1:35. It remembers.**
Click Override. Type `billing now normalizes upstream`. The "What Rationale learned" list gains a correction. Run the same diff again. The old decision is gone from the warnings. The summary says it learned the override. Optional cut to FalkorDB Browser: `(new:Decision)-[:SUPERSEDES]->(old:Decision)` and a `Correction` node.

**1:35 – 2:00. The agent calls the tool.**
Switch to Cursor. `mcp.json` is on screen for two seconds, stdio command `python3 -m rationale.mcp_server`. The agent calls `guard_diff` on the same risky diff, gets the same decision id, and rewrites the line so unknown stays null. Say: "This is the action. The agent asks the graph before it edits."

**2:00 – 2:25. Someone else's PR.**
Back to the web. Paste a public PR URL from a different repository. The progress events tick: files, history, at most 30 PRs, then a report or an honest "no past decision touches these files." The graph list now contains a second `rationale__*` name. Say: "We do not write to that repository. We only read the public API."

**2:25 – 2:45. Numbers and stop.**
Cut to the eval table: recall 6/6, false positives 0/6, graph p50 about 6 ms. One sentence on the three tracks: the guard acts, the graph is the company memory, the override is what it learned. End on the pitch line. Do not show a chat box. Do not show a second product.
