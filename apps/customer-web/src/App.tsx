import { useCallback, useEffect, useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent } from "react";
import { formatAtomic, validateGeneralTaskForm } from "./client.mjs";

type Model = { name: string; digest: string; sizeBytes: number; parameterSize: string; family: string; format: string };
type Skill = { name: string; version: string; contentHash: string; description: string; requiredModels: string[]; verificationStrategy: string };
type SkillDetail = Pick<Skill, "name" | "version" | "contentHash" | "description"> & { instructions: string; manifest: Record<string, unknown> };
type WorkflowSkillNode = { id: string; skillName: string; from: string };
type RuntimeNode = {
  nodeId: string;
  skillName: string;
  status: "QUEUED" | "RUNNING" | "IN_PROGRESS" | "COMPLETED" | "FAILED";
  completedUnits: number;
  totalUnits: number;
  currentUnit: number | null;
  inputValues?: unknown;
  outputValues?: unknown;
};
type NodeExecution = {
  currentNodeId: string | null;
  totalUnits: number;
  inputValues?: unknown;
  outputValues?: unknown;
  nodes: RuntimeNode[];
};
type JsonSchema = {
  type?: string;
  properties?: Record<string, { type?: string; description?: string; format?: string }>;
  required?: string[];
};
type WorkflowSpec = {
  version: 1;
  nodes: Array<{ id: string; type: "input" } | { id: string; type: "skill"; skillName: string } | { id: string; type: "output" }>;
  edges: Array<{ from: string; to: string }>;
};
type SavedWorkflow = { workflowId: string; name: string; workflow: WorkflowSpec; skills: Array<{ nodeId: string; name: string; version: string; contentHash: string }>; contentHash: string; createdAt: string; updatedAt: string };
type Job = {
  task: { taskId: string; inputCount: number; skillName?: string; skillNames?: string[]; model?: string; workflowHash?: string };
  quote: { quoteId: string; totalAtomic: number; executionRewardAtomic: number; verificationBudgetAtomic: number; protocolFeeAtomic: number; currency: { symbol: string; decimals: number }; expiresAt: string; sampleCount: number; validatorCount: number; verificationPolicy: Record<string, unknown>; failureRules: Record<string, unknown> };
  state: string;
  progress: string;
  error?: string | null;
  benchmark: Record<string, unknown>;
  pricing?: Record<string, unknown>;
  settlement?: { outcome: string; totalChargedAtomic: number; payouts: Array<{ payee: string; amountAtomic: number; purpose: string }> } | null;
  nodeExecution?: NodeExecution;
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

function asSchema(value: unknown): JsonSchema {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as JsonSchema;
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "No values available yet.";
  try { return JSON.stringify(value, null, 2) ?? String(value); }
  catch { return String(value); }
}

export default function App() {
  const [health, setHealth] = useState<Record<string, unknown> | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [modelsError, setModelsError] = useState("");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillCatalogDetails, setSkillCatalogDetails] = useState<Record<string, SkillDetail>>({});
  const [skillSearch, setSkillSearch] = useState("");
  const [selectedSkillName, setSelectedSkillName] = useState("");
  const [selectedSkillDetail, setSelectedSkillDetail] = useState<SkillDetail | null>(null);
  const [skillDetailLoading, setSkillDetailLoading] = useState(false);
  const [selectedNodeId, setSelectedNodeId] = useState("input");
  const [inspectorTab, setInspectorTab] = useState<"schema" | "values" | "skill">("values");
  const [workflowNodes, setWorkflowNodes] = useState<WorkflowSkillNode[]>([]);
  const [workflowName, setWorkflowName] = useState("New Workflow");
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [savedWorkflows, setSavedWorkflows] = useState<SavedWorkflow[]>([]);
  const [editingWorkflowName, setEditingWorkflowName] = useState(false);
  const [actionNotice, setActionNotice] = useState("");
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
  const workflowFormRef = useRef<HTMLFormElement>(null);

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

  const refreshWorkflows = useCallback(async () => {
    try { setSavedWorkflows(await api<SavedWorkflow[]>("/api/workflows")); }
    catch (reason) { setError(String(reason)); }
  }, []);

  useEffect(() => { void refreshWorkflows(); }, [refreshWorkflows]);

  useEffect(() => {
    let current = true;
    if (!skills.length) { setSkillCatalogDetails({}); return; }
    Promise.all(skills.map(async skill => {
      try { return await api<SkillDetail>(`/api/skills/${encodeURIComponent(skill.name)}`); }
      catch { return null; }
    })).then(details => {
      if (current) setSkillCatalogDetails(Object.fromEntries(details.filter((item): item is SkillDetail => item !== null).map(item => [item.name, item])));
    });
    return () => { current = false; };
  }, [skills]);

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
  const runningNodeId = job?.nodeExecution?.currentNodeId ?? null;
  const selectedWorkflowNode = workflowNodes.find(node => node.id === selectedNodeId);
  const selectedRuntimeNode = job?.nodeExecution?.nodes.find(node => node.nodeId === selectedNodeId);
  const selectedSkill = skills.find(skill => skill.name === selectedSkillName);
  const inputSchema = asSchema(selectedSkillDetail?.manifest.inputSchema);
  const outputSchema = asSchema(selectedSkillDetail?.manifest.outputSchema);
  const visibleSkills = skills.filter(skill => `${skill.name} ${skill.description}`.toLowerCase().includes(skillSearch.trim().toLowerCase()));
  const graphInputX = 24;
  const graphInputWidth = 288;
  const graphSkillWidth = 228;
  const graphSkillGap = 48;
  const graphOutputWidth = 176;
  const graphSkillX = (index: number) => graphInputX + graphInputWidth + 56 + index * (graphSkillWidth + graphSkillGap);
  const graphOutputX = workflowNodes.length
    ? graphSkillX(workflowNodes.length - 1) + graphSkillWidth + 38
    : graphInputX + graphInputWidth + 38;
  const graphWidth = graphOutputX + graphOutputWidth + 24;
  const graphEdges = workflow.edges.map(edge => {
    const fromX = edge.from === "input" ? graphInputX + graphInputWidth : graphSkillX(workflowNodes.findIndex(node => node.id === edge.from)) + graphSkillWidth;
    const toX = edge.to === "output" ? graphOutputX : graphSkillX(workflowNodes.findIndex(node => node.id === edge.to));
    const direction = Math.max(12, (toX - fromX) * 0.45);
    return { edge, path: `M ${fromX} 206 C ${fromX + direction} 206, ${toX - direction} 206, ${toX} 206` };
  });

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
    setSelectedNodeId(id);
    setInspectorTab("schema");
    setError("");
  };

  const selectLibrarySkill = (skillName: string) => {
    setSelectedSkillName(skillName);
    const existingNode = workflowNodes.find(node => node.skillName === skillName);
    setSelectedNodeId(existingNode?.id ?? `library:${skillName}`);
    setInspectorTab("schema");
  };

  const selectWorkflowNode = (node: WorkflowSkillNode) => {
    setSelectedSkillName(node.skillName);
    setSelectedNodeId(node.id);
    setInspectorTab(job?.nodeExecution?.nodes.some(item => item.nodeId === node.id && item.status !== "QUEUED") ? "values" : "schema");
  };

  const removeSkillNode = (nodeId: string) => {
    setWorkflowNodes(current => {
      const removed = current.find(node => node.id === nodeId);
      if (!removed) return current;
      return current.filter(node => node.id !== nodeId).map(node => node.from === nodeId ? { ...node, from: removed.from } : node);
    });
    if (selectedNodeId === nodeId) {
      setSelectedNodeId("input");
      setInspectorTab("values");
    }
  };

  const connectSkillNode = (nodeId: string, from: string) => {
    setWorkflowNodes(current => {
      const index = current.findIndex(node => node.id === nodeId);
      if (index < 0 || (from !== "input" && !current.slice(0, index).some(node => node.id === from))) return current;
      return current.map(node => node.id === nodeId ? { ...node, from } : node);
    });
  };

  const saveWorkflow = async () => {
    setError(""); setBusy(true);
    try {
      const saved = await api<SavedWorkflow>("/api/workflows", {
        method: "POST",
        body: JSON.stringify({ name: workflowName, workflow, ...(workflowId ? { workflowId } : {}) }),
      });
      setWorkflowId(saved.workflowId);
      setWorkflowName(saved.name);
      await refreshWorkflows();
      setActionNotice("Workflow saved to the local network registry.");
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); window.setTimeout(() => setActionNotice(""), 2800); }
  };

  const loadWorkflow = (saved: SavedWorkflow) => {
    const skillNodes = saved.workflow.nodes.filter((node): node is { id: string; type: "skill"; skillName: string } => node.type === "skill");
    setWorkflowNodes(skillNodes.map(node => ({ id: node.id, skillName: node.skillName, from: saved.workflow.edges.find(edge => edge.to === node.id)?.from ?? "input" })));
    const numericIds = skillNodes.map(node => Number(node.id.match(/skill-(\d+)/)?.[1] ?? 0));
    nextNodeNumber.current = Math.max(0, ...numericIds) + 1;
    setWorkflowName(saved.name);
    setWorkflowId(saved.workflowId);
    setPrompt("");
    setFiles([]);
    setSampleCount(1);
    setValidatorCount(1);
    setJob(null); setArtifacts([]); setResults([]); setLoadedTaskId(""); setRating("");
    setSelectedNodeId("input");
    setInspectorTab("values");
    const changedSkill = saved.skills?.some(ref => skillCatalogDetails[ref.name] && skillCatalogDetails[ref.name].contentHash !== ref.contentHash);
    setActionNotice(changedSkill
      ? `Loaded “${saved.name}”. A skill version changed since it was saved; review the current library before running.`
      : `Loaded “${saved.name}”. Add task input before running.`);
    window.setTimeout(() => setActionNotice(""), 2800);
  };

  const startNewWorkflow = () => {
    if (isTaskActive || busy) {
      setActionNotice("Wait for the current run or quote to finish before starting a new workflow.");
      window.setTimeout(() => setActionNotice(""), 2800);
      return;
    }
    setWorkflowNodes([]);
    setWorkflowName("New Workflow");
    setWorkflowId(null);
    setPrompt(""); setFiles([]); setSampleCount(1); setValidatorCount(1);
    setJob(null); setArtifacts([]); setResults([]); setLoadedTaskId(""); setRating("");
    setSelectedNodeId("input"); setInspectorTab("values");
    nextNodeNumber.current = 1;
    setActionNotice("New workflow started.");
    window.setTimeout(() => setActionNotice(""), 2200);
  };

  const shareWorkflow = async () => {
    const shareable = JSON.stringify({ name: workflowName, workflow, skills: workflowNodes.map(node => node.skillName) }, null, 2);
    try {
      await navigator.clipboard.writeText(shareable);
      setActionNotice("Workflow graph copied to clipboard.");
    } catch {
      setActionNotice("Clipboard access is unavailable in this browser.");
    }
    window.setTimeout(() => setActionNotice(""), 2500);
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
      <a className="brand" href="#top" aria-label="AI Mining Network home"><span className="brand-mark">AM</span><span>AI Mining Network<small>OPEN COMPUTE MARKET</small></span></a>
      <nav className="main-nav" aria-label="Main navigation">
        <button type="button" className="nav-link active" onClick={() => document.querySelector(".workflow-workspace")?.scrollIntoView({ behavior: "smooth" })}>Build</button>
        <button type="button" className="nav-link" onClick={() => document.querySelector(".run-output")?.scrollIntoView({ behavior: "smooth" })}>Runs</button>
        <details className="nav-menu"><summary className="nav-link">My Workflows <span>⌄</span></summary>
          <div className="workflow-menu-panel"><button type="button" className="workflow-menu-new" onClick={event => { startNewWorkflow(); event.currentTarget.closest("details")?.removeAttribute("open"); }}>＋ New workflow</button>
            {savedWorkflows.map(saved => <button type="button" className="saved-workflow-option" key={saved.workflowId} onClick={event => { loadWorkflow(saved); event.currentTarget.closest("details")?.removeAttribute("open"); }}><span><strong>{saved.name}</strong><small>{saved.workflow.nodes.filter(node => node.type === "skill").length} skill nodes · {new Date(saved.updatedAt).toLocaleDateString()}</small></span><em>Open</em></button>)}
            {!savedWorkflows.length && <p>No saved workflows yet.</p>}
          </div>
        </details>
        <button type="button" className="nav-link" onClick={() => document.querySelector(".node-library")?.scrollIntoView({ behavior: "smooth" })}>Market</button>
        <a className="nav-link" href="https://github.com/luiz-rocha94/AI-Mining-Network/tree/main/docs" target="_blank" rel="noreferrer">Docs</a>
      </nav>
      <div className="network-chip"><span className={`dot ${health?.ollama === "ready" ? "ready" : ""}`} />
        {health?.ollama === "ready" ? `Local node · ${models.length} models` : "Local node offline"}
      </div>
    </header>

    <section className="page-toolbar">
      <div className="workflow-title-area">
        <span className="crumb">WORKFLOWS <b>›</b> BUILD</span>
        {editingWorkflowName ? <input className="workflow-title-input" autoFocus value={workflowName} maxLength={120} onChange={event => setWorkflowName(event.target.value)} onBlur={() => setEditingWorkflowName(false)} onKeyDown={event => { if (event.key === "Enter") setEditingWorkflowName(false); }} aria-label="Workflow name" disabled={!canEditRequest} /> : <button type="button" className="workflow-title-button" onClick={() => canEditRequest && setEditingWorkflowName(true)} title="Click to rename workflow"><h1>{workflowName}</h1><span>✎</span></button>}
        <p>Connect skills into a workflow and run it on the network.</p>
      </div>
      <div className="toolbar-actions">
        {actionNotice && <span className="action-notice" role="status">{actionNotice}</span>}
        <button type="button" className="button secondary" onClick={() => void shareWorkflow()} disabled={!workflowNodes.length}>↗ <span>Share</span></button>
        <button type="button" className="button secondary save-button" onClick={() => void saveWorkflow()} disabled={!canEditRequest || !workflowNodes.length}>{busy ? "Saving…" : "Save"}</button>
        <button type="button" className="button primary run-button" onClick={() => workflowFormRef.current?.requestSubmit()} disabled={!canEditRequest || !models.length || !workflowNodes.length}>{busy ? <><i className="spinner" /> Working…</> : <>Run workflow <span>▶</span></>}</button>
      </div>
    </section>

    {error && <div className="global-error" role="alert">{error}<button type="button" onClick={() => setError("")} aria-label="Dismiss error">×</button></div>}
    {modelsError && <div className="global-error" role="alert">Ollama model catalog unavailable. {modelsError} Start Ollama and install a model with <code>ollama pull qwen3:4b-instruct</code>.</div>}

    {job && job.state !== "QUOTED" && <section className="run-output" aria-live="polite">
      <div className="run-output-heading"><div><span className="crumb">RUN OUTPUT</span><h2>{terminal.has(job.state) ? "Workflow result" : "Workflow running"}</h2></div><span className={`run-state ${terminal.has(job.state) ? "done" : "active"}`}>{job.state}</span></div>
      <p className="run-context">{job.task.skillName ?? workflowName} · {job.task.model ?? model} · {job.task.inputCount} input{job.task.inputCount === 1 ? "" : "s"}</p>
      {!terminal.has(job.state) && <div className="task-progress running"><i className="spinner" /><span>{job.progress}</span></div>}
      {job.error && <div className="error-box">{job.error}</div>}
      {terminal.has(job.state) && results.length > 0 && <div className="result-output"><div className="output-heading"><strong>Output</strong></div><pre>{JSON.stringify(results.length === 1 ? results[0].result : results.map(item => item.result), null, 2)}</pre></div>}
      {terminal.has(job.state) && results.length === 0 && !job.error && <div className="result-output"><p className="empty-output">No output was returned by this workflow.</p></div>}
      {terminal.has(job.state) && artifacts.some(item => item.name === "results.xlsx") && <div className="result-links"><a href={artifacts.find(item => item.name === "results.xlsx")?.url} download>Download spreadsheet ↓</a></div>}
      {terminal.has(job.state) && job.settlement && <div className="reaction-row" aria-label="Rate this workflow output"><span>Was this output helpful?</span>
        <button className={`reaction-button ${rating === "like" ? "selected" : ""}`} type="button" onClick={() => void submitRating("like")} disabled={Boolean(rating) || ratingBusy} aria-label="Like this output" aria-pressed={rating === "like"} title="Like"><span aria-hidden="true">👍</span></button>
        <button className={`reaction-button ${rating === "dislike" ? "selected" : ""}`} type="button" onClick={() => void submitRating("dislike")} disabled={Boolean(rating) || ratingBusy} aria-label="Dislike this output" aria-pressed={rating === "dislike"} title="Dislike"><span aria-hidden="true">👎</span></button>
        {rating && <small>Thanks for your feedback</small>}
      </div>}
    </section>}

    <div className="workflow-workspace">
      <form ref={workflowFormRef} className="workflow-canvas" onSubmit={createQuote}>
        <div className="canvas-toolbar"><div><span className="canvas-title">Workflow canvas</span><span className="canvas-subtitle">{workflowNodes.length} skill node{workflowNodes.length === 1 ? "" : "s"} · drag files to the input node</span></div><div className="canvas-controls"><span className="canvas-zoom">− <b>100%</b> ＋</span><button className="refresh-button" type="button" onClick={() => void refreshCatalog()} disabled={!canEditRequest}>↻ Refresh</button></div></div>
        <div className="graph-scroll" aria-label="Workflow graph">
          <div className="graph-world" style={{ width: graphWidth }}>
            <svg className="graph-connections" width={graphWidth} height="412" viewBox={`0 0 ${graphWidth} 412`} aria-hidden="true">
              <defs><marker id="arrowhead" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M1 1 L6 4 L1 7" fill="none" stroke="currentColor" strokeWidth="1.4" /></marker></defs>
              {graphEdges.map(({ edge, path }) => <path key={`${edge.from}-${edge.to}`} d={path} className="graph-edge" markerEnd="url(#arrowhead)" />)}
            </svg>
            <div className={`workflow-node input-node graph-node ${draggingFiles ? "composer-dragging" : ""} ${selectedNodeId === "input" ? "focused-node" : ""}`} style={{ left: graphInputX, top: 80, width: graphInputWidth }} onDragEnter={handleDragEnter} onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = canEditRequest ? "copy" : "none"; }} onDragLeave={handleDragLeave} onDrop={handleDrop}>
              {draggingFiles && <div className="drop-overlay"><strong>Drop files to attach</strong><span>Files stay private in this local demo</span></div>}
              <span className="graph-port port-output" />
              <div className="workflow-node-header"><span className="node-icon">01</span><button type="button" className="node-name-button" onClick={() => { setSelectedNodeId("input"); setInspectorTab("values"); }}><strong>Input</strong><small>Prompt, files and model</small></button><span className="node-kind">INPUT</span></div>
              <textarea rows={4} maxLength={20000} value={prompt} onChange={event => setPrompt(event.target.value)} onKeyDown={handlePromptKeyDown} placeholder="Describe what you want the workflow to do…" disabled={!canEditRequest} aria-label="Task prompt" />
              <label className={`graph-upload-zone ${!canEditRequest ? "disabled" : ""}`} htmlFor="workflow-files"><span>⇧</span><strong>{files.length ? `${files.length} file${files.length === 1 ? "" : "s"} attached` : "Drop files here or browse"}</strong><small>PDF, TXT, MD, CSV or JSON · up to 25 MiB</small></label>
              <input id="workflow-files" className="workflow-file-input" type="file" multiple accept=".pdf,.txt,.md,.csv,.json" onChange={event => { addFiles(event.target.files); event.target.value = ""; }} disabled={!canEditRequest} />
              {files.length > 0 && <div className="attachment-list">{files.map((file, index) => <span className="attachment-chip" key={`${file.name}-${index}`} title={file.name}><span>▤</span><span className="attachment-name">{file.name}</span><button type="button" onClick={() => removeFile(index)} disabled={!canEditRequest} aria-label={`Remove ${file.name}`}>×</button></span>)}</div>}
              <label className={`composer-select model-select ${!canEditRequest ? "disabled" : ""}`} title="Choose an Ollama model"><span>◉</span><select aria-label="Ollama model" value={model} onChange={event => setModel(event.target.value)} disabled={!canEditRequest || !models.length}>{models.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}</select><span className="chevron">⌄</span></label>
              <div className="input-node-hint">Private input · sent only after quote approval</div>
            </div>

            {workflowNodes.map((node, index) => {
              const run = job?.nodeExecution?.nodes.find(item => item.nodeId === node.id);
              const isRunning = runningNodeId === node.id;
              const statusText = run?.status === "RUNNING" && run.currentUnit ? `RUNNING · ${run.currentUnit}/${run.totalUnits}` : run?.status;
              return <article key={node.id} className={`workflow-node skill-node graph-node ${selectedNodeId === node.id ? "focused-node" : ""} ${isRunning ? "running-node" : ""}`} style={{ left: graphSkillX(index), top: 150, width: graphSkillWidth }} aria-current={isRunning ? "step" : undefined} onClick={() => selectWorkflowNode(node)}>
                <span className="graph-port port-input" /><span className="graph-port port-output" />
                <div className="workflow-node-header"><span className="node-icon skill-icon">✳</span><button type="button" className="node-name-button" onClick={event => { event.stopPropagation(); selectWorkflowNode(node); }} aria-label={`Inspect ${node.skillName} node`}><strong>{node.skillName}</strong><small>{skills.find(item => item.name === node.skillName)?.description ?? "Published skill"}</small></button>
                  {run ? <span className={`node-run-badge ${isRunning ? "active" : run.status.toLowerCase()}`} title={`${run.completedUnits} of ${run.totalUnits} documents processed`}>{isRunning && <i className="spinner" />}{statusText}</span> : <span className="node-kind">SKILL</span>}
                  <button className="remove-node" type="button" onClick={event => { event.stopPropagation(); removeSkillNode(node.id); }} disabled={!canEditRequest} aria-label={`Remove ${node.skillName} from workflow`}>×</button>
                </div>
                <div className="node-card-summary"><span>{String(index + 1).padStart(2, "0")}</span><b>{sourceName(node.from)} → {node.skillName}</b></div>
                <label className="node-connect-control"><span>From</span><select aria-label={`Connect ${node.skillName} from`} value={node.from} onClick={event => event.stopPropagation()} onChange={event => connectSkillNode(node.id, event.target.value)} disabled={!canEditRequest}><option value="input">Input</option>{workflowNodes.slice(0, index).map(source => <option key={source.id} value={source.id}>{source.skillName}</option>)}</select></label>
                {isRunning && run && <div className="node-progress-track"><span style={{ width: `${Math.max(4, Math.min(100, run.totalUnits ? run.completedUnits / run.totalUnits * 100 : 4))}%` }} /></div>}
              </article>;
            })}

            <div className={`workflow-node output-node graph-node ${selectedNodeId === "output" ? "focused-node" : ""}`} style={{ left: graphOutputX, top: 160, width: graphOutputWidth }} onClick={() => { setSelectedNodeId("output"); setInspectorTab("values"); }}><span className="graph-port port-input" /><div className="workflow-node-header"><span className="node-icon output-icon">↗</span><button type="button" className="node-name-button" onClick={() => { setSelectedNodeId("output"); setInspectorTab("values"); }}><strong>Final output</strong><small>{terminalWorkflowNodes.length ? `${terminalWorkflowNodes.length} result${terminalWorkflowNodes.length === 1 ? "" : "s"}` : "Workflow result"}</small></button><span className="node-kind">END</span></div></div>
          </div>
        </div>
        {job?.state === "QUOTED" && <div className="quote-dock" ref={quoteRef}>
          <div className="quote-dock-total"><span className="quote-check">✓</span><span><small>FIXED PRICE QUOTE</small><strong>{format(job.quote.totalAtomic)}</strong><em>Expires {new Date(job.quote.expiresAt).toLocaleTimeString()}</em></span></div>
          <div className="quote-dock-details"><div className="quote-cost-row"><span>Execution</span><b>{format(job.quote.executionRewardAtomic)}</b></div><div className="quote-cost-row"><span>Verification ({job.quote.sampleCount} samples · {job.quote.validatorCount} validators)</span><b>{format(job.quote.verificationBudgetAtomic)}</b></div><div className="quote-cost-row"><span>Protocol fee</span><b>{format(job.quote.protocolFeeAtomic)}</b></div>
            {skillEstimates.length > 0 && <div className="quote-skill-pills">{skillEstimates.map(item => <span key={item.nodeId}>{item.name} <b>{format(item.executionRewardAtomic + item.verificationBudgetAtomic + item.protocolFeeAtomic)}</b></span>)}</div>}
          </div>
          <div className="quote-dock-action"><button className="button primary" type="button" onClick={() => void acceptQuote()} disabled={busy}>{busy ? <><i className="spinner" /> Starting…</> : <>Approve price & run <span>→</span></>}</button><button type="button" className="text-button" onClick={() => setJob(null)} disabled={busy}>Edit workflow</button><details className="small-disclosure"><summary>Quote details</summary><p>{String(job.pricing?.explanation ?? "Fixed local estimate based on task size and available benchmark profile.")}</p><p>The final charge cannot exceed the quote. Customer ratings do not change payment.</p></details></div>
        </div>}
      </form>

      <aside className="node-library">
        <div className="library-heading"><div><h2>Skills</h2><p>Choose skills to build your workflow.</p></div><span className="skill-count">{skills.length}</span></div>
        <label className="skill-search"><span>⌕</span><input value={skillSearch} onChange={event => setSkillSearch(event.target.value)} placeholder="Search skills…" aria-label="Search skills" /></label>
        <div className="skill-library-list">
          {visibleSkills.map(skill => {
            const detail = skillCatalogDetails[skill.name];
            const inWorkflow = workflowNodes.some(node => node.skillName === skill.name);
            const schema = asSchema(detail?.manifest.inputSchema);
            const output = asSchema(detail?.manifest.outputSchema);
            return <button key={skill.name} type="button" className={`library-skill ${selectedSkillName === skill.name ? "active" : ""}`} onClick={() => selectLibrarySkill(skill.name)}>
              <span className="library-node-mark">✳</span><span className="skill-option-copy"><strong>{skill.name}</strong><small>{skill.description}</small><span className="skill-io-preview"><i>IN</i> {Object.keys(schema.properties ?? {}).slice(0, 2).join(", ") || "Any input"} <i>OUT</i> {Object.keys(output.properties ?? {}).slice(0, 2).join(", ") || "Structured result"}</span></span><span className="library-chevron">{inWorkflow ? "✓" : "＋"}</span>
            </button>;
          })}
          {!visibleSkills.length && <p className="library-empty">{skills.length ? "No skills match your search." : "No published skills are available."}</p>}
        </div>
        <div className="skill-inspector">
          {(() => {
            const inspectInput = selectedNodeId === "input";
            const inspectOutput = selectedNodeId === "output";
            const inspectSkill = !inspectInput && !inspectOutput;
            const heading = inspectInput ? "Input" : inspectOutput ? "Final output" : selectedSkillName || "Choose a skill";
            const inputValues = job?.nodeExecution?.inputValues ?? { prompt, model, files: files.map(file => ({ name: file.name, sizeBytes: file.size })) };
            const outputValues = results.length ? results.map(item => item.result) : job?.nodeExecution?.outputValues;
            const requiredModels = Array.isArray(selectedSkillDetail?.manifest.requiredModels) ? selectedSkillDetail.manifest.requiredModels.filter((item): item is string => typeof item === "string") : [];
            const verificationStrategy = typeof selectedSkillDetail?.manifest.verificationStrategy === "string" ? selectedSkillDetail.manifest.verificationStrategy : selectedSkill?.verificationStrategy ?? "Not specified";
            const renderSchema = (title: string, schema: JsonSchema) => <section className="schema-section"><div className="schema-heading"><strong>{title}</strong><span>{schema.type ?? "object"}</span></div>
              {Object.entries(schema.properties ?? {}).length ? <div className="schema-fields">{Object.entries(schema.properties ?? {}).map(([fieldName, field]) => <div className="schema-field" key={fieldName}><div><strong>{fieldName}</strong><span>{field.type ?? "any"}{field.format ? ` · ${field.format}` : ""}{schema.required?.includes(fieldName) ? " · required" : " · optional"}</span></div><p>{field.description || "No field description provided."}</p></div>)}</div> : <p className="skill-detail-placeholder">This skill does not declare {title.toLowerCase()} fields.</p>}
            </section>;
            return <>
              <div className="inspector-heading"><div><span>{inspectInput ? "INPUT NODE" : inspectOutput ? "OUTPUT NODE" : selectedWorkflowNode ? "WORKFLOW NODE" : "SKILL PREVIEW"}</span><strong>{heading}</strong></div>{inspectSkill && selectedSkillDetail && <span className="version-badge">v{selectedSkillDetail.version}</span>}</div>
              <div className="inspector-tabs" role="tablist" aria-label={`${heading} inspector`}><button type="button" role="tab" aria-selected={inspectorTab === "schema"} className={inspectorTab === "schema" ? "active" : ""} onClick={() => setInspectorTab("schema")}>{inspectSkill ? "Description & I/O" : "Details"}</button><button type="button" role="tab" aria-selected={inspectorTab === "values"} className={inspectorTab === "values" ? "active" : ""} onClick={() => setInspectorTab("values")}>Values</button>{inspectSkill && <button type="button" role="tab" aria-selected={inspectorTab === "skill"} className={inspectorTab === "skill" ? "active" : ""} onClick={() => setInspectorTab("skill")}>SKILL.md</button>}</div>
              <div className="inspector-content">
                {inspectSkill && inspectorTab === "schema" && (skillDetailLoading ? <p className="skill-detail-placeholder">Loading skill details…</p> : selectedSkillDetail ? <><p className="skill-short-description">{selectedSkillDetail.description || selectedSkill?.description || "No short description provided."}</p>{renderSchema("Inputs", inputSchema)}{renderSchema("Outputs", outputSchema)}<div className="inspector-metadata"><span>Verification</span><b>{verificationStrategy}</b></div>{requiredModels.length > 0 && <div className="inspector-metadata"><span>Required models</span><b>{requiredModels.join(", ")}</b></div>}</> : <p className="skill-detail-placeholder">Select a skill to inspect its description and input/output fields.</p>)}
                {inspectSkill && inspectorTab === "values" && <><div className="runtime-node-status"><span>Status</span><b className={selectedRuntimeNode?.status === "RUNNING" ? "live-status" : ""}>{selectedRuntimeNode?.status ?? "Not run yet"}</b>{selectedRuntimeNode && <small>{selectedRuntimeNode.completedUnits}/{selectedRuntimeNode.totalUnits} units processed</small>}</div><section className="inspector-value-group"><strong>Current input</strong><pre>{formatValue(selectedRuntimeNode?.inputValues)}</pre></section><section className="inspector-value-group"><strong>Current output</strong><pre>{formatValue(selectedRuntimeNode?.outputValues)}</pre></section>{!selectedRuntimeNode?.inputValues && <p className="skill-detail-placeholder">Values appear here when this node runs.</p>}<small className="preview-note">Live values are bounded previews and kept in memory for this local session.</small></>}
                {inspectSkill && inspectorTab === "skill" && (skillDetailLoading ? <p className="skill-detail-placeholder">Loading SKILL.md…</p> : selectedSkillDetail ? <pre className="skill-instructions">{selectedSkillDetail.instructions}</pre> : <p className="skill-detail-placeholder">Select a skill to read its instructions.</p>)}
                {inspectInput && inspectorTab === "schema" && <><p className="skill-short-description">The prompt, selected model, and private file attachments are passed into connected skill nodes.</p><div className="inspector-metadata"><span>Attachments</span><b>{files.length} file{files.length === 1 ? "" : "s"}</b></div><div className="inspector-metadata"><span>Model</span><b>{model || "No model selected"}</b></div></>}
                {inspectInput && inspectorTab === "values" && <section className="inspector-value-group"><strong>Current input values</strong><pre>{formatValue(inputValues)}</pre></section>}
                {inspectOutput && inspectorTab === "schema" && <p className="skill-short-description">The final output contains values produced by terminal skills. Completed tasks can provide downloadable output artifacts.</p>}
                {inspectOutput && inspectorTab === "values" && <section className="inspector-value-group"><strong>{results.length ? "Completed output" : "Current output"}</strong><pre>{formatValue(outputValues)}</pre>{!outputValues && <p className="skill-detail-placeholder">Output values appear here as the workflow completes.</p>}</section>}
              </div>
              {inspectSkill && <button className="button primary add-node-button" type="button" onClick={addSkillNode} disabled={!canEditRequest || !selectedSkillDetail || skillInWorkflow || workflowNodes.length >= 10}>{skillInWorkflow ? "Already in workflow" : workflowNodes.length >= 10 ? "Workflow is full" : "＋ Add to canvas"}</button>}
            </>;
          })()}
        </div>
      </aside>
    </div>
    <footer className="minimal-footer"><span>AI Mining Network · local development network</span><span>{savedWorkflows.length} saved workflow{savedWorkflows.length === 1 ? "" : "s"}{selectedModel ? ` · ${selectedModel.parameterSize}` : ""}</span></footer>
  </main>;
}
