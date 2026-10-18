import { useEffect, useMemo, useState } from "react";

async function readError(response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text);
    if (typeof body.detail === "string") return body.detail;
    if (body.detail && body.detail.error) return body.detail.error;
    return JSON.stringify(body.detail || body);
  } catch {
    return text || `HTTP ${response.status}`;
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

  async function refreshHealth() {
    try {
      const response = await fetch("/api/health");
      if (!response.ok) {
        setHealth(null);
        setHealthError(await readError(response));
        return;
      }
      setHealth(await response.json());
      setHealthError("");
    } catch (err) {
      setHealth(null);
      setHealthError(err.message || "Graph offline");
    }
  }

  useEffect(() => {
    refreshHealth();
    const timer = setInterval(refreshHealth, 8000);
    fetch("/api/samples").then((response) => response.json()).then((body) => setSamples(body.samples || [])).catch(() => setSamples([]));
    refreshLearned();
    return () => clearInterval(timer);
  }, []);

  async function refreshLearned() {
    const response = await fetch("/api/learned");
    if (!response.ok) return;
    const body = await response.json();
    setLearned(body.learned || []);
  }

  const offline = !health || health.falkordb !== true;

  async function runGuard(nextDiff, nextUrl) {
    setBusy(true);
    setError("");
    setWhy(null);
    setReport(null);
    try {
      const response = await fetch("/api/guard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo: "paperclipai/paperclip",
          diff: nextUrl ? "" : nextDiff,
          pr_url: nextUrl || undefined,
          judge: "auto",
        }),
      });
      if (!response.ok) {
        setError(await readError(response));
        return;
      }
      setReport(await response.json());
      refreshLearned();
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
      body: JSON.stringify({ repo: "paperclipai/paperclip", decision_id: decisionId }),
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
    setError("");
    const response = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo: "paperclipai/paperclip", decision_id: decisionId, kind, text, by: "you" }),
    });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    await refreshLearned();
    if (diff.trim()) await runGuard(diff, "");
  }

  const trace = useMemo(() => report?.cypher_trace || [], [report]);

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
          <strong>{offline ? "Graph offline" : "Graph online"}</strong>
          <span>{health ? `${health.graph} · ${health.nodes} nodes` : healthError || "Checking FalkorDB"}</span>
        </div>
      </header>
      {offline && <div className="banner">Graph offline — retrying. Submit stays disabled until FalkorDB answers a real count query.</div>}
      {error && <div className="banner">{error}</div>}
      <div className="layout">
        <section className="panel">
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
                onClick={() => { setActiveSample(sample.id); setDiff(sample.diff); setPrUrl(""); }}
              >
                {sample.label === "violates" ? "Risky" : "Benign"}: {sample.title}
              </button>
            ))}
          </div>
          <button className="primary" disabled={offline || busy || (!diff.trim() && !prUrl.trim())} onClick={() => runGuard(diff, prUrl.trim())}>
            {busy ? "Guarding…" : "Guard this change"}
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
                  {openTrace === finding.decision_id && <pre>{trace.map((step) => `-- ${step.name || "query"} (${step.ms} ms)\n${step.cypher}`).join("\n\n")}</pre>}
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
          <p className="meta">{(health?.graphs || []).join(", ") || "—"}</p>
        </aside>
      </div>
    </div>
  );
}
