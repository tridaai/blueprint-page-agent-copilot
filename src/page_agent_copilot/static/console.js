/* OrderDesk console logic: plain fetches against the shell's JSON
 * API. The same endpoints a human uses here are the ones the in-page
 * agent's form submissions land on — one mutation path for everyone.
 * `window.OrderDesk` exposes the navigation the copilot bridge uses
 * to put the agent on the right screen before each step. */
(function () {
  const $ = (id) => document.getElementById(id);
  const api = async (path, opts) => {
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error(path + " -> " + res.status);
    return res.json();
  };
  const json = (method, body) => ({
    method, headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  let current = { order: null, customer: null, invoice: null, task: null };
  let roleData = { roles: [], operators: [] };

  // The acting role, from the shell's governed role data: the line
  // under the operator name states what that name may do, and the
  // rights summary is rendered from the same definitions the API
  // enforces — the console shows what the shell will refuse.
  function rightsLine(role) {
    const acts = [];
    if (role.can_plan) acts.push("plan");
    if (role.can_confirm) acts.push("confirm others' plans");
    if (role.can_amend) acts.push("amend");
    if (role.can_execute) acts.push("execute");
    if (role.can_administer) acts.push("edit roles");
    return acts.length ? acts.join(", ") : "no actions — read-only";
  }
  function updateRoleLine() {
    const name = ($("task-operator").value || "").trim();
    const entry = roleData.operators.find((o) => o.id === name);
    if (!entry) {
      $("task-role-line").textContent = name
        ? `Acting as ${name} — no role in the operator directory: this name may do nothing.`
        : "";
      return;
    }
    const role = roleData.operators.length
      ? roleData.roles.find((r) => r.name === entry.role) : null;
    $("task-role-line").textContent = role
      ? `Acting as ${name} — role: ${role.name} (may ${rightsLine(role)}; ` +
        `may never confirm a plan they planned themselves).`
      : `Acting as ${name} — role: ${entry.role}.`;
  }
  async function refreshRoles() {
    roleData = await api("/api/roles");
    $("roles-list").textContent = roleData.roles.map((r) => {
      const fields = Object.entries(r.fields)
        .map(([k, v]) => `${k}: ${v.join(", ") || "—"}`).join(" · ");
      return `${r.name} — may ${rightsLine(r)} | pages: ` +
        `${r.pages.join(", ") || "—"} | fields: ${fields || "—"}`;
    }).join("\n") + "\n" + roleData.operators.map(
      (o) => `${o.id} → ${o.role}`).join("\n");
    updateRoleLine();
  }
  $("task-operator").addEventListener("input", updateRoleLine);

  function showScreen(name) {
    document.querySelectorAll("nav button").forEach((b) =>
      b.classList.toggle("active", b.dataset.screen === name));
    ["orders", "customers", "invoices", "copilot"].forEach((s) =>
      ($("screen-" + s).hidden = s !== name));
  }
  document.querySelectorAll("nav button").forEach((b) =>
    b.addEventListener("click", () => showScreen(b.dataset.screen)));

  function rows(tbody, records, cols, onClick) {
    tbody.innerHTML = "";
    records.forEach((r) => {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      cols.forEach((c) => {
        const td = document.createElement("td");
        td.textContent = r[c] ?? "";
        tr.appendChild(td);
      });
      tr.addEventListener("click", () => onClick(r));
      tbody.appendChild(tr);
    });
  }

  async function refreshOrders() {
    const data = await api("/api/console/orders");
    rows(document.querySelector("#orders-table tbody"), data,
      ["id", "customer_id", "items", "total", "status", "ship_date", "carrier"],
      (r) => openOrder(r.id));
  }
  async function openOrder(id) {
    const r = await api("/api/console/orders/" + id);
    current.order = r;
    $("order-id-label").textContent = r.id;
    $("ord-status").value = r.status;
    $("ord-ship-date").value = r.ship_date || "";
    $("ord-carrier").value = r.carrier || "";
    $("order-form-panel").hidden = false;
  }
  $("order-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api("/api/console/orders/" + current.order.id, json("PUT", {
      fields: { status: $("ord-status").value,
                ship_date: $("ord-ship-date").value,
                carrier: $("ord-carrier").value },
    }));
    $("order-save-note").textContent = "Saved.";
    refreshOrders();
  });

  async function refreshCustomers() {
    const data = await api("/api/console/customers");
    rows(document.querySelector("#customers-table tbody"), data,
      ["id", "name", "email", "phone", "city", "credit_limit"],
      (r) => openCustomer(r.id));
  }
  async function openCustomer(id) {
    const r = await api("/api/console/customers/" + id);
    current.customer = r;
    $("customer-id-label").textContent = r.id;
    ["name", "email", "phone", "address", "city", "postcode", "credit-limit"]
      .forEach((f) => { $("cust-" + f).value = r[f.replace("-", "_")] ?? r[f] ?? ""; });
    $("cust-credit-limit").value = r.credit_limit;
    $("cust-bank").textContent = r.bank_account || "—";
    $("cust-card").textContent = r.card_last4 ? "•••• " + r.card_last4 : "—";
    $("cust-note").textContent = r.note || "";
    $("customer-form-panel").hidden = false;
  }
  $("customer-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api("/api/console/customers/" + current.customer.id, json("PUT", {
      fields: { name: $("cust-name").value, email: $("cust-email").value,
                phone: $("cust-phone").value, address: $("cust-address").value,
                city: $("cust-city").value, postcode: $("cust-postcode").value,
                credit_limit: $("cust-credit-limit").value },
    }));
    $("customer-save-note").textContent = "Saved.";
    refreshCustomers();
  });

  async function refreshInvoices() {
    const data = await api("/api/console/invoices");
    rows(document.querySelector("#invoices-table tbody"), data,
      ["id", "customer_id", "amount", "status", "due_date", "memo"],
      (r) => openInvoice(r.id));
  }
  async function openInvoice(id) {
    const r = await api("/api/console/invoices/" + id);
    current.invoice = r;
    $("invoice-id-label").textContent = r.id;
    $("inv-status").value = r.status;
    $("inv-due-date").value = r.due_date || "";
    $("inv-memo").value = r.memo || "";
    $("cm-invoice-id").value = r.id;
    $("invoice-form-panel").hidden = false;
    const memos = await api("/api/console/credit-memos");
    const mine = memos.filter((m) => m.invoice_id === r.id);
    $("cm-list").textContent = mine.length
      ? "Credit memos on this invoice: " + mine.map(
          (m) => `${m.id} — ${m.amount} (${m.reason})`).join("; ")
      : "No credit memos on this invoice yet.";
  }
  $("invoice-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await api("/api/console/invoices/" + current.invoice.id, json("PUT", {
      fields: { status: $("inv-status").value,
                due_date: $("inv-due-date").value,
                memo: $("inv-memo").value },
    }));
    $("invoice-save-note").textContent = "Saved.";
    refreshInvoices();
  });
  $("credit-memo-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const memo = await api("/api/console/credit-memos", json("POST", {
      fields: { invoice_id: $("cm-invoice-id").value,
                reason: $("cm-reason").value,
                amount: $("cm-amount").value, status: "issued" },
      task_id: $("cm-task-id").value || null,
    }));
    $("cm-note").textContent = "Issued " + memo.id + ".";
    openInvoice($("cm-invoice-id").value);
  });

  async function refreshTasks() {
    const data = await api("/api/tasks");
    const tbody = document.querySelector("#tasks-table tbody");
    tbody.innerHTML = "";
    data.forEach((t) => {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      [t.task_id, t.text, t.status, t.operator, t.driver || ""].forEach((v) => {
        const td = document.createElement("td"); td.textContent = v; tr.appendChild(td);
      });
      tr.addEventListener("click", () => openTask(t.task_id));
      tbody.appendChild(tr);
    });
  }

  async function renderDiff(taskId, fromV, toV, div) {
    const diff = await api(
      `/api/tasks/${taskId}/plan-diff?from_version=${fromV}&to_version=${toV}`);
    if (!diff.step_diffs.length) {
      div.textContent = `Plan v${fromV} → v${toV}: no differences.`;
      return;
    }
    div.textContent = diff.step_diffs.map((sd) => {
      if (sd.change !== "changed") return `step ${sd.seq}: ${sd.change}`;
      return sd.changes.map((c) =>
        `step ${sd.seq} · ${c.field}: '${c.old ?? ""}' → '${c.new ?? ""}'`
      ).join("\n");
    }).join("\n");
  }

  // The verification report, rendered from the stored record the
  // task detail carries — the same report the API serves at
  // /api/tasks/{id}/verification. Per-field verdicts, the linkage
  // checks, and the correction chain a discrepancy has spawned.
  function renderVerification(task, report) {
    const div = $("task-verification");
    div.innerHTML = "";
    $("task-verify").innerHTML = "";
    if (!report) return;
    const h = document.createElement("h3");
    h.style.marginTop = "16px";
    h.textContent = "Verification report";
    div.appendChild(h);
    const head = document.createElement("p");
    head.className = "note";
    head.textContent =
      `Plan v${report.plan_version} · driver ${report.driver} · read via ` +
      `${report.read_path} · ${report.verified_at} · ` +
      `${report.checked} field checks, ${report.linkage_checks.length} ` +
      `linkage checks — ${report.ok ? "all match" : "DIFFERENCES NAMED"}. ` +
      `Verification ran once, at the end of execution; the report is the ` +
      `record and is not recomputed.`;
    div.appendChild(head);
    const lines = [];
    report.field_checks.forEach((c) => lines.push(
      `${c.verdict === "match" ? "✓" : "✗"} step ${c.seq} · ` +
      `${c.record_kind} ${c.record_id}.${c.field}: planned ` +
      `'${c.expected}', read '${c.actual}'`));
    report.linkage_checks.forEach((c) => lines.push(
      `${c.verdict === "match" ? "✓" : "✗"} step ${c.seq} · linkage ` +
      `${c.check} (${c.record_kind} ${c.record_id}.${c.field}): ` +
      `${c.detail}`));
    report.corrections.forEach((c) => lines.push(
      `→ corrective ${c.task_id}: ${c.status}` +
      (c.verification_ok === null ? ""
        : c.verification_ok ? " — its verification passed"
                            : " — its verification named differences")));
    const pre = document.createElement("div");
    pre.className = "diff";
    pre.textContent = lines.join("\n");
    div.appendChild(pre);
    if (!report.ok) {
      $("task-verify").innerHTML =
        `<span class="err">Verification failed — differing fields: ` +
        report.diffs.map((d) =>
          `${d.record_kind} ${d.record_id}.${d.field}: planned ` +
          `'${d.planned}', actual '${d.actual}'`).join("; ") + "</span>";
    }
  }

  // The execution attempts, rendered from the stored records:
  // every attempt with the steps it completed and — for a failed
  // one — the step it stopped at and the driver's error verbatim,
  // plus the state lines naming what the attempt left in place.
  function renderAttempts(attempts) {
    const div = $("task-attempts");
    div.innerHTML = "";
    if (!attempts || !attempts.length) return;
    const h = document.createElement("h3");
    h.style.marginTop = "16px";
    h.textContent = "Execution attempts";
    div.appendChild(h);
    attempts.forEach((a) => {
      const head = document.createElement("p");
      head.className = "note";
      head.textContent =
        `Attempt ${a.number} (${a.mode}) · driver ${a.driver} · ` +
        `${a.outcome} · ${a.started_at}` +
        (a.finished_at ? ` → ${a.finished_at}` : "");
      div.appendChild(head);
      const lines = a.steps.map((s) =>
        s.ok ? `✓ step ${s.seq}: ${s.detail}`
             : `✗ step ${s.seq}: FAILED — ${s.detail}`);
      a.state_lines.forEach((l) => lines.push(`  left standing: ${l}`));
      const pre = document.createElement("div");
      pre.className = "diff";
      pre.textContent = lines.join("\n");
      div.appendChild(pre);
    });
  }

  async function openTask(taskId) {
    const { task, plan, plan_versions, verification, attempts } =
      await api("/api/tasks/" + taskId);
    current.task = task;
    $("task-detail").hidden = false;
    $("task-id-label").textContent = task.task_id;
    $("task-status-chip").innerHTML =
      `<span class="chip ${task.status}">${task.status}</span>`;
    $("task-refusal").textContent =
      task.status === "REFUSED" ? task.refusal_reason : "";
    $("task-stale").textContent = task.status === "STALE"
      ? "Stale — these records changed after the plan was derived: " +
        (task.stale_records || []).join(", ") +
        ". Nothing ran. Re-plan to derive a fresh version."
      : "";
    // The correction chain, both ways: a corrective names its
    // parent (click to open it); a parent lists its correctives in
    // its verification report above.
    const parentDiv = $("task-parent");
    parentDiv.innerHTML = "";
    if (task.parent_task_id) {
      parentDiv.appendChild(document.createTextNode(
        `This is a corrective task for ${task.parent_task_id}. `));
      const btn = document.createElement("button");
      btn.className = "act secondary";
      btn.textContent = "Open the parent task";
      btn.addEventListener("click", () => openTask(task.parent_task_id));
      parentDiv.appendChild(btn);
    }
    $("task-deviation").textContent =
      task.status === "DISCREPANCY_ACCEPTED"
        ? `Deviation accepted by ${task.deviation_accepted_by}: ` +
          `${task.deviation_reason}. The verification report below ` +
          `stands as written.`
        : "";
    $("task-failure").textContent =
      task.status === "EXECUTION_FAILED"
        ? `Execution failed at step ${task.failed_step}: ` +
          `${task.failure_error}. The steps before it stand as ` +
          `recorded in the attempt below — nothing was rolled back.`
        : "";
    $("task-abandoned").textContent =
      task.status === "ABANDONED"
        ? `Abandoned by ${task.abandoned_by}: ${task.abandon_reason}. ` +
          `The partial state stands on the records, named in the ` +
          `final attempt below.`
        : "";
    const planDiv = $("task-plan");
    if (plan) {
      planDiv.innerHTML = "";
      const head = document.createElement("p");
      head.innerHTML = `<b>Plan v${plan.version}</b>` +
        (plan.superseded ? " (superseded)" : "") + ` — ${plan.summary}`;
      planDiv.appendChild(head);
      const ol = document.createElement("ol"); ol.className = "steps";
      plan.steps.forEach((s) => {
        const li = document.createElement("li");
        li.textContent = s.instruction;
        ol.appendChild(li);
      });
      planDiv.appendChild(ol);
    } else { planDiv.innerHTML = ""; }
    // Version history + a compare view between any two versions.
    const plansDiv = $("task-plans");
    plansDiv.innerHTML = "";
    const versions = plan_versions || (plan ? [plan.version] : []);
    if (versions.length > 1) {
      const row = document.createElement("div");
      row.className = "field";
      const label = document.createElement("label");
      label.textContent = "Plan versions";
      const opts = versions.map((v) => `<option value="${v}">v${v}</option>`).join("");
      row.innerHTML = "";
      const fromSel = document.createElement("select");
      fromSel.innerHTML = opts;
      fromSel.value = String(versions[versions.length - 2]);
      const toSel = document.createElement("select");
      toSel.innerHTML = opts;
      toSel.value = String(versions[versions.length - 1]);
      const btn = document.createElement("button");
      btn.className = "act secondary"; btn.textContent = "Compare versions";
      const diffDiv = document.createElement("div");
      diffDiv.className = "diff"; diffDiv.style.marginTop = "8px";
      btn.addEventListener("click", () =>
        renderDiff(taskId, fromSel.value, toSel.value, diffDiv));
      row.appendChild(label); row.appendChild(fromSel);
      row.appendChild(toSel); row.appendChild(btn);
      plansDiv.appendChild(row); plansDiv.appendChild(diffDiv);
      renderDiff(taskId, fromSel.value, toSel.value, diffDiv);
    }
    $("task-amend").hidden = task.status !== "PLANNED";
    $("task-confirm").disabled = task.status !== "PLANNED";
    const canRun = task.status === "CONFIRMED" && !task.driver;
    $("task-run-offline").disabled = !canRun;
    $("task-run-live").disabled = !canRun;
    $("task-reject").disabled =
      !(task.status === "PLANNED" || task.status === "CONFIRMED");
    $("task-replan").disabled = task.status !== "STALE";
    $("task-discrepancy").hidden = task.status !== "DISCREPANCY";
    $("discrepancy-note").textContent = "";
    $("task-recovery").hidden = task.status !== "EXECUTION_FAILED";
    $("recovery-note").textContent = "";
    renderAttempts(attempts);
    renderVerification(task, verification);
    const audit = await api("/api/tasks/" + taskId + "/audit");
    $("task-audit").textContent = audit.map((a) =>
      `${a.ts}  ${a.event}  by ${a.actor}  ${JSON.stringify(a.detail)}`).join("\n");
  }

  $("task-create").addEventListener("click", async () => {
    const body = { text: $("task-text").value,
                   operator: $("task-operator").value || "operator" };
    const { task } = await api("/api/tasks", json("POST", body));
    $("task-create-note").textContent =
      task.status === "REFUSED" ? task.refusal_reason : "Plan drafted.";
    refreshTasks(); openTask(task.task_id);
  });
  // Task actions go through here so a refusal — the gate's 409, a
  // role's "not yours to confirm" — is shown in the console with
  // the server's own reason, never swallowed.
  async function actOnTask(path, body, after) {
    const res = await fetch(path, json("POST", body));
    const data = await res.json().catch(() => ({}));
    $("task-action-note").textContent = res.ok ? ""
      : "Refused: " + (data.detail || `${path} -> ${res.status}`);
    if (res.ok && after) await after();
    return res.ok;
  }
  const asOperator = () => $("task-operator").value || "operator";
  $("task-confirm").addEventListener("click", async () => {
    await actOnTask(`/api/tasks/${current.task.task_id}/confirm`,
      { operator: asOperator() },
      async () => { refreshTasks(); openTask(current.task.task_id); });
  });
  $("task-run-offline").addEventListener("click", async () => {
    await actOnTask(`/api/tasks/${current.task.task_id}/execute`,
      { driver: "offline", operator: asOperator() },
      async () => { refreshAll(); openTask(current.task.task_id); });
  });
  $("task-reject").addEventListener("click", async () => {
    await actOnTask(`/api/tasks/${current.task.task_id}/reject`,
      { operator: asOperator() },
      async () => { refreshTasks(); openTask(current.task.task_id); });
  });
  $("task-replan").addEventListener("click", async () => {
    await actOnTask(`/api/tasks/${current.task.task_id}/replan`,
      { operator: asOperator() },
      async () => { refreshTasks(); openTask(current.task.task_id); });
  });
  // The discrepancy exits. Spawning a corrective opens the new
  // task; accepting a deviation is a supervisor act with a
  // mandatory reason — both refusals surface with the server's
  // reason, like every other act.
  $("task-corrective").addEventListener("click", async () => {
    const res = await fetch(
      `/api/tasks/${current.task.task_id}/corrective`,
      json("POST", { operator: asOperator() }));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("discrepancy-note").textContent =
        "Refused: " + (body.detail || `-> ${res.status}`);
      return;
    }
    refreshTasks();
    openTask(body.task.task_id);
  });
  $("task-accept-deviation").addEventListener("click", async () => {
    await actOnTask(
      `/api/tasks/${current.task.task_id}/accept-deviation`,
      { operator: asOperator(),
        reason: $("deviation-reason").value },
      async () => { refreshTasks(); openTask(current.task.task_id); });
  });
  // The failure exits. Resume and restart are execution acts on
  // the failed task; abandon closes it with a mandatory reason.
  // Refusals — a moved basis, a create already landed — surface
  // with the server's reason, like every other act.
  $("task-resume").addEventListener("click", async () => {
    await actOnTask(`/api/tasks/${current.task.task_id}/resume`,
      { operator: asOperator() },
      async () => { refreshAll(); openTask(current.task.task_id); });
  });
  $("task-restart").addEventListener("click", async () => {
    await actOnTask(`/api/tasks/${current.task.task_id}/restart`,
      { operator: asOperator() },
      async () => { refreshAll(); openTask(current.task.task_id); });
  });
  $("task-abandon").addEventListener("click", async () => {
    await actOnTask(`/api/tasks/${current.task.task_id}/abandon`,
      { operator: asOperator(),
        reason: $("abandon-reason").value },
      async () => { refreshTasks(); openTask(current.task.task_id); });
  });
  $("task-amend-btn").addEventListener("click", async () => {
    const seq = parseInt($("amend-seq").value, 10);
    const field = $("amend-field").value.trim();
    const value = $("amend-value").value;
    const change = field ? { seq, fields: { [field]: value } }
                         : { seq, value };
    const res = await fetch(
      `/api/tasks/${current.task.task_id}/amend`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          operator: $("task-operator").value || "operator",
          changes: [change] }),
      });
    const body = await res.json().catch(() => ({}));
    $("amend-note").textContent = res.ok
      ? `Amended — plan v${body.plan.version} now awaits confirmation.`
      : (body.detail || `Amendment refused (${res.status})`);
    refreshTasks(); openTask(current.task.task_id);
  });

  async function refreshAll() {
    await Promise.all([refreshOrders(), refreshCustomers(),
                       refreshInvoices(), refreshTasks(),
                       refreshRoles()]);
  }
  refreshAll();

  window.OrderDesk = {
    showScreen, openOrder, openCustomer, openInvoice, openTask,
    refreshAll, current: () => current,
  };
})();
