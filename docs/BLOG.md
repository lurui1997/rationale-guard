# My coding agent almost undid a decision it never knew existed

The pull request looked harmless. In `packages/adapter-utils/src/billing.ts` someone, or something, changed one expression:

```ts
const price = usage.cost ?? null;
const price = usage.cost ?? 0; // treat unknown as zero
```

A reviewer skimming the diff sees a null made concrete. A coding agent sees a type error going away. Neither is looking at issue [#14548](https://github.com/paperclipai/paperclip/issues/14548) on [paperclipai/paperclip](https://github.com/paperclipai/paperclip), which is still open, and which says the cost API returns `costCents: 0` with no way to tell "the cost is zero" from "we do not know the cost." They are also not looking at PR [#14991](https://github.com/paperclipai/paperclip/pull/14991), merged on 7 October 2026, whose body says, verbatim:

> Preserve explicit zero prices and missing prices as distinct states, including later unpriced Gemini usage.

That sentence is the decision. The one-line diff contradicts it. The premise is still live, because #14548 was referenced, not closed. The person to ask is the human who actually edits those files.

Rationale Guard is the small system that says this before the change merges. This post is about why that sentence has to come from a graph.

## The failure mode is not "the model forgot"

Agents are good at the file in front of them. They are bad at organizational memory, and the usual fix — embed the docs, retrieve the nearest paragraphs — does not match the question a diff asks.

A diff has paths and lines. It does not have a question. "Is this like the cost discussion?" is the wrong question. The right question is structural:

Which past decision was implemented by a change to this file? Does that decision still rest on an open issue, a pending ADR, a dependency that never merged, or a commit that was reverted? What did the authors refuse to do instead? Who still works here?

Those are edges with state on them. An embedding of "unknown cost" will surface #14548 and also a dozen unrelated pricing comments. It will not tell you that the link from #14991 is `Refs`, not `Fixes`, and that the issue state is `OPEN` today. It will not drop the decision after someone writes "billing now normalizes upstream" and have the next run say so. State changes. Similarity does not.

## What we stored

FalkorDB holds one graph per repository. The paperclip seed is `rationale__paperclipai__paperclip`. The node types are the ones the guard actually walks: `Decision`, `PR`, `Issue`, `File`, `Person`, `Premise`, `Alternative`, `Correction`, plus `Commit`, `Doc`, and `Area` so the path has somewhere to land.

Structural edges come from rules, not from a model. A closing keyword creates `FIXES`, and a checklist line that merely contains the words "Fixes #" does not. `Refs #14548` creates `REFERENCES`. A squash subject ending in `(#14991)` is how history walk finds the PR. Bot logins ending in `[bot]` stay in the graph for audit and stay out of "who to ask."

Only the decision text is extracted, and the quote has to be a substring of the artifact. The seed in the repository was produced by the project's own ingester in offline mode, because that mode is deterministic and the quotes can be checked by eye. The same function calls an OpenAI-compatible endpoint when `DEEPSEEK_API_KEY` is set. DeepSeek gets `response_format: {type: json_object}` — not `json_schema` — and list fields that come back as strings are normalized. If the quote is not in the source, the decision is dropped. The model name sent on the wire is `LLM_MODEL`, default `deepseek-chat`, never an internal tag like `offline`.

Two quotes the offline scorer kept, both verbatim:

- From #14991: "Preserve explicit zero prices and missing prices as distinct states, including later unpriced Gemini usage."
- From the MCP ADR: "they do not provide a single default-deny policy engine…" and "Designs must make default deny, approval wait, denied direct call… obvious." The ADR status line is still "pending SecurityEngineer and UXDesigner validation gates."

PR #15614, "stop polling five empty delivery queues," is closed and not merged. Its decision is stored with status `rejected` because that is what the GitHub payload says, not because someone drew an edge by hand.

## The guard is a query, then a label

For a diff, the graph step is a handful of parameterized queries. The first one is the whole product:

```cypher
MATCH (f:File) WHERE f.path IN $paths
MATCH (pr:PR)-[:TOUCHES]->(f)
MATCH (d:Decision)-[:DERIVED_FROM]->(pr)
WHERE coalesce(d.status, 'accepted') <> 'superseded'
RETURN d.id, d.source_quote, d.summary, f.path, pr.number
```

Further hops follow `FIXES` and `REFERENCES` into issues, and `ADDS` into docs, so a decision written in an ADR still meets a diff that touches a file from the PR that added the ADR. A symbol hop covers the case where the path is new but the identifiers are old.

Then the premise rules run, still in Cypher. R1 has two shapes, and the difference matters. If a merged PR says `Fixes #N` and the issue is open, the "we fixed it" premise is stale. If it only says `Refs #N` and the issue is open, the premise holds: the reason for the decision is still sitting there. R3 reads the ADR status. R4 follows `SUPERSEDES`. R7 follows `REVERTS`. R6 calls an author inactive only when GitHub public events say so. A quiet slice of ingested pull requests is `unknown`, not proof they left.

Rejected alternatives are `Alternative` nodes with `REJECTED_FOR`. Experts are not a leaderboard page. We materialize `COLLABORATED` between humans who edit the same file, then:

```cypher
CALL algo.pageRank('Person', 'COLLABORATED')
YIELD node, score
RETURN node.login, score
```

Betweenness is called the same way. The names returned are intersected with the people who actually edit the files in the diff. On the cost files that is `@devinfoley`, and the card says the bus factor is 1, because it is.

Only after that list exists does a judge run. The offline judge is a polarity check: a quote that keeps unknown and zero distinct, plus a diff that writes `?? 0`, is a contradiction. The LLM judge receives the quote and the hunk and must return `contradicts`, `related`, or `unrelated`. Unrelated candidates are dropped. If you asked for the LLM and the call fails, the request fails. The offline judge is not a silent backup. Eval with `--judge llm` exits non-zero if any case did not get an LLM verdict. That rule exists because a prototype of this idea once cached "the graph has no decision" while the database was down, and then served the cached miss after the database came back.

On the twelve labelled diffs in `eval/cases.yaml`, the offline judge's recall is 6/6 and the false-positive count is 0/6. The graph step's median is about 6 milliseconds. The 1 second budget is not close.

## Memory is a node, or it is a slogan

The brief asks for an agent that remembers what it learns. A 24 hour cache is not that. Rationale Guard writes a `Correction`:

```cypher
MERGE (c:Correction {id: $id})
SET c.kind = $kind, c.text = $text, c.by = $by, c.at = $at
MERGE (c)-[:ABOUT]->(d:Decision {id: $decision})
```

`ack` leaves the finding in place and marks it acknowledged. `wrong` multiplies that decision's match score by 0.25 so it falls out of the top five when there is anything else to show. `override` sets the old decision to `superseded` and creates a new decision whose summary is the reason you typed, with `SUPERSEDES` pointing at the old one. The next guard run does not warn on the old quote. It says it learned the override. The correction survives a process restart because it is in FalkorDB, on a volume if you used Compose.

You can send the same correction from the web buttons, from `record_feedback` on MCP, or by commenting `/rationale override: billing now normalizes upstream` on a PR where the GitHub Action is installed. The Action updates one comment, the one that contains `<!-- rationale-guard -->`, and posts a neutral check. It does not fail the build unless you choose to require the check. It also never posts to a repository you do not own. The paperclip data is read-only.

## What FalkorDB was for

We did not need a graph database to store JSON. We needed one place that can do all of the following without a second system:

- a separate graph per repository, so a judge can paste their own PR URL and not land in someone else's company brain
- multi-hop matches with parameters, fast enough to run on every diff
- a fulltext index for the secondary "why" question, next to the structural index
- PageRank and betweenness as procedures, not as a library we would have to keep in sync with the edges

`FALKORDB_URL` takes both `redis://` and `rediss://`. The free Cloud tier, in our experience, answers plaintext and hangs on TLS, so the client does not "helpfully" upgrade the scheme. Locally the same URL points at `docker compose` or at `redis-server --loadmodule falkordb.so`. The app's health check is a real `count(n)`, and a connection error is a 503, not an empty answer.

## What this does not do

It does not know your company. The seed is three areas of one public repo, built from nine pull requests, one open issue, and one ADR. The offline extractor keeps sentences with decision-shaped cues; it will miss a decision that was only spoken, and it will keep a sentence that is a constraint but not a capital-D Decision. The LLM path is stricter about being a choice among alternatives, and it still depends on the quote being real.

It does not block the merge. It comments. An agent that calls `guard_diff` from MCP can refuse to write `?? 0` itself, which is the action, but the GitHub check is neutral on purpose.

It will not invent a rejected alternative to make the demo prettier. #15614 is in the graph because it was closed without merging and its body is explicit. Where a sentence says "instead of" or "alternatives considered… both were rejected," that sentence is the alternative. Where the text does not say it, there is no edge.

The interesting failure is still the one-line diff. The agent was not wrong about JavaScript. It was wrong about a decision it was never given. The graph is how you give it to the agent without trusting the agent to remember.
