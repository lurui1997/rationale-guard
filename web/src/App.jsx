import { useEffect, useState } from "react";

function hideDbLocation(text) {
  const raw = String(text || "");
  if (/rediss?:\/\/|falkors?:\/\/|falkordb|\b\d{1,3}(?:\.\d{1,3}){3}:\d+|\b(?:localhost|[\w.-]+\.[\w.-]+):\d{2,5}/i.test(raw)) return "graph unavailable";
  if (/redis/i.test(raw) && /:\d{2,5}/.test(raw)) return "graph unavailable";
  return raw;
}

async function readError(response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text);
    if (typeof body.detail === "string") return hideDbLocation(body.detail);
    if (body.detail && body.detail.error) return hideDbLocation(body.detail.error);
    return hideDbLocation(JSON.stringify(body.detail || body));
  } catch {
    return hideDbLocation(text || `HTTP ${response.status}`);
  }
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState("");
  const [samples, setSamples] = useState([]);
  const [diff, setDiff] = useState("");
  const [prUrl, setPrUrl] = useState("");
  const [activeSample, setActiveSample] = useState("");
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [why, setWhy] = useState(null);
  const [learned, setLearned] = useState([]);
  const [openTrace, setOpenTrace] = useState("");
  const [repo, setRepo] = useState("paperclipai/paperclip");
  const [graph, setGraph] = useState("");
  const [lastDiff, setLastDiff] = useState("");
  const [authorName, setAuthorName] = useState("");

  async function refreshHealth(selectedGraph) {
    const target = selectedGraph === undefined ? graph : selectedGraph;
    const params = new URLSearchParams();
    if (target) params.set("graph", target);
    try {
      const response = await fetch(`/api/health${params.toString() ? `?${params}` : ""}`);
      if (!response.ok) {
        setHealth(null);
        setHealthError(await readError(response));
        return;
      }
      setHealth(await response.json());
      setHealthError("");
    } catch (err) {
      setHealth(null);
      setHealthError(hideDbLocation(err.message || "Graph offline"));
    }
  }

  useEffect(() => {
    fetch("/api/samples").then((response) => response.json()).then((body) => setSamples(body.samples || [])).catch(() => setSamples([]));
    refreshLearned("paperclipai/paperclip", "");
  }, []);

  useEffect(() => {
    refreshHealth(graph);
    const timer = setInterval(() => refreshHealth(graph), health?.loading ? 2000 : 8000);
    return () => clearInterval(timer);
  }, [graph, health?.loading]);

  async function refreshLearned(nextRepo, nextGraph) {
    const params = new URLSearchParams({ repo: nextRepo || repo });
    if (nextGraph) params.set("graph", nextGraph);
    const response = await fetch(`/api/learned?${params.toString()}`);
    if (!response.ok) return;
    const body = await response.json();
    setLearned(body.learned || []);
  }

  const offline = !health || health.falkordb !== true;
  const loading = Boolean(health?.loading);

  function rememberReport(body) {
    if (body.repo) setRepo(body.repo);
    if (body.graph) setGraph(body.graph);
    setReport(body);
    refreshLearned(body.repo || repo, body.graph || graph);
  }

  async function runGuard(nextDiff, nextUrl, options = {}) {
    const ingest = options.ingest !== undefined ? options.ingest : Boolean(nextUrl);
    setBusy(true);
    setError("");
    setWhy(null);
    setReport(null);
    try {
      const response = await fetch("/api/guard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo,
          graph: graph || undefined,
          diff: nextUrl ? "" : nextDiff,
          pr_url: nextUrl || undefined,
          ingest,
          judge: "auto",
        }),
      });
      if (!response.ok) {
        setError(await readError(response));
        return;
      }
      const body = await response.json();
      setLastDiff(nextUrl ? "" : (nextDiff || ""));
      rememberReport(body);
      refreshHealth();
    } catch (err) {
      setError(err.message || "Guard failed");
    } finally {
      setBusy(false);
    }
  }

  async function askWhy(decisionId) {
    setError("");
    const response = await fetch("/api/why", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, graph: graph || undefined, decision_id: decisionId }),
    });
    if (!response.ok) {
      setWhy(null);
      setError(await readError(response));
      return;
    }
    setWhy(await response.json());
  }

  async function feedback(decisionId, kind) {
    const text = window.prompt(`Reason for ${kind}`);
    if (!text) return;
    const by = authorName.trim();
    if (!by) {
      setError("Enter your name before ack, override, or wrong. That name is stored on the correction.");
      return;
    }
    setError("");
    const response = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repo,
        graph: graph || undefined,
        decision_id: decisionId,
        kind,
        text,
        by,
      }),
    });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    await refreshLearned(repo, graph);
    if (diff.trim() || lastDiff.trim()) await runGuard(diff.trim() || lastDiff, "", { ingest: false });
    else if (prUrl.trim()) await runGuard("", prUrl.trim(), { ingest: false });
  }

  return (
    <div className="app">
      <header className="top">
        <div>
          <p className="mark">Rationale <span>Guard</span></p>
          <p className="pitch">
            Rationale Guard is a decision guard for coding agents: before a change merges, it shows which past decision it breaks, whether that decision's premise still holds, and who to ask — and it remembers your answer.
          </p>
        </div>
        <div className={offline ? "health off" : "health on"}>
          <strong>{loading ? "Loading seed" : offline ? "Graph offline" : "Graph online"}</strong>
          <span>{health ? `${health.graph} · ${loading ? "loading" : `${health.nodes} nodes · ${health.edges ?? 0} edges`}` : healthError || "Checking FalkorDB"}</span>
        </div>
      </header>
      {loading && <div className="banner">Loading the paperclip seed. The guard starts when that graph is ready.</div>}
      {offline && <div className="banner">Graph offline — retrying. Submit stays disabled until FalkorDB answers a real count query.</div>}
      {error && <div className="banner">{error}</div>}
      <div className="layout">
        <section className="panel">
          <label>Repository graph</label>
          <select
            value={graph}
            onChange={(event) => {
              const next = event.target.value;
              if (!next) {
                setGraph("");
                setRepo("paperclipai/paperclip");
                refreshLearned("paperclipai/paperclip", "");
                return;
              }
              const match = (health?.repos || []).find((item) => item.graph === next);
              setGraph(next);
              if (match?.repo) setRepo(match.repo);
              refreshLearned(match?.repo || repo, next);
            }}
          >
            <option value="">paperclipai/paperclip (seed)</option>
            {(health?.repos || []).filter((item) => item.graph !== "rationale__paperclipai__paperclip" && (item.decisions ?? 0) > 0).map((item) => (
              <option key={item.graph} value={item.graph}>{item.repo || item.graph}</option>
            ))}
          </select>
          <p className="meta">Pasted diffs use {repo}. A pull request URL builds or updates that repository's graph, then guards the diff. Decisions extracted from that same PR are not judged against it.</p>
          <label>Your name</label>
          <input type="text" placeholder="Name recorded on ack, override, and wrong" value={authorName} onChange={(event) => setAuthorName(event.target.value)} />
          <label>Diff or public pull request</label>
          <textarea value={diff} onChange={(event) => { setDiff(event.target.value); setActiveSample(""); setPrUrl(""); }} placeholder="Paste a unified diff" />
          <div className="row">
            <input type="url" placeholder="https://github.com/owner/repo/pull/123" value={prUrl} onChange={(event) => setPrUrl(event.target.value)} />
          </div>
          <div className="row">
            {samples.map((sample) => (
              <button
                key={sample.id}
                className={activeSample === sample.id ? "chip active" : "chip"}
                onClick={() => {
                  setActiveSample(sample.id);
                  setDiff(sample.diff);
                  setPrUrl("");
                  if (sample.repo) {
                    setRepo(sample.repo);
                    setGraph("");
                  }
                }}
              >
                {sample.label === "violates" ? "Risky" : "Benign"}: {sample.title}
              </button>
            ))}
          </div>
          <button className="primary" disabled={offline || loading || busy || (!diff.trim() && !prUrl.trim())} onClick={() => runGuard(diff, prUrl.trim(), { ingest: Boolean(prUrl.trim()) })}>
            {busy ? (prUrl.trim() ? "Building graph and guarding…" : "Guarding…") : "Guard this change"}
          </button>
          {report && (
            <div style={{ marginTop: 18 }}>
              <div className="status">
                <h2 className={report.status === "warn" ? "warn" : "pass"}>{report.status === "warn" ? "Contradiction" : "No contradiction"}</h2>
                <span className="meta">graph {report.graph_ms} ms · judge {report.judge}</span>
              </div>
              <p>{report.summary}</p>
              {(report.findings || []).map((finding) => (
                <article key={finding.decision_id} className="card">
                  <strong>{finding.verdict}</strong> · {finding.summary}
                  <blockquote className="quote">{finding.quote}</blockquote>
                  <p className="meta">{finding.reason}</p>
                  <p className="meta">Premise <b>{finding.premise?.status}</b> {finding.premise?.rules?.map((rule) => rule.id).join(", ")}</p>
                  {finding.rejected_alternatives?.length > 0 && (
                    <p className="meta">Rejected: {finding.rejected_alternatives.map((alt) => alt.description).join(" · ")}</p>
                  )}
                  {finding.ask?.length > 0 && (
                    <p className="meta">Ask {finding.ask.map((person) => `@${person.login} (${person.algorithm})`).join(", ")}</p>
                  )}
                  {finding.learned_from && <p className="meta">Learned from @{finding.learned_from.by}: {finding.learned_from.kind} — {finding.learned_from.text}</p>}
                  <div className="path">
                    {(finding.subgraph?.nodes || []).map((node) => (
                      <div key={node.id} className="node"><small>{node.label}</small>{node.title}</div>
                    ))}
                  </div>
                  <div className="row">
                    <button className="ghost" onClick={() => setOpenTrace(openTrace === finding.decision_id ? "" : finding.decision_id)}>Cypher</button>
                    <button className="ghost" onClick={() => askWhy(finding.decision_id)}>Ask why</button>
                    <button className="ghost" onClick={() => feedback(finding.decision_id, "ack")}>Ack</button>
                    <button className="ghost" onClick={() => feedback(finding.decision_id, "override")}>Override</button>
                    <button className="ghost danger" onClick={() => feedback(finding.decision_id, "wrong")}>Wrong</button>
                  </div>
                  {openTrace === finding.decision_id && (
                    <pre>{(finding.cypher_trace || []).map((step) => `-- ${step.name || "query"} (${step.ms} ms)\n${step.cypher}`).join("\n\n") || "No query was recorded for this finding."}</pre>
                  )}
                  {why && why.decision_id === finding.decision_id && (
                    <div className="why">
                      <p>{why.conclusion}</p>
                      <p className="meta">{(why.chain || []).map((item) => item.id).join(" → ")}</p>
                    </div>
                  )}
                </article>
              ))}
              {report.learned?.length > 0 && (
                <p className="meta">Learned: {report.learned.map((row) => `${row.kind} by @${row.by} on ${row.decision_id}`).join("; ")}</p>
              )}
            </div>
          )}
        </section>
        <aside className="panel side">
          <h3>What Rationale learned</h3>
          {learned.length === 0 && <p className="meta">No corrections yet. Ack, override, or mark a finding wrong and the graph keeps it.</p>}
          {learned.map((row) => (
            <div key={row.id} className="learned">
              <b>{row.kind}</b> by @{row.by}
              <div className="meta">{row.text}</div>
              <div className="meta">{row.decision_id} {row.successor_id ? `→ ${row.successor_id}` : ""}</div>
            </div>
          ))}
          <h3 style={{ marginTop: 22 }}>Active graphs</h3>
          <p className="meta">{(health?.repos || []).map((item) => item.repo || item.graph).join(", ") || "—"}</p>
          <h3 style={{ marginTop: 22 }}>From an agent</h3>
          <p className="meta">MCP tools: guard_diff, why, who_knows, check_premises, record_feedback. Run <code>python3 -m rationale.mcp_server</code> from this repo with the package installed.</p>
          <p className="meta">The GitHub Action posts one sticky comment when RATIONALE_API_URL is set. Only collaborators with write access can <code>/rationale override</code>.</p>
        </aside>
      </div>
    </div>
  );
}
