# Queries the guard actually runs

All statements are parameterized. `$paths`, `$ids`, `$symbols`, and `$cutoff` come from the diff and the server clock, never concatenated into the query text.

## Candidates

Decision implemented by a PR that touches a changed file:

```cypher
MATCH (f:File) WHERE f.path IN $paths
MATCH (pr:PR)-[:TOUCHES]->(f)
MATCH (d:Decision)-[:DERIVED_FROM]->(pr)
WHERE coalesce(d.status, 'accepted') <> 'superseded'
RETURN d.id AS decision_id, d.source_quote AS quote, f.path AS path, pr.number AS via_number
```

The issue hop and the doc hop are the same shape through `FIXES` / `REFERENCES` and `ADDS`. Superseded decisions are excluded here and reported separately from `Correction` nodes.

## Open linked issue

```cypher
MATCH (d:Decision)-[:DERIVED_FROM]->(pr:PR)-[:REFERENCES]->(i:Issue {state: 'OPEN'})
WHERE d.id IN $ids
RETURN d.id AS decision_id, pr.id AS left_id, i.id AS right_id
```

The closing-keyword variant uses `FIXES` and marks the premise stale. The reference variant marks the premise as still holding.

## Who to ask

```cypher
MATCH (p1:Person)-[:EDITED]->(f:File)<-[:EDITED]-(p2:Person)
WHERE p1.is_bot = false AND p2.is_bot = false AND p1.id < p2.id
MERGE (p1)-[:COLLABORATED]->(p2)
MERGE (p2)-[:COLLABORATED]->(p1)
```

```cypher
CALL algo.pageRank('Person', 'COLLABORATED')
YIELD node, score
RETURN node.login AS login, score
```

## Override

```cypher
MATCH (d:Decision {id: $id}) SET d.status = 'superseded'
```

The new decision is `MERGE`d and linked with `SUPERSEDES`. The next candidate query skips `status = 'superseded'`.

## Learned on this diff

```cypher
MATCH (f:File) WHERE f.path IN $paths
MATCH (pr:PR)-[:TOUCHES]->(f)
MATCH (d:Decision {status: 'superseded'})-[:DERIVED_FROM]->(pr)
MATCH (c:Correction)-[:ABOUT]->(d)
OPTIONAL MATCH (n:Decision)-[:SUPERSEDES]->(d)
RETURN d.id, c.kind, c.by, c.text, n.summary
```
