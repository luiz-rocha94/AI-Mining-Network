import { useCallback, useEffect, useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent } from "react";
import { formatAtomic, validateGeneralTaskForm } from "./client.mjs";

type Model = { name: string; digest: string; sizeBytes: number; parameterSize: string; family: string; format: string };
type Skill = { name: string; version: string; contentHash: string; description: string; requiredModels: string[]; verificationStrategy: string };
type SkillDetail = Skill & { instructions: string; manifest: Record<string, unknown> };
type WorkflowSkillNode = { id: string; skillName: string; from: string };
type WorkflowSpec = {
  version: 1;
  nodes: Array<{ id: string; type: "input" } | { id: string; type: "skill"; skillName: string } | { id: string; type: "output" }>;
  edges: Array<{ from: string; to: string }>;
};
type Job = {
  task: { taskId: string; inputCount: number; skillName?: string; skillNames?: string[]; model?: string; workflowHash?: string };
  quote: { quoteId: string; totalAtomic: number; executionRewardAtomic: number; verificationBudgetAtomic: number; protocolFeeAtomic: number; currency: { symbol: string; decimals: number }; expiresAt: string; sampleCount: number; validatorCount: number; verificationPolicy: Record<string, unknown>; failureRules: Record<string, unknown> };
  state: string;
  progress: string;
  error?: string | null;
  benchmark: Record<string, unknown>;
  pricing?: Record<string, unknown>;
  settlement?: { outcome: string; totalChargedAtomic: number; payouts: Array<{ payee: string; amountAtomic: number; purpose: string }> } | null;
};
type ResultRecord = { result?: unknown };
type ApiError = { error?: string };

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(url, { ...init, headers });
  const body = await response.json() as T & ApiError;
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

const terminal = new Set(["SETTLED", "REFUNDED", "DISPUTED"]);
const supportedFileExtensions = new Set([".pdf", ".txt", ".md", ".csv", ".json"]);
const maxFileBytes = 10 * 1024 * 1024;
const maxTaskBytes = 25 * 1024 * 1024;

export default function App() {
  const [health, setHealth] = useState<Record<string, unknown> | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [modelsError, setModelsError] = useState("");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selectedSkillName, setSelectedSkillName] = useState("");
  const [selectedSkillDetail, setSelectedSkillDetail] = useState<SkillDetail | null>(null);
  const [skillDetailLoading, setSkillDetailLoading] = useState(false);
  const [workflowNodes, setWorkflowNodes] = useState<WorkflowSkillNode[]>([]);
  const nextNodeNumber = useRef(1);
  const [model, setModel] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [prompt, setPrompt] = useState("");
  const [sampleCount, setSampleCount] = useState(1);
  const [validatorCount, setValidatorCount] = useState(1);
  const [job, setJob] = useState<Job | null>(null);
  const [artifacts, setArtifacts] = useState<Array<{ name: string; url: string }>>([]);
  const [results, setResults] = useState<ResultRecord[]>([]);
  const [loadedTaskId, setLoadedTaskId] = useState("");
  const [rating, setRating] = useState<"like" | "dislike" | "" >("");
  const [ratingBusy, setRatingBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [draggingFiles, setDraggingFiles] = useState(false);
  const dragDepth = useRef(0);
  const quoteRef = useRef<HTMLDivElement>(null);

  const refreshCatalog = useCallback(async () => {
    try {
      const [nextHealth, nextModels, nextSkills] = await Promise.all([
        api<Record<string, unknown>>("/api/health"),
        api<{ available: boolean; models: Model[]; error?: string }>("/api/models"),
        api<Skill[]>("/api/skills"),
      ]);
      setHealth(nextHealth);
      setModels(nextModels.models);
      setModelsError(nextModels.error ?? "");
      setSkills(nextSkills);
      const preferredModel = typeof nextHealth.model === "string" && nextModels.models.some(item => item.name === nextHealth.model)
        ? nextHealth.model
        : nextModels.models[0]?.name ?? "";
      setModel(current => current && nextModels.models.some(item => item.name === current) ? current : preferredModel);
      setSelectedSkillName(current => current && nextSkills.some(item => item.name === current) ? current : nextSkills[0]?.name ?? "");
    } catch (reason) { setError(String(reason)); }
  }, []);

  useEffect(() => { void refreshCatalog(); }, [refreshCatalog]);

  useEffect(() => {
    if (!selectedSkillName) { setSelectedSkillDetail(null); return; }
    let current = true;
    setSelectedSkillDetail(null);
    setSkillDetailLoading(true);
    api<SkillDetail>(`/api/skills/${encodeURIComponent(selectedSkillName)}`)
      .then(detail => { if (current) setSelectedSkillDetail(detail); })
      .catch(reason => { if (current) setError(String(reason)); })
      .finally(() => { if (current) setSkillDetailLoading(false); });
    return () => { current = false; };
  }, [selectedSkillName]);

  useEffect(() => {
    if (!job) return;
    const taskId = job.task.taskId;
    const loadCompletedTask = async () => {
      if (loadedTaskId === taskId) return;
      setLoadedTaskId(taskId);
      try {
        const nextArtifacts = await api<Array<{ name: string; url: string }>>(`/api/tasks/${taskId}/artifacts`);
        setArtifacts(nextArtifacts);
        const resultArtifact = nextArtifacts.find(item => item.name === "results.json");
        if (resultArtifact) setResults(await api<ResultRecord[]>(resultArtifact.url));
      } catch (reason) { setError(String(reason)); }
    };
    if (terminal.has(job.state)) {
      void loadCompletedTask();
      return;
    }
    const timer = window.setInterval(async () => {
      try { setJob(await api<Job>(`/api/tasks/${taskId}`)); }
      catch (reason) { setError(String(reason)); }
    }, 1200);
    return () => window.clearInterval(timer);
  }, [job?.task.taskId, job?.state, loadedTaskId]);

  useEffect(() => {
    if (job?.state === "QUOTED") quoteRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [job?.state, job?.task.taskId]);

  const selectedModel = models.find(item => item.name === model);
  const usedAsInput = new Set(workflowNodes.map(node => node.from).filter(nodeId => nodeId !== "input"));
  const terminalWorkflowNodes = workflowNodes.filter(node => !usedAsInput.has(node.id));
  const workflow: WorkflowSpec = {
    version: 1,
    nodes: [
      { id: "input", type: "input" },
      ...workflowNodes.map(node => ({ id: node.id, type: "skill" as const, skillName: node.skillName })),
      { id: "output", type: "output" },
    ],
    edges: [
      ...workflowNodes.map(node => ({ from: node.from, to: node.id })),
      ...(workflowNodes.length
        ? terminalWorkflowNodes.map(node => ({ from: node.id, to: "output" }))
        : [{ from: "input", to: "output" }]),
    ],
  };
  const isTaskActive = Boolean(job && !terminal.has(job.state));
  const canEditRequest = !busy && !ratingBusy && !isTaskActive;
  const skillInWorkflow = selectedSkillName ? workflowNodes.some(node => node.skillName === selectedSkillName) : false;

  const sourceName = (id: string) => id === "input"
    ? "Input"
    : skills.find(item => item.name === workflowNodes.find(node => node.id === id)?.skillName)?.name ?? "Skill";

  const createQuote = async (event: FormEvent) => {
    event.preventDefault(); setError(""); setRating(""); setArtifacts([]); setResults([]); setLoadedTaskId("");
    const validation = validateGeneralTaskForm({ fileCount: files.length, sampleCount, validatorCount, prompt, model, skills: workflowNodes.map(node => node.skillName) });
    if (validation) { setError(validation); return; }
    setBusy(true);
    try {
      const form = new FormData();
      form.set("prompt", prompt);
      form.set("workflow", JSON.stringify(workflow));
      form.set("skill", workflowNodes[0]?.skillName ?? "");
      form.set("model", model);
      form.set("sampleCount", String(sampleCount));
      form.set("validatorCount", String(validatorCount));
      files.forEach(file => form.append("files", file, file.name));
      setJob(await api<Job>("/api/quote", { method: "POST", body: form }));
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };

  const acceptQuote = async () => {
    if (!job) return;
    setError(""); setBusy(true);
    try { setJob(await api<Job>(`/api/tasks/${job.task.taskId}/accept`, { method: "POST", body: "{}" })); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };

  const submitRating = async (choice: "like" | "dislike") => {
    if (!job || ratingBusy || rating) return;
    setError(""); setRatingBusy(true);
    try {
      await api(`/api/tasks/${job.task.taskId}/rating`, { method: "POST", body: JSON.stringify({ stars: choice === "like" ? 5 : 1 }) });
      setRating(choice);
    } catch (reason) { setError(String(reason)); }
    finally { setRatingBusy(false); }
  };

  const currency = job?.quote.currency.symbol ?? "TESTUSD";
  const format = (amount: number) => job ? `${formatAtomic(amount, job.quote.currency.decimals)} ${currency}` : "";
  const skillEstimates = job && Array.isArray(job.pricing?.skills)
    ? job.pricing.skills as Array<{ nodeId: string; name: string; executionRewardAtomic: number; verificationBudgetAtomic: number; protocolFeeAtomic: number }>
    : [];

  const addFiles = (selection: FileList | null) => {
    if (!canEditRequest) return;
    const chosen = Array.from(selection ?? []);
    if (!chosen.length) return;
    const next = [...files, ...chosen];
    if (next.length > 100) { setError("Attach up to 100 supported files."); return; }
    const unsupported = chosen.find(file => !supportedFileExtensions.has(file.name.match(/\.[^.]+$/)?.[0].toLowerCase() ?? ""));
    if (unsupported) { setError(`${unsupported.name} is not supported. Add a text-based PDF, TXT, Markdown, CSV, or JSON file.`); return; }
    const oversized = chosen.find(file => file.size > maxFileBytes);
    if (oversized) { setError(`${oversized.name} is larger than the 10 MiB per-file limit.`); return; }
    if (next.reduce((total, file) => total + file.size, 0) > maxTaskBytes) { setError("Combined files must be no larger than 25 MiB."); return; }
    setError("");
    setFiles(next);
    const samples = Math.max(1, Math.min(next.length, 30));
    setSampleCount(samples);
    setValidatorCount(Math.min(3, samples));
  };

  const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (!canEditRequest || !Array.from(event.dataTransfer.types).includes("Files")) return;
    dragDepth.current += 1;
    setDraggingFiles(true);
  };

  const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDraggingFiles(false);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth.current = 0;
    setDraggingFiles(false);
    if (canEditRequest) addFiles(event.dataTransfer.files);
  };

  const addSkillNode = () => {
    if (!canEditRequest || !selectedSkillName || skillInWorkflow) return;
    if (workflowNodes.length >= 10) { setError("A workflow can have up to 10 skill nodes."); return; }
    const id = `skill-${nextNodeNumber.current++}`;
    const from = workflowNodes.at(-1)?.id ?? "input";
    setWorkflowNodes(current => [...current, { id, skillName: selectedSkillName, from }]);
    setError("");
  };

  const removeSkillNode = (nodeId: string) => {
    setWorkflowNodes(current => {
      const removed = current.find(node => node.id === nodeId);
      if (!removed) return current;
      return current.filter(node => node.id !== nodeId).map(node => node.from === nodeId ? { ...node, from: removed.from } : node);
    });
  };

  const connectSkillNode = (nodeId: string, from: string) => {
    setWorkflowNodes(current => {
      const index = current.findIndex(node => node.id === nodeId);
      if (index < 0 || (from !== "input" && !current.slice(0, index).some(node => node.id === from))) return current;
      return current.map(node => node.id === nodeId ? { ...node, from } : node);
    });
  };

  const removeFile = (index: number) => {
    const next = files.filter((_, fileIndex) => fileIndex !== index);
    setFiles(next);
    const samples = Math.max(1, Math.min(next.length || 1, 30));
    setSampleCount(samples);
    setValidatorCount(Math.min(3, samples));
  };

  const handlePromptKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return <main className="shell app-shell">
    <header className="topbar">
      <a className="brand" href="#top"><span className="brand-mark">AM</span><span>AI Mining Network<small>OPEN COMPUTE MARKET</small></span></a>
      <div className="network-chip"><span className={`dot ${health?.ollama === "ready" ? "ready" : ""}`} />
        {health?.ollama === "ready" ? `Ollama · ${models.length} models` : "Ollama not ready"}
      </div>
    </header>

    <section className="conversation" aria-live="polite">
      {!job && <div className="workflow-intro"><h1>Build a workflow</h1><p>Start with the task input, connect skill nodes, and review the fixed price before running it.</p></div>}
      {job && <div className="assistant-message" ref={job.state === "QUOTED" ? quoteRef : null}>
        <div className="assistant-avatar">AM</div>
        <div className="assistant-content">
          <div className="message-heading"><strong>{job.state === "QUOTED" ? "Price estimate" : "Workflow output"}</strong><span>{job.state === "QUOTED" ? "Ready for your approval" : job.state}</span></div>
          <p className="message-context">{job.task.skillName ?? "Workflow"} · {job.task.model ?? model} · {files.length ? `${files.length} file${files.length === 1 ? "" : "s"}` : "prompt only"}</p>
          {job.state === "QUOTED" ? <div className="quote-message">
            <div className="quote-amount"><span>Fixed total</span><strong>{format(job.quote.totalAtomic)}</strong><small>Expires {new Date(job.quote.expiresAt).toLocaleTimeString()}</small></div>
            <div className="quote-details">
              <span>Execution <b>{format(job.quote.executionRewardAtomic)}</b></span>
              <span>Verification <b>{format(job.quote.verificationBudgetAtomic)}</b></span>
              <span>Protocol fee <b>{format(job.quote.protocolFeeAtomic)}</b></span>
            </div>
            {skillEstimates.length > 0 && <div className="skill-price-list"><span>Workflow node estimates · included in total</span>{skillEstimates.map(item => <div key={item.nodeId}><span>{item.name}</span><b>{format(item.executionRewardAtomic + item.verificationBudgetAtomic + item.protocolFeeAtomic)}</b></div>)}</div>}
            <p className="quote-policy">Includes {job.quote.sampleCount} verification samples and {job.quote.validatorCount} validator{job.quote.validatorCount === 1 ? "" : "s"}. Refund on failure. The final charge cannot exceed this quote.</p>
            <div className="quote-actions"><button className="button primary" onClick={() => void acceptQuote()} disabled={busy}>{busy ? "Starting…" : "Approve price & start"}<span>→</span></button><button className="text-button" onClick={() => setJob(null)} disabled={busy}>Edit workflow</button></div>
            <details className="small-disclosure"><summary>Pricing and verification details</summary><p>{String(job.pricing?.explanation ?? "This local fixed estimate is not a production provider offer.")}</p><p>Replay checks sampled output consistency and commitment inclusion. It does not prove factual correctness; a replay mismatch is inconclusive and follows the refund policy.</p></details>
          </div> : <>
            {!terminal.has(job.state) && <div className="task-progress running"><i className="spinner" /><span>{job.progress}</span></div>}
            {job.error && <div className="error-box">{job.error}</div>}
            {terminal.has(job.state) && results.length > 0 && <div className="result-output"><div className="output-heading"><strong>Final output</strong></div>
              <pre>{JSON.stringify(results.length === 1 ? results[0].result : results.map(item => item.result), null, 2)}</pre>
            </div>}
            {terminal.has(job.state) && results.length === 0 && !job.error && <div className="result-output"><div className="output-heading"><strong>Final output</strong></div><p className="empty-output">No output was returned by the workflow.</p></div>}
            {terminal.has(job.state) && artifacts.some(item => item.name === "results.xlsx") && <div className="result-links"><a href={artifacts.find(item => item.name === "results.xlsx")?.url} download>Download spreadsheet ↓</a></div>}
            {terminal.has(job.state) && job.settlement && <div className="reaction-row" aria-label="Rate this workflow output">
              <span>Was this output helpful?</span>
              <button className={`reaction-button ${rating === "like" ? "selected" : ""}`} type="button" onClick={() => void submitRating("like")} disabled={Boolean(rating) || ratingBusy} aria-label="Like this output" aria-pressed={rating === "like"} title="Like"><span aria-hidden="true">👍</span></button>
              <button className={`reaction-button ${rating === "dislike" ? "selected" : ""}`} type="button" onClick={() => void submitRating("dislike")} disabled={Boolean(rating) || ratingBusy} aria-label="Dislike this output" aria-pressed={rating === "dislike"} title="Dislike"><span aria-hidden="true">👎</span></button>
              {rating && <small>Thanks for your feedback</small>}
            </div>}
          </>}
        </div>
      </div>}
    </section>

    <div className="workflow-workspace">
      <form className="workflow-canvas" onSubmit={createQuote}>
        <div className="workflow-heading"><div><h2>Workflow</h2><span>{workflowNodes.length} skill node{workflowNodes.length === 1 ? "" : "s"} · connected graph</span></div><button className="refresh-button" type="button" onClick={() => void refreshCatalog()} disabled={!canEditRequest}>Refresh nodes</button></div>
        {error && <div className="composer-error" role="alert">{error}</div>}
        {modelsError && <div className="composer-error" role="alert">Ollama model catalog unavailable. {modelsError} Start Ollama and install a model with <code>ollama pull qwen3:4b-instruct</code>.</div>}

        <div className={`workflow-node input-node ${draggingFiles ? "composer-dragging" : ""}`} onDragEnter={handleDragEnter} onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = canEditRequest ? "copy" : "none"; }} onDragLeave={handleDragLeave} onDrop={handleDrop}>
          {draggingFiles && <div className="drop-overlay"><strong>Drop files to attach</strong><span>Files stay private in this local demo</span></div>}
          <div className="workflow-node-header"><span className="node-icon">01</span><div><strong>Input</strong><small>Prompt, files and model</small></div><span className="node-kind">START</span></div>
          {files.length > 0 && <div className="attachment-list">{files.map((file, index) => <span className="attachment-chip" key={`${file.name}-${index}`} title={file.name}><span>▤</span><span className="attachment-name">{file.name}</span><button type="button" onClick={() => removeFile(index)} disabled={!canEditRequest} aria-label={`Remove ${file.name}`}>×</button></span>)}</div>}
          <textarea rows={3} maxLength={20000} value={prompt} onChange={event => setPrompt(event.target.value)} onKeyDown={handlePromptKeyDown} placeholder="Describe what you want the workflow to do…" disabled={!canEditRequest} aria-label="Task prompt" />
          <div className="input-node-toolbar">
            <label className={`composer-select model-select ${!canEditRequest ? "disabled" : ""}`} title="Choose an Ollama model">
              <span>◉</span><select aria-label="Ollama model" value={model} onChange={event => setModel(event.target.value)} disabled={!canEditRequest || !models.length}>
                {models.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
              </select><span className="chevron">⌄</span>
            </label>
            <label className={`attach-button ${!canEditRequest ? "disabled" : ""}`} title="Add files">
              <input type="file" multiple accept=".pdf,.txt,.md,.csv,.json" onChange={event => { addFiles(event.target.files); event.target.value = ""; }} disabled={!canEditRequest} />
              <span>＋</span><span>Add files</span>
            </label>
            <button className="button primary calculate-button" type="submit" disabled={!canEditRequest || !models.length || !workflowNodes.length}>{busy ? <><i className="spinner" /> Calculating…</> : <>Calculate price <span>→</span></>}</button>
          </div>
          <div className="input-node-hint">Drop supported files here · private input · up to 100 files / 25 MiB</div>
        </div>

        {workflowNodes.map((node, index) => <div className="workflow-step" key={node.id}>
          <div className="workflow-edge-indicator"><span className="edge-arrow">↓</span><span>{sourceName(node.from)} connects to</span></div>
          <article className={`workflow-node skill-node ${selectedSkillName === node.skillName ? "focused-node" : ""}`}>
            <div className="workflow-node-header">
              <span className="node-icon skill-icon">{String(index + 2).padStart(2, "0")}</span>
              <button type="button" className="node-name-button" onClick={() => setSelectedSkillName(node.skillName)}><strong>{node.skillName}</strong><small>{skills.find(item => item.name === node.skillName)?.description ?? "Published skill"}</small></button>
              <span className="node-kind">SKILL</span>
              <button className="remove-node" type="button" onClick={() => removeSkillNode(node.id)} disabled={!canEditRequest} aria-label={`Remove ${node.skillName} from workflow`}>×</button>
            </div>
            <label className="node-connect-control"><span>Connect from</span><select aria-label={`Connect ${node.skillName} from`} value={node.from} onChange={event => connectSkillNode(node.id, event.target.value)} disabled={!canEditRequest}>
              <option value="input">Input</option>
              {workflowNodes.slice(0, index).map(source => <option key={source.id} value={source.id}>{source.skillName}</option>)}
            </select></label>
          </article>
        </div>)}

        <div className="workflow-edge-list" aria-label="Connections to final output">
          {terminalWorkflowNodes.length > 0
            ? terminalWorkflowNodes.map(node => <div className="workflow-edge-indicator" key={`${node.id}-output`}><span className="edge-arrow">↓</span><span>{node.skillName} connects to</span></div>)
            : <div className="workflow-edge-indicator"><span className="edge-arrow">↓</span><span>Input connects to</span></div>}
        </div>
        <div className="workflow-node output-node"><div className="workflow-node-header"><span className="node-icon output-icon">{String(workflowNodes.length + 2).padStart(2, "0")}</span><div><strong>Final output</strong><small>{terminalWorkflowNodes.length ? terminalWorkflowNodes.map(node => node.skillName).join(" + ") : "Connect a skill to produce output"}</small></div><span className="node-kind">END</span></div></div>
      </form>

      <aside className="node-library">
        <div className="library-heading"><div><h2>Node library</h2><p>Select a skill to inspect it, then add it to the workflow.</p></div></div>
        <div className="core-node-list"><div><span className="library-node-mark">IN</span><span><strong>Input</strong><small>Prompt, files, model</small></span><em>Fixed</em></div><div><span className="library-node-mark output-mark">OUT</span><span><strong>Final output</strong><small>Workflow result</small></span><em>Fixed</em></div></div>
        <h3>Available skills</h3>
        <div className="skill-library-list">
          {skills.map(skill => <button key={skill.name} type="button" className={`library-skill ${selectedSkillName === skill.name ? "active" : ""}`} onClick={() => setSelectedSkillName(skill.name)}>
            <span className="library-node-mark">✳</span><span><strong>{skill.name}</strong><small>{skill.description}</small></span><span className="library-chevron">›</span>
          </button>)}
          {!skills.length && <p className="library-empty">No published skills are available.</p>}
        </div>
        <div className="skill-inspector">
          <div className="inspector-heading"><div><span>SELECTED SKILL</span><strong>{selectedSkillName || "Choose a skill"}</strong></div>{selectedSkillDetail && <span className="version-badge">v{selectedSkillDetail.version}</span>}</div>
          {skillDetailLoading ? <p className="skill-detail-placeholder">Loading SKILL.md…</p> : selectedSkillDetail
            ? <pre className="skill-instructions">{selectedSkillDetail.instructions}</pre>
            : <p className="skill-detail-placeholder">Click a skill above to read its instructions.</p>}
          <button className="button primary add-node-button" type="button" onClick={addSkillNode} disabled={!canEditRequest || !selectedSkillDetail || skillInWorkflow || workflowNodes.length >= 10}>
            {skillInWorkflow ? "Already in workflow" : workflowNodes.length >= 10 ? "Workflow is full" : "＋ Add skill node"}
          </button>
        </div>
      </aside>
    </div>

    <footer className="minimal-footer"><span>AI Mining Network · local development</span><span>{workflowNodes.length ? `${workflowNodes.length} skill node${workflowNodes.length === 1 ? "" : "s"}` : "Add a skill to begin"}{selectedModel ? ` · ${selectedModel.parameterSize}` : ""}</span></footer>
  </main>;
}
