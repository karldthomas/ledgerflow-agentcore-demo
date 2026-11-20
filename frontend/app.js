(() => {
  const config = window.LEDGERFLOW_CONFIG || {};
  const FLOW = [
    ["started", "Request accepted"],
    ["incident_lookup", "Find incident"],
    ["invoice_lookup", "Check invoices"],
    ["log_lookup", "Read application logs"],
    ["deployment_lookup", "Trace deployment"],
    ["retrieval", "Hybrid evidence search"],
    ["memory_lookup", "Recall verified memory"],
    ["context_compaction", "Compact context"],
    ["report_synthesis", "Synthesize report"],
    ["completed", "Publish report"],
  ];
  const FLOW_EVENT_MAP = {
    search_incidents: "incident_lookup",
    get_customer_invoices: "invoice_lookup",
    get_application_logs: "log_lookup",
    get_deployments: "deployment_lookup",
    search_evidence: "retrieval",
    memory_lookup: "memory_lookup",
    context_compaction: "context_compaction",
    reasoning_compaction: "context_compaction",
    reasoning_plan: "started",
    reasoning_synthesis: "report_synthesis",
  };
  const FLOW_DETAILS = {
    started: {
      description: "Accepts the request, opens the durable workflow, and starts the model planning pass.",
      source: "Workflow engine + model",
      operation: "request accepted",
    },
    incident_lookup: {
      description: "Finds the tenant-scoped incident record that anchors the investigation.",
      source: "Incidents",
      operation: "search_incidents",
    },
    invoice_lookup: {
      description: "Checks customer invoices for duplicate charges, retries, and payment state.",
      source: "Invoices",
      operation: "get_customer_invoices",
    },
    log_lookup: {
      description: "Reads application logs around the incident window to connect symptoms to behavior.",
      source: "Application logs",
      operation: "get_application_logs",
    },
    deployment_lookup: {
      description: "Traces the deployment or release most relevant to the incident timeline.",
      source: "Deployments",
      operation: "get_deployments",
    },
    retrieval: {
      description: "Combines lexical and vector evidence search, then applies tenant-scoped ranking.",
      source: "S3 Vectors + runbooks",
      operation: "search_evidence",
    },
    memory_lookup: {
      description: "Recalls eligible verified memory for the tenant without replacing primary evidence.",
      source: "AgentCore Memory",
      operation: "memory_lookup",
    },
    context_compaction: {
      description: "Preserves safety invariants and cited evidence while reducing the model's working context.",
      source: "Runtime compactor + optional Bedrock semantic pass",
      operation: "context compaction",
    },
    report_synthesis: {
      description: "Builds the typed report from the authorized evidence packet and prepares durable publication.",
      source: "Runtime + Bedrock",
      operation: "report synthesis",
    },
    completed: {
      description: "Publishes the prepared report and records the completed workflow checkpoint.",
      source: "Report store",
      operation: "report publication",
    },
  };
  const state = { accessToken: null, idToken: null, claims: {}, challengeSession: null, workflowId: null, timer: null, events: [] };
  const $ = (id) => document.getElementById(id);
  const show = (el, visible) => el.classList.toggle("hidden", !visible);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[ch]));

  function decodeJwt(token) {
    try {
      const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      return JSON.parse(decodeURIComponent(atob(part).split("").map((char) => `%${(`00${char.charCodeAt(0).toString(16)}`).slice(-2)}`).join("")));
    } catch (_) { return {}; }
  }

  function groups() {
    const value = state.claims["cognito:groups"] || [];
    if (Array.isArray(value)) return value;
    try { return JSON.parse(value); } catch (_) { return String(value).split(",").map((item) => item.trim()).filter(Boolean); }
  }

  async function cognitoRequest(target, body) {
    const response = await fetch(`https://cognito-idp.${config.region}.amazonaws.com/`, {
      method: "POST",
      headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}` },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || data.__type || "Cognito request failed");
    return data;
  }

  function saveAuth(result) {
    state.accessToken = result.AccessToken;
    state.idToken = result.IdToken;
    state.claims = decodeJwt(result.IdToken || result.AccessToken);
    sessionStorage.setItem("ledgerflow.auth", JSON.stringify({ accessToken: state.accessToken, idToken: state.idToken }));
    show($('login-view'), false); show($('app-view'), true);
    renderIdentity();
  }

  function restoreAuth() {
    try {
      const saved = JSON.parse(sessionStorage.getItem("ledgerflow.auth") || "null");
      if (saved?.accessToken && saved?.idToken) {
        state.accessToken = saved.accessToken; state.idToken = saved.idToken; state.claims = decodeJwt(saved.idToken);
        if ((state.claims.exp || 0) * 1000 > Date.now()) { show($('login-view'), false); show($('app-view'), true); renderIdentity(); return true; }
      }
    } catch (_) {}
    return false;
  }

  function renderIdentity() {
    const admin = groups().includes("admins");
    const devToolsAvailable = admin && ["dev", "test"].includes(String(config.environment || "dev").toLowerCase());
    $("user-chip").textContent = `${state.claims.email || state.claims.username || "signed in"}${admin ? " · admin" : ""}`;
    show($("admin-panel"), admin);
    show($("prompts-tab"), admin);
    show($("dev-tools-tab"), devToolsAvailable);
    show($("workflow-id-wrap"), admin);
    if (!admin) selectTab("investigate");
    if (admin) loadUsers();
  }

  function selectTab(tab) {
    const admin = groups().includes("admins");
    const devToolsAvailable = admin && ["dev", "test"].includes(String(config.environment || "dev").toLowerCase());
    const selected = admin && (tab === "prompts" || (tab === "dev-tools" && devToolsAvailable)) ? tab : "investigate";
    show($("investigate-panel"), selected === "investigate");
    show($("prompts-panel"), selected === "prompts");
    show($("dev-tools-panel"), selected === "dev-tools");
    document.querySelectorAll("[data-tab]").forEach((button) => button.classList.toggle("active", button.dataset.tab === selected));
    if (selected === "prompts") loadPrompts();
    if (selected === "dev-tools") loadDevTools();
  }

  async function api(path, options = {}) {
    // The console uses Cognito user claims for tenant and admin authorization.
    // Send the ID token so the API receives the same group claims used to
    // render the admin-only controls above.
    const authorizationToken = state.idToken || state.accessToken;
    const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", Authorization: `Bearer ${authorizationToken}`, ...(options.headers || {}) } });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) { signOut(); throw new Error("Your session expired."); }
    if (!response.ok) throw new Error(data.error || "Request failed");
    return data;
  }

  function setLoginError(message) { $("login-error").textContent = message; show($("login-error"), Boolean(message)); }

  async function login(event) {
    event.preventDefault(); setLoginError("");
    try {
      let result;
      if (state.challengeSession) {
        result = await cognitoRequest("RespondToAuthChallenge", { ClientId: config.clientId, ChallengeName: "NEW_PASSWORD_REQUIRED", Session: state.challengeSession, ChallengeResponses: { USERNAME: $("login-email").value.trim(), NEW_PASSWORD: $("new-password").value } });
      } else {
        result = await cognitoRequest("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", ClientId: config.clientId, AuthParameters: { USERNAME: $("login-email").value.trim(), PASSWORD: $("login-password").value } });
      }
      if (result.ChallengeName === "NEW_PASSWORD_REQUIRED") { state.challengeSession = result.Session; show($("new-password-wrap"), true); $("login-password").removeAttribute("required"); setLoginError("Set a permanent password, then submit again."); return; }
      saveAuth(result.AuthenticationResult);
    } catch (error) { setLoginError(error.message); }
  }

  function signOut() { state.accessToken = null; state.idToken = null; state.claims = {}; state.workflowId = null; if (state.timer) clearInterval(state.timer); sessionStorage.removeItem("ledgerflow.auth"); show($("app-view"), false); show($("login-view"), true); }

  function flowKeyForEvent(item) {
    return FLOW_EVENT_MAP[item.step] || FLOW_EVENT_MAP[item.event_type] || (item.event_type === "workflow" ? (item.status === "completed" ? "completed" : "started") : null);
  }

  function flowEventsFor(stageKey, events) { return events.filter((item) => flowKeyForEvent(item) === stageKey); }

  function compactFlowValue(value, limit = 96) {
    const text = String(value ?? "");
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  }

  function flowEventFacts(item) {
    const payload = item.payload || {};
    const facts = [];
    if (payload.tool) facts.push(`Tool: ${payload.tool}`);
    if (payload.target) facts.push(`Target: ${payload.target}`);
    if (payload.result_count !== undefined) facts.push(`${payload.result_count} results returned`);
    else if (payload.count !== undefined) facts.push(`${payload.count} records returned`);
    if (Array.isArray(payload.result_preview)) facts.push(payload.result_preview.length ? `Showing ${payload.result_preview.length} result previews` : "No result records");
    if (payload.preview_truncated) facts.push("Preview limited to the first 4 records");
    if (payload.model_id) facts.push(`Model: ${payload.model_id}`);
    if (payload.stop_reason) facts.push(`Stop reason: ${payload.stop_reason}`);
    if (payload.input_evidence_count !== undefined) facts.push(`Context input: ${payload.input_evidence_count} evidence records`);
    if (payload.selected_evidence_count !== undefined) facts.push(`Context selected: ${payload.selected_evidence_count}`);
    if (payload.full_evidence_count !== undefined) facts.push(`Full records: ${payload.full_evidence_count}`);
    if (payload.reference_only_evidence_count !== undefined) facts.push(`Citation-only: ${payload.reference_only_evidence_count}`);
    if (payload.omitted_evidence_count !== undefined) facts.push(`Omitted: ${payload.omitted_evidence_count}`);
    if (payload.token_count !== undefined && payload.budget_tokens !== undefined) facts.push(`Budget: ${payload.token_count} / ${payload.budget_tokens} tokens`);
    if (payload.input_token_count !== undefined && payload.budget_tokens === undefined) facts.push(`Input: ${payload.input_token_count} tokens`);
    if (payload.mode) facts.push(`Mode: ${payload.mode}`);
    if (payload.fallback_used) facts.push("Deterministic fallback used");
    if (payload.latency_ms !== undefined) facts.push(`Latency: ${payload.latency_ms} ms`);
    if (payload.deployment_version) facts.push(`Version: ${payload.deployment_version}`);
    if (payload.confidence !== undefined && Number.isFinite(Number(payload.confidence))) facts.push(`Confidence: ${Math.round(Number(payload.confidence) * 100)}%`);
    if (Array.isArray(payload.evidence_ids) && payload.evidence_ids.length) facts.push(`Evidence: ${payload.evidence_ids.slice(0, 5).join(", ")}${payload.evidence_ids.length > 5 ? ` +${payload.evidence_ids.length - 5}` : ""}`);
    if (payload.report) {
      facts.push("Report persisted");
      if (payload.report.evidence_schema_version) facts.push(`Schema: ${payload.report.evidence_schema_version}`);
      if (Number.isFinite(Number(payload.report.confidence))) facts.push(`Report confidence: ${Math.round(Number(payload.report.confidence) * 100)}%`);
      if (Array.isArray(payload.report.evidence_ids) && payload.report.evidence_ids.length) facts.push(`Report evidence: ${payload.report.evidence_ids.slice(0, 5).join(", ")}${payload.report.evidence_ids.length > 5 ? ` +${payload.report.evidence_ids.length - 5}` : ""}`);
      if (Array.isArray(payload.report.evidence_claims) && payload.report.evidence_claims.length) facts.push(`${payload.report.evidence_claims.length} typed claims`);
    }
    if (payload.plan) {
      if (payload.plan.incident_query) facts.push(`Incident query: ${compactFlowValue(payload.plan.incident_query)}`);
      if (payload.plan.evidence_query) facts.push(`Evidence query: ${compactFlowValue(payload.plan.evidence_query)}`);
      const sources = [payload.plan.include_invoices && "invoices", payload.plan.include_logs && "logs", payload.plan.include_deployments && "deployments"].filter(Boolean);
      if (sources.length) facts.push(`Sources: ${sources.join(", ")}`);
    }
    if (payload.arguments && typeof payload.arguments === "object") {
      const keys = Object.keys(payload.arguments);
      const safeKeys = ["customer_id", "incident_id", "tenant_id", "query", "version", "limit", "top_k"];
      safeKeys.forEach((key) => { if (payload.arguments[key] !== undefined && payload.arguments[key] !== null) facts.push(`${key}: ${compactFlowValue(payload.arguments[key])}`); });
      if (!safeKeys.some((key) => payload.arguments[key] !== undefined && payload.arguments[key] !== null)) facts.push(`Inputs: ${keys.slice(0, 4).join(", ")}${keys.length > 4 ? ` +${keys.length - 4}` : ""}`);
    }
    if (payload.reason) facts.push(`Reason: ${compactFlowValue(payload.reason)}`);
    if (payload.error) facts.push(`Error: ${compactFlowValue(payload.error)}`);
    if (!facts.length) facts.push(item.status === "completed" ? "Checkpoint completed" : "Checkpoint recorded");
    return facts;
  }

  function flowResultPreviewMarkup(item) {
    const payload = item.payload || {};
    if (!Array.isArray(payload.result_preview)) return "";
    const preview = payload.result_preview;
    const resultCount = Number.isFinite(Number(payload.result_count)) ? Number(payload.result_count) : preview.length;
    const heading = resultCount ? `Returned results · showing ${preview.length} of ${resultCount}` : "Returned results · none";
    if (!preview.length) return `<div class="flow-result-preview"><div class="flow-result-heading">${heading}</div><div class="flow-results-empty">The tool returned no records.</div></div>`;
    return `<div class="flow-result-preview"><div class="flow-result-heading">${heading}${payload.preview_truncated ? " · preview limited" : ""}</div><div class="flow-result-records">${preview.map((record, index) => { const label = record.IncidentId || record.InvoiceId || record.LogId || record.DeploymentId || record.memory_id || record.id || record.title || `Record ${index + 1}`; return `<div class="flow-result-record"><div class="flow-result-record-label">${escapeHtml(label)}</div><pre>${escapeHtml(JSON.stringify(record, null, 2))}</pre></div>`; }).join("")}</div></div>`;
  }

  function contextCompactionSummaryMarkup(events) {
    const compaction = events.slice().reverse().find((item) => {
      const payload = item.payload || {};
      return payload.selected_evidence_count !== undefined || payload.input_evidence_count !== undefined;
    });
    if (!compaction) return "";
    const payload = compaction.payload || {};
    const fallback = events.some((item) => (item.payload || {}).fallback_used);
    const tokenCount = payload.token_count ?? payload.input_token_count;
    const budget = payload.budget_tokens;
    const tokenLabel = tokenCount !== undefined && budget !== undefined ? `${tokenCount} / ${budget} tokens` : tokenCount !== undefined ? `${tokenCount} input tokens` : "token usage unavailable";
    return `<div class="flow-hover-facts context-compaction-summary"><span>${payload.input_evidence_count ?? "—"} input records</span><span>${payload.full_evidence_count ?? "—"} full records</span><span>${payload.reference_only_evidence_count ?? "—"} citation-only</span><span>${payload.omitted_evidence_count ?? "—"} omitted</span><span>${tokenLabel}</span>${fallback ? "<span>LLM fallback used</span>" : ""}</div>`;
  }

  function flowEventMarkup(item) {
    const payload = item.payload || {};
    const title = item.event_type === "tool_call" && payload.tool ? payload.tool : item.event_type || item.step;
    const facts = flowEventFacts(item);
    const statusClass = item.status === "failed" ? "failed" : item.status === "completed" ? "completed" : "pending";
    return `<div class="flow-event-item ${statusClass}"><span class="flow-event-mark">${item.status === "failed" ? "!" : item.status === "completed" ? "✓" : "•"}</span><div class="flow-event-body"><div class="flow-event-head"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(item.status || "observed")}</span></div><div class="flow-event-meta">${escapeHtml(item.step || "workflow")} · ${escapeHtml(eventTime(item).toLocaleTimeString())}</div><div class="flow-event-facts">${facts.map((fact) => `<span>${escapeHtml(fact)}</span>`).join("")}</div>${flowResultPreviewMarkup(item)}</div></div>`;
  }

  function flowDetailMarkup(stageKey, events) {
    const meta = FLOW_DETAILS[stageKey] || {};
    const stageEvents = flowEventsFor(stageKey, events).slice().sort((a, b) => eventTime(a) - eventTime(b));
    const completed = stageEvents.filter((item) => item.status === "completed").length;
    const failed = stageEvents.filter((item) => item.status === "failed").length;
    const resultValues = stageEvents.map((item) => (item.payload || {}).result_count ?? (item.payload || {}).count).filter((value) => value !== undefined && value !== null && Number.isFinite(Number(value)));
    const resultCount = resultValues.reduce((total, value) => total + Number(value), 0);
    const signals = [...new Set(stageEvents.map((item) => item.event_type || item.step).filter(Boolean))];
    const latest = stageEvents[stageEvents.length - 1];
    const stateLabel = failed ? "Needs review" : stageEvents.length && completed === stageEvents.length ? "Completed" : stageEvents.length ? "Observed" : "Waiting";
    const eventLabel = `${stageEvents.length} event${stageEvents.length === 1 ? "" : "s"}`;
    const eventTrace = stageEvents.length ? stageEvents.map(flowEventMarkup).join("") : `<div class="flow-event-empty">No events have been recorded for this stage yet.</div>`;
    return `<div class="flow-hover-heading"><div><p class="eyebrow">TIMELINE RESULTS</p><strong>${escapeHtml(FLOW.find(([key]) => key === stageKey)?.[1] || stageKey)}</strong></div><span class="flow-hover-status ${failed ? "failed" : ""}">${escapeHtml(stateLabel)}</span></div><p class="flow-hover-copy">${escapeHtml(meta.description || "Workflow activity for this stage.")}</p><div class="flow-hover-facts"><span>${eventLabel}</span><span>${completed} completed</span><span>Source: ${escapeHtml(meta.source || "workflow")}</span>${resultValues.length ? `<span>${resultCount} result${resultCount === 1 ? "" : "s"}</span>` : ""}</div>${stageKey === "context_compaction" ? contextCompactionSummaryMarkup(stageEvents) : ""}<div class="flow-hover-operation"><span>Operation</span><code>${escapeHtml(meta.operation || "—")}</code>${signals.length ? `<span class="flow-hover-signals">Observed: ${escapeHtml(signals.join(", "))}</span>` : ""}${latest ? `<span class="flow-hover-time">Last activity ${escapeHtml(eventTime(latest).toLocaleTimeString())}</span>` : ""}</div><div class="flow-event-trace"><div class="flow-event-trace-heading"><span>Event trace + returned data</span><span>${stageEvents.length} raw event${stageEvents.length === 1 ? "" : "s"}</span></div><div class="flow-event-list">${eventTrace}</div></div>`;
  }

  let flowHideTimer = null;

  function showFlowDetail(stageKey, events) {
    const detail = $("flow-hover-detail");
    if (!detail) return;
    window.clearTimeout(flowHideTimer);
    detail.innerHTML = flowDetailMarkup(stageKey, events);
    show(detail, true);
  }

  function hideFlowDetail() {
    window.clearTimeout(flowHideTimer);
    const detail = $("flow-hover-detail");
    if (detail) show(detail, false);
  }

  function scheduleHideFlowDetail() {
    window.clearTimeout(flowHideTimer);
    flowHideTimer = window.setTimeout(() => {
      const detail = $("flow-hover-detail");
      if (!detail || !detail.matches(":hover")) hideFlowDetail();
    }, 180);
  }

  function bindFlowHover(events) {
    const detail = $("flow-hover-detail");
    if (detail) {
      detail.addEventListener("mouseenter", () => window.clearTimeout(flowHideTimer));
      detail.addEventListener("mouseleave", scheduleHideFlowDetail);
    }
    document.querySelectorAll("#flow-board [data-flow-key]").forEach((stage) => {
      const stageKey = stage.dataset.flowKey;
      stage.addEventListener("mouseenter", () => showFlowDetail(stageKey, events));
      stage.addEventListener("focus", () => showFlowDetail(stageKey, events));
      stage.addEventListener("mouseleave", scheduleHideFlowDetail);
      stage.addEventListener("blur", scheduleHideFlowDetail);
    });
  }

  function renderFlow(status, events) {
    const checkpoint = status?.step || "started";
    const currentIndex = FLOW.findIndex(([key]) => key === checkpoint);
    const failed = events.find((item) => item.status === "failed");
    const counts = {};
    events.forEach((item) => {
      const flowKey = flowKeyForEvent(item);
      if (flowKey) counts[flowKey] = (counts[flowKey] || 0) + 1;
    });
    $("flow-board").classList.remove("empty-flow");
    $("flow-board").innerHTML = `<div class="flow-track">${FLOW.map(([key, label], index) => {
      const done = index < currentIndex || checkpoint === "completed";
      const active = index === currentIndex && checkpoint !== "completed";
      const stateClass = failed && key === checkpoint ? "failed" : done ? "done" : active ? "active" : "";
      const description = FLOW_DETAILS[key]?.description || "Inspect this workflow stage.";
      return `<button type="button" class="flow-step ${stateClass}" data-flow-key="${key}" aria-label="${escapeHtml(label)}: ${escapeHtml(description)}"><span class="step-dot">${done ? "✓" : active ? "•" : index + 1}</span><span class="step-label">${label}</span><span class="step-count">${counts[key] || 0} events</span></button>`;
    }).join("")}</div><div id="flow-hover-detail" class="flow-hover-detail hidden" role="status" aria-live="polite"></div>`;
    bindFlowHover(events);
  }

  function eventTime(item) {
    const rawTime = Number(item.created_at) || Date.now();
    return new Date(rawTime < 100000000000 ? rawTime * 1000 : rawTime);
  }

  function eventRowMarkup(item, index) {
    const payload = item.payload || {};
    const detail = payload.result_count !== undefined ? `${payload.result_count} results` : payload.plan ? "plan ready" : payload.stop_reason || "inspect details";
    return `<button type="button" class="event-row ${item.status === "failed" ? "failed" : ""}" data-event-index="${index}" aria-label="Inspect ${escapeHtml(item.event_type)} event"><span class="event-dot"></span><span><strong>${escapeHtml(item.event_type)} · ${escapeHtml(item.step)}</strong><small>${eventTime(item).toLocaleTimeString()} · ${escapeHtml(detail)}</small></span><span class="event-status">${escapeHtml(item.status)}</span></button>`;
  }

  function bindEventRows(container) {
    container.querySelectorAll("[data-event-index]").forEach((row) => row.addEventListener("click", () => openEventDetails(Number(row.dataset.eventIndex))));
  }

  function openEventDetails(index) {
    const item = state.events[index];
    if (!item) return;
    $("event-popover-detail").classList.remove("empty-detail");
    $("event-popover-detail").innerHTML = `<div class="event-detail-heading"><div><strong>${escapeHtml(item.event_type)} · ${escapeHtml(item.step)}</strong><span>${eventTime(item).toLocaleString()} · ${escapeHtml(item.status)}</span></div><span class="claim-type">${escapeHtml(item.visibility || "operator")}</span></div><pre>${escapeHtml(JSON.stringify(item.payload || {}, null, 2))}</pre>`;
  }

  function openEventPopover() {
    const modal = $("event-popover");
    if (!modal) return;
    const rows = state.events.slice().reverse().map((item, reverseIndex) => eventRowMarkup(item, state.events.length - reverseIndex - 1)).join("");
    $("event-popover-list").innerHTML = rows || "<span class='empty-detail'>No execution events yet.</span>";
    $("event-popover-detail").innerHTML = "<span>Select an event to inspect its structured payload.</span>";
    $("event-popover-detail").classList.add("empty-detail");
    bindEventRows($("event-popover-list"));
    show(modal, true);
    document.body.classList.add("modal-open");
  }

  function closeEventPopover() {
    const modal = $("event-popover");
    if (!modal) return;
    show(modal, false);
    document.body.classList.remove("modal-open");
  }

  function reasoningTitle(item) {
    if (item.event_type === "reasoning_plan") return "Plan summary";
    if (["reasoning_compaction", "context_compaction"].includes(item.event_type)) return "Context compaction";
    return "Synthesis summary";
  }

  function renderEvents(events) {
    state.events = events || [];
    $("event-count").textContent = state.events.length;
    if (!state.events.length) { $("event-list").innerHTML = "<span>Waiting for workflow events.</span>"; } else {
      $("event-list").classList.remove("empty-detail");
      $("event-list").innerHTML = state.events.slice().reverse().map((item, reverseIndex) => eventRowMarkup(item, state.events.length - reverseIndex - 1)).join("");
      bindEventRows($("event-list"));
    }
    const llm = state.events.filter((item) => ["reasoning_plan", "reasoning_compaction", "context_compaction", "reasoning_synthesis"].includes(item.event_type));
    $("inspector-count").textContent = llm.length;
    if (!llm.length) { $("inspector").innerHTML = "<span>Safe planning, context compaction, and synthesis summaries will appear here for every authenticated operator.</span>"; return; }
    $("inspector").classList.remove("empty-detail");
    $("inspector").innerHTML = llm.map((item) => `<div class="inspector-card"><div class="inspector-title"><span>${escapeHtml(reasoningTitle(item))}</span><span>${escapeHtml(item.status)}</span></div><pre>${escapeHtml(JSON.stringify(item.payload, null, 2))}</pre></div>`).join("");
  }

  function listMarkup(items) { return Array.isArray(items) ? `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""; }

  function claimMarkup(claims) {
    if (!Array.isArray(claims) || !claims.length) return "";
    return `<div class="report-label">Evidence reasoning</div><div class="claim-list">${claims.map((claim) => `<div class="claim-card"><div class="claim-heading"><span class="claim-type">${escapeHtml(claim.type || "claim")}</span><span>${escapeHtml((claim.evidence_ids || []).join(", "))}</span></div><p>${escapeHtml(claim.statement || "")}</p></div>`).join("")}</div>`;
  }

  function renderReport(report) {
    if (!report) { $("report-view").innerHTML = "<span>Complete an investigation to see the report.</span>"; $("report-confidence").textContent = "—"; return; }
    $("report-confidence").textContent = `${Math.round(Number(report.confidence || 0) * 100)}% confidence`;
    $("report-view").classList.remove("empty-detail");
    $("report-view").innerHTML = `<div class="report-grid"><div><div class="report-label">Executive summary</div><p class="report-lead">${escapeHtml(report.executive_summary)}</p><div class="report-label">Root cause</div><p>${escapeHtml(report.root_cause)}</p><div class="report-label">Timeline</div>${listMarkup(report.incident_timeline)}<div class="report-label">Recommended actions</div>${listMarkup(report.recommended_actions)}</div><div><div class="report-label">Verified findings</div>${listMarkup(report.verified_findings)}<div class="report-label">Unresolved questions</div>${listMarkup(report.unresolved_questions)}<div class="report-label">Evidence IDs</div><div class="evidence-chips">${(report.evidence_ids || []).map((id) => `<span class="evidence-chip">${escapeHtml(id)}</span>`).join("")}</div></div></div>${claimMarkup(report.evidence_claims)}`;
  }

  async function refreshWorkflow() {
    if (!state.workflowId) return;
    try {
      const data = await api(`/api/investigations/${encodeURIComponent(state.workflowId)}`);
      $("run-title").textContent = `${data.customer_id || "Investigation"} · ${data.workflow_id}`;
      $("run-meta").innerHTML = `<span>${escapeHtml(data.tenant_id || "tenant")}</span><span>checkpoint: ${escapeHtml(data.step || "queued")}</span><span>${data.updated_at ? new Date(data.updated_at).toLocaleTimeString() : "starting"}</span>`;
      const isDone = data.step === "completed"; const hasInjectedFailure = data.events?.some((item) => item.event_type === "failure_injection" && item.status === "failed"); const isFailed = !hasInjectedFailure && data.events?.some((item) => item.status === "failed");
      $("run-status").className = `status-pill ${isFailed ? "failed" : isDone ? "ready" : "running"}`; $("run-status").innerHTML = `<span></span> ${isFailed ? "Needs review" : isDone ? "Completed" : hasInjectedFailure ? "Recovering" : "Investigating"}`;
      renderFlow(data, data.events || []); renderEvents(data.events || []); renderReport(data.report);
      if (isDone || isFailed) { clearInterval(state.timer); state.timer = null; }
    } catch (error) { $("investigation-error").textContent = error.message; show($("investigation-error"), true); }
  }

  async function startInvestigation(event) {
    event.preventDefault(); show($("investigation-error"), false);
    try {
      const payload = { customer_id: $("customer-id").value.trim(), incident_id: $("incident-id").value.trim(), tenant_id: $("tenant-id").value.trim(), request: $("request-text").value.trim() };
      const workflowId = groups().includes("admins") ? $("workflow-id").value.trim() : "";
      if (workflowId) payload.workflow_id = workflowId;
      const data = await api("/api/investigations", { method: "POST", body: JSON.stringify(payload) });
      state.workflowId = data.workflow_id; $("run-title").textContent = `${$("customer-id").value.trim()} · ${data.workflow_id}`; $("report-view").innerHTML = "<span>Investigation started…</span>"; $("flow-board").innerHTML = "<div class='empty-state'><div class='empty-icon'>◌</div><strong>Building the investigation map…</strong><span>Waiting for the first checkpoint.</span></div>"; $("flow-board").classList.add("empty-flow");
      if (state.timer) clearInterval(state.timer); await refreshWorkflow(); state.timer = setInterval(refreshWorkflow, 1400);
    } catch (error) { $("investigation-error").textContent = error.message; show($("investigation-error"), true); }
  }

  async function loadUsers() {
    try { const data = await api("/api/admin/users"); $("user-list").innerHTML = data.users.map((user) => `<div class="user-row"><strong>${escapeHtml(user.email || user.username)}</strong><span>${escapeHtml(user.tenant_id)} · ${(user.groups || []).join(", ")} · ${escapeHtml(user.status)}</span></div>`).join("") || "<span class='muted small-copy'>No users yet.</span>"; } catch (error) { $("user-list").innerHTML = `<span class="form-error">${escapeHtml(error.message)}</span>`; }
  }

  async function createUser(event) {
    event.preventDefault(); show($("user-error"), false);
    try { await api("/api/admin/users", { method: "POST", body: JSON.stringify({ email: $("new-user-email").value.trim(), tenant_id: $("new-user-tenant").value.trim(), group: $("new-user-group").value }) }); $("user-form").reset(); $("new-user-tenant").value = "tenant_acme"; await loadUsers(); } catch (error) { $("user-error").textContent = error.message; show($("user-error"), true); }
  }

  function planDate(value) {
    if (!value) return "date unavailable";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function planExpiry(epoch) {
    if (!epoch) return "no expiry";
    const date = new Date(Number(epoch) * 1000);
    return Number.isNaN(date.getTime()) ? "invalid expiry" : date.toLocaleString();
  }

  function failurePlanMarkup(plan) {
    const status = String(plan.status || "UNKNOWN").toLowerCase();
    const disable = plan.status === "ACTIVE"
      ? `<button class="ghost-button plan-disable-button" type="button" data-disable-plan="${escapeHtml(plan.control_id)}">Disarm</button>`
      : "";
    return `<article class="failure-plan-row ${status}"><div class="failure-plan-main"><div class="failure-plan-heading"><strong>${escapeHtml(plan.workflow_id)}</strong><span class="plan-status ${status}">${escapeHtml(plan.status)}</span></div><div class="failure-plan-facts"><span>after <code>${escapeHtml(plan.step)}</code></span><span>expires ${escapeHtml(planExpiry(plan.expires_at_epoch))}</span><span>armed by ${escapeHtml(plan.created_by || "admin")}</span></div><div class="failure-plan-meta"><span>${escapeHtml(plan.control_id)}</span><span>${escapeHtml(planDate(plan.created_at))}</span>${plan.consumed_at ? `<span>consumed ${escapeHtml(planDate(plan.consumed_at))}</span>` : ""}</div></div>${disable}</article>`;
  }

  async function loadDevTools() {
    if (!groups().includes("admins")) return;
    show($("dev-tools-error"), false);
    $("failure-plan-list").innerHTML = "<span class='empty-detail'>Loading failure plans…</span>";
    try {
      const data = await api("/api/admin/dev-tools");
      $("dev-tools-environment").textContent = data.environment || "unknown";
      const controls = data.controls || [];
      $("dev-tools-count").textContent = controls.length;
      $("failure-plan-list").innerHTML = controls.map(failurePlanMarkup).join("") || "<span class='empty-detail'>No failure plans have been armed.</span>";
    } catch (error) {
      $("failure-plan-list").innerHTML = "<span class='empty-detail'>Failure plans could not be loaded.</span>";
      $("dev-tools-error").textContent = error.message;
      show($("dev-tools-error"), true);
    }
  }

  async function armFailurePlan(event) {
    event.preventDefault();
    show($("dev-tools-error"), false);
    try {
      const workflowId = $("dev-tools-workflow-id").value.trim();
      const data = await api("/api/admin/dev-tools/failure-plans", {
        method: "POST",
        body: JSON.stringify({ workflow_id: workflowId, step: $("failure-plan-step").value, ttl_minutes: $("failure-plan-ttl").value }),
      });
      $("workflow-id").value = data.control.workflow_id;
      await loadDevTools();
    } catch (error) {
      $("dev-tools-error").textContent = error.message;
      show($("dev-tools-error"), true);
    }
  }

  async function disableFailurePlan(controlId) {
    show($("dev-tools-error"), false);
    try {
      await api(`/api/admin/dev-tools/failure-plans/${encodeURIComponent(controlId)}/disable`, { method: "POST", body: "{}" });
      await loadDevTools();
    } catch (error) {
      $("dev-tools-error").textContent = error.message;
      show($("dev-tools-error"), true);
    }
  }

  function promptDate(value) {
    if (!value) return "date unavailable";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function promptCardMarkup(prompt) {
    const draft = prompt.draft || {};
    const runtime = Boolean(prompt.runtime);
    const versions = Array.isArray(prompt.versions) ? prompt.versions : [];
    const options = versions.map((version) => `<option value="${escapeHtml(version.version)}"${version.version === prompt.active_version ? " selected" : ""}>Version ${escapeHtml(version.version)}${version.version === prompt.default_version ? " · initial" : ""}</option>`).join("");
    const rows = versions.map((version) => `<div class="prompt-version-row ${version.version === prompt.active_version ? "active" : ""}"><div><strong>v${escapeHtml(version.version)}</strong><span>${escapeHtml(version.description || "No description")}</span></div><div><span>${version.version === prompt.active_version ? "ACTIVE" : ""}</span><small>${escapeHtml(promptDate(version.created_at || version.updated_at))}</small></div></div>`).join("");
    const runtimeControls = runtime
      ? `<div class="prompt-active-row"><label>Runtime version<select data-prompt-version="${escapeHtml(prompt.key)}">${options || "<option>No versions found</option>"}</select></label><button class="secondary-button" type="button" data-activate-prompt="${escapeHtml(prompt.key)}">Use selected version</button></div>`
      : `<p class="muted small-copy prompt-library-note">Library prompt · create versions here, then assign it to a runtime stage when you are ready.</p>`;
    const activeLabel = runtime ? `v${escapeHtml(prompt.active_version || "—")} active` : "library prompt";
    const readonlyName = runtime ? " readonly" : "";
    return `<article class="prompt-card" data-prompt-card="${escapeHtml(prompt.key)}"><div class="prompt-card-header"><div><p class="eyebrow">${escapeHtml(prompt.key)}</p><h3>${escapeHtml(prompt.label)}</h3><code>${escapeHtml(prompt.arn)}</code></div><span class="confidence-badge">${activeLabel}</span></div><div class="prompt-draft-meta">DRAFT · updated ${escapeHtml(promptDate(draft.updated_at))}</div><div class="prompt-editor"><div class="prompt-editor-heading"><div><p class="eyebrow">DRAFT CONTENT</p><strong>Edit native Bedrock prompt</strong></div><span class="prompt-type-badge">${escapeHtml((draft.input_variables || []).map((item) => `{{${item}}}`).join(", ") || "no variables")}</span></div><div class="prompt-form-grid"><label>Prompt name<input data-prompt-name="${escapeHtml(prompt.key)}" value="${escapeHtml(draft.name || prompt.label)}"${readonlyName} /></label><label>Description<input data-prompt-description-text="${escapeHtml(prompt.key)}" value="${escapeHtml(draft.description || "")}" placeholder="What this prompt is for" /></label></div><label>System instructions<textarea data-prompt-system="${escapeHtml(prompt.key)}" rows="6" placeholder="Optional instructions for the model">${escapeHtml(draft.system || "")}</textarea></label><label>User message template<textarea data-prompt-user="${escapeHtml(prompt.key)}" rows="5">${escapeHtml(draft.user || "{{input}}")}</textarea></label><div class="prompt-parameter-grid"><label>Model ID<input data-prompt-model="${escapeHtml(prompt.key)}" value="${escapeHtml(draft.model_id || "")}" /></label><label>Max tokens<input data-prompt-max-tokens="${escapeHtml(prompt.key)}" type="number" min="1" value="${escapeHtml(draft.max_tokens ?? 1000)}" /></label><label>Temperature<input data-prompt-temperature="${escapeHtml(prompt.key)}" type="number" min="0" max="2" step="0.1" value="${escapeHtml(draft.temperature ?? 0)}" /></label></div><div class="prompt-editor-actions"><button class="secondary-button" type="button" data-save-prompt="${escapeHtml(prompt.key)}">Save draft</button><span class="muted small-copy">Saving updates the Bedrock DRAFT only.</span></div></div>${runtimeControls}<div class="prompt-create-row"><label>New version description<input data-prompt-version-description="${escapeHtml(prompt.key)}" placeholder="What changed?" /></label><button class="ghost-button" type="button" data-create-prompt="${escapeHtml(prompt.key)}">Create immutable version</button></div><div class="prompt-version-list"><div class="prompt-version-heading"><span>Version history</span><span>${versions.length} version${versions.length === 1 ? "" : "s"}</span></div>${rows || "<span class='muted small-copy'>No immutable versions returned from Bedrock.</span>"}</div></article>`;
  }

  async function loadPrompts() {
    if (!groups().includes("admins")) return;
    show($("prompt-error"), false);
    $("prompt-list").innerHTML = "<span class='empty-detail'>Loading prompt versions…</span>";
    try {
      const data = await api("/api/admin/prompts");
      $("prompt-list").innerHTML = (data.prompts || []).map(promptCardMarkup).join("") || "<span class='empty-detail'>No prompt definitions configured.</span>";
    } catch (error) {
      $("prompt-list").innerHTML = "<span class='empty-detail'>Prompt versions could not be loaded.</span>";
      $("prompt-error").textContent = error.message;
      show($("prompt-error"), true);
    }
  }

  async function activatePrompt(key) {
    const select = document.querySelector(`[data-prompt-version="${key}"]`);
    if (!select) return;
    show($("prompt-error"), false);
    try {
      await api(`/api/admin/prompts/${encodeURIComponent(key)}/active`, { method: "POST", body: JSON.stringify({ version: select.value }) });
      await loadPrompts();
    } catch (error) {
      $("prompt-error").textContent = error.message;
      show($("prompt-error"), true);
    }
  }

  async function savePrompt(key) {
    const value = (field) => document.querySelector(`[data-prompt-${field}="${key}"]`)?.value ?? "";
    show($("prompt-error"), false);
    try {
      await api(`/api/admin/prompts/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify({ name: value("name"), description: document.querySelector(`[data-prompt-description-text="${key}"]`)?.value ?? "", system: value("system"), user: value("user"), model_id: value("model"), max_tokens: value("max-tokens"), temperature: value("temperature") }) });
      await loadPrompts();
    } catch (error) {
      $("prompt-error").textContent = error.message;
      show($("prompt-error"), true);
    }
  }

  async function createPrompt(event) {
    event.preventDefault();
    show($("prompt-error"), false);
    try {
      await api("/api/admin/prompts", { method: "POST", body: JSON.stringify({ name: $("new-prompt-name").value.trim(), description: $("new-prompt-description").value.trim(), system: $("new-prompt-system").value, user: $("new-prompt-user").value, model_id: $("new-prompt-model").value.trim(), max_tokens: $("new-prompt-max-tokens").value, temperature: $("new-prompt-temperature").value }) });
      $("new-prompt-form").reset();
      $("new-prompt-user").value = "{{input}}";
      $("new-prompt-model").value = "amazon.nova-micro-v1:0";
      $("new-prompt-max-tokens").value = "1000";
      $("new-prompt-temperature").value = "0";
      await loadPrompts();
    } catch (error) {
      $("prompt-error").textContent = error.message;
      show($("prompt-error"), true);
    }
  }

  async function createPromptVersion(key) {
    const input = document.querySelector(`[data-prompt-version-description="${key}"]`);
    show($("prompt-error"), false);
    try {
      await api(`/api/admin/prompts/${encodeURIComponent(key)}/versions`, { method: "POST", body: JSON.stringify({ description: input?.value.trim() || "Created from the Bedrock draft." }) });
      await loadPrompts();
    } catch (error) {
      $("prompt-error").textContent = error.message;
      show($("prompt-error"), true);
    }
  }

  $("login-form").addEventListener("submit", login); $("signout-button").addEventListener("click", signOut); $("investigation-form").addEventListener("submit", startInvestigation); $("user-form").addEventListener("submit", createUser); $("new-prompt-form").addEventListener("submit", createPrompt);
  $("failure-plan-form").addEventListener("submit", armFailurePlan);
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => selectTab(button.dataset.tab)));
  $("refresh-prompts").addEventListener("click", loadPrompts);
  $("refresh-dev-tools").addEventListener("click", loadDevTools);
  $("failure-plan-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-disable-plan]");
    if (button) disableFailurePlan(button.dataset.disablePlan);
  });
  $("prompt-list").addEventListener("click", (event) => {
    const activate = event.target.closest("[data-activate-prompt]");
    const create = event.target.closest("[data-create-prompt]");
    const save = event.target.closest("[data-save-prompt]");
    if (activate) activatePrompt(activate.dataset.activatePrompt);
    if (create) createPromptVersion(create.dataset.createPrompt);
    if (save) savePrompt(save.dataset.savePrompt);
  });
  $("open-log-button").addEventListener("click", openEventPopover);
  $("close-event-popover").addEventListener("click", closeEventPopover);
  $("event-popover").addEventListener("click", (event) => { if (event.target.id === "event-popover") closeEventPopover(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeEventPopover(); });
  $("failure-plan-step").innerHTML = FLOW.filter(([key]) => key !== "completed").map(([key, label]) => `<option value="${escapeHtml(key)}">${escapeHtml(label)}</option>`).join("");
  if (!restoreAuth()) { show($("login-view"), true); show($("app-view"), false); }
})();
