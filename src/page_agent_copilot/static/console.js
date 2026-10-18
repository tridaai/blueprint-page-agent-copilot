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
    if (entry.active === false) {
      $("task-role-line").textContent =
        `Acting as ${name} — DEACTIVATED: this name may perform no act. ` +
        `Its unfinished work is orphaned until a supervisor picks it up.`;
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
      (o) => `${o.id} → ${o.role}` +
        (o.active === false ? " (deactivated)" : "") +
        (o.prefers_spoken ? " (prefers the spoken form)" : "")
        ).join("\n");
    renderDirectory();
    updateRoleLine();
  }
  $("task-operator").addEventListener("input", updateRoleLine);

  // The operator directory, rendered from the same governed data
  // the API enforces: who holds which role, and who is
  // deactivated. Directory acts — enrol, assign, deactivate,
  // reactivate — are supervisor acts, taken as the name at the top
  // of this screen, and a refusal is shown with the server's
  // reason, like every other act in this console.
  async function dirAct(path, opts, note) {
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    $(note || "directory-note").textContent = res.ok ? ""
      : "Refused: " + (data.detail || `${path} -> ${res.status}`);
    if (res.ok) await refreshAll();
    return res.ok;
  }
  function renderDirectory() {
    const list = $("directory-list");
    if (!list) return;
    list.textContent = roleData.operators.map((o) =>
      `${o.id} → ${o.role}` +
      (o.active === false ? " — DEACTIVATED (may perform no act)"
                          : " — active")).join("\n");
    const sel = $("enroll-role");
    if (sel && !sel.options.length) {
      sel.innerHTML = roleData.roles.map(
        (r) => `<option value="${r.name}">${r.name}</option>`).join("");
    }
    const box = $("directory-actions");
    box.innerHTML = "";
    roleData.operators.forEach((o) => {
      const row = document.createElement("div");
      row.className = "field";
      const label = document.createElement("label");
      label.textContent = o.id;
      row.appendChild(label);
      const stateBtn = document.createElement("button");
      stateBtn.className = "act secondary";
      stateBtn.textContent = o.active === false
        ? "Reactivate" : "Deactivate";
      stateBtn.addEventListener("click", () => dirAct(
        `/api/operators/${o.id}/` +
          (o.active === false ? "reactivate" : "deactivate"),
        json("POST", { operator: asOperator() })));
      row.appendChild(stateBtn);
      const roleSel = document.createElement("select");
      roleSel.innerHTML = roleData.roles.map((r) =>
        `<option value="${r.name}"` +
        (r.name === o.role ? " selected" : "") +
        `>${r.name}</option>`).join("");
      row.appendChild(roleSel);
      const assignBtn = document.createElement("button");
      assignBtn.className = "act secondary";
      assignBtn.textContent = "Assign role";
      assignBtn.addEventListener("click", () => dirAct(
        `/api/operators/${o.id}/role`,
        json("PUT", { operator: asOperator(),
                      role: roleSel.value })));
      row.appendChild(assignBtn);
      const prefBtn = document.createElement("button");
      prefBtn.className = "act secondary";
      prefBtn.textContent = o.prefers_spoken
        ? "Spoken form: on" : "Spoken form: off";
      prefBtn.addEventListener("click", () => dirAct(
        `/api/operators/${o.id}/preference`,
        json("PUT", { operator: asOperator(),
                      prefers_spoken: !o.prefers_spoken })));
      row.appendChild(prefBtn);
      box.appendChild(row);
    });
  }
  $("enroll-btn").addEventListener("click", async () => {
    const ok = await dirAct("/api/operators", json("POST", {
      operator: asOperator(),
      name: $("enroll-name").value,
      role: $("enroll-role").value,
    }));
    if (ok) $("enroll-name").value = "";
  });

  // Orphaned work: a deactivated operator's unfinished tasks,
  // listed as they stand. Pickup is a supervisor's recorded act —
  // the task's operator becomes the supervisor, and the original
  // planner stays on the task.
  async function refreshOrphaned() {
    const items = await api("/api/operators/orphaned");
    const div = $("orphaned-list");
    if (!items.length) {
      div.textContent = "No orphaned work.";
      return;
    }
    div.innerHTML = "";
    items.forEach((o) => {
      const row = document.createElement("div");
      row.className = "field";
      const label = document.createElement("label");
      label.textContent = o.task_id;
      row.appendChild(label);
      const text = document.createElement("span");
      text.textContent =
        `${o.status} — planned by ${o.planned_by}, operator ` +
        `${o.operator} (deactivated): ${o.text}`;
      row.appendChild(text);
      const btn = document.createElement("button");
      btn.className = "act secondary";
      btn.textContent = "Pick up (supervisor)";
      btn.addEventListener("click", async () => {
        const ok = await actOnTask(
          `/api/tasks/${o.task_id}/pickup`,
          { operator: asOperator() },
          async () => { refreshAll(); openTask(o.task_id); });
        if (!ok) return;
      });
      row.appendChild(btn);
      div.appendChild(row);
    });
  }

  // The per-operator report and the attribution check, both
  // folded from the audit trail by the shell. The budget read
  // folds from the attempt records the same way: steps consumed
  // and exhaustions per operator, never from counters.
  async function refreshReport() {
    const [report, check, budget] = await Promise.all([
      api("/api/operators/report"), api("/api/attribution"),
      api("/api/operators/budget")]);
    $("attribution-line").textContent =
      `Attribution check: ${check.checked} acts scanned — ` +
      (check.ok
        ? "every act is attributable to a directory name or a " +
          "system component."
        : `${check.problems.length} act(s) cannot be attributed: ` +
          check.problems.map((p) =>
            `${p.event} in ${p.scope} by '${p.actor}' ` +
            `(${p.problem})`).join("; "));
    $("operator-report").textContent = report.map((r) =>
      `${r.operator} (${r.role}${r.active ? "" : ", deactivated"}) — ` +
      `${r.acts_total} acts · planned ${r.tasks_planned} · ` +
      `confirmed ${r.tasks_confirmed} · executed ` +
      `${r.tasks_executed} · refusals received ` +
      `${r.refusals_received}` +
      (r.refusals_received
        ? " (" + Object.entries(r.refusals_by_code)
            .map(([k, v]) => `${k} ×${v}`).join(", ") + ")"
        : "")).join("\n");
    $("operator-budget").textContent = budget.map((b) =>
      `${b.operator} — attempts ${b.attempts_run} · tasks executed ` +
      `${b.tasks_executed} · steps consumed ${b.steps_consumed} · ` +
      `budget exhaustions ${b.budget_exhaustions}`).join("\n");
  }

  // The task library, rendered from the same governed records
  // the API serves: every version with its health folded in.
  // Instantiating renders the pattern's text and states it as a
  // task — the plan it comes back with is the planner's
  // derivation, and a refusal is shown with the server's reason,
  // like every other act in this console.
  let libraryData = [];
  function libraryKey(t) { return t.name + ":v" + t.version; }
  function pickedTemplate() {
    const pick = $("library-pick").value;
    return libraryData.find((t) => libraryKey(t) === pick) || null;
  }
  function renderLibrarySlots() {
    const box = $("library-slots");
    box.innerHTML = "";
    const t = pickedTemplate();
    if (!t) return;
    t.slots.forEach((s) => {
      const row = document.createElement("div");
      row.className = "field";
      const label = document.createElement("label");
      label.textContent = s.name + (s.kind === "record"
        ? ` (${s.record_kind})` : ` (${s.kind})`);
      row.appendChild(label);
      const input = document.createElement("input");
      input.type = "text";
      input.id = "slot-" + s.name;
      const example = t.examples[0];
      if (example && example.slots[s.name] !== undefined)
        input.value = example.slots[s.name];
      row.appendChild(input);
      box.appendChild(row);
    });
  }
  async function refreshTemplates() {
    libraryData = await api("/api/templates");
    $("library-list").textContent = libraryData.map((t) => {
      const standing = t.default
        ? (t.promoted ? " · DEFAULT (promoted)" : " · DEFAULT (seeded)")
        : ` · standing: ${t.standing}`;
      const evaluation = t.evaluation
        ? ` · evaluated: ${t.evaluation.ok ? "pass" : "fail"} by ` +
          `${t.evaluation.evaluated_by} (hash ` +
          `${t.evaluation.hash_short}` +
          `${t.evaluation.hash_matches ? "" : ", stale"})`
        : " · not evaluated";
      return `${t.name} v${t.version}${t.current ? "" : " (superseded)"} — ` +
        `${t.description} · health: ` +
        (t.health.ok ? "OK — examples derive to their pinned shape"
                     : "BROKEN — " + t.health.examples
                         .filter((e) => !e.ok)
                         .map((e) => e.detail).join("; ")) +
        standing + evaluation +
        ` · pattern: ${t.pattern}`;
    }).join("\n");
    const pick = $("library-pick");
    const before = pick.value;
    pick.innerHTML = libraryData.map((t) =>
      `<option value="${libraryKey(t)}">${t.name} v${t.version}` +
      `${t.current ? "" : " (superseded)"}` +
      `${t.default ? " — default" : ""}</option>`).join("");
    if (libraryData.some((t) => libraryKey(t) === before))
      pick.value = before;
    renderLibrarySlots();
  }
  // The regression gate's two acts, on the picked version, in
  // the operator named above — the shell enforces every rule
  // (the publisher may neither vouch nor promote) and the
  // refusal shows here in the server's own words, like every
  // other refusal in this console.
  async function gateOnTemplate(path, done) {
    const t = pickedTemplate();
    if (!t) return;
    const res = await fetch(`/api/templates/${t.name}/${path}`,
      json("POST", { operator: asOperator(), version: t.version }));
    const body = await res.json().catch(() => ({}));
    $("library-note").textContent = res.ok ? done(t, body)
      : "Refused: " + (body.detail || `-> ${res.status}`);
    if (res.ok) await refreshTemplates();
  }
  $("library-evaluate").addEventListener("click", async () => {
    await gateOnTemplate("evaluate", (t, body) =>
      `Evaluated ${t.name} v${t.version}: ` +
      (body.ok ? "pass" : "fail") +
      ` — recorded against hash ${body.content_hash.slice(0, 12)}.`);
  });
  $("library-promote").addEventListener("click", async () => {
    await gateOnTemplate("promote", (t) =>
      `${t.name} v${t.version} is now the default.`);
  });
  $("library-pick").addEventListener("change", renderLibrarySlots);
  $("library-create").addEventListener("click", async () => {
    const t = pickedTemplate();
    if (!t) return;
    const slots = {};
    t.slots.forEach((s) => { slots[s.name] = $("slot-" + s.name).value; });
    const res = await fetch(
      `/api/templates/${t.name}/instantiate`,
      json("POST", { operator: asOperator(), version: t.version,
                     slots }));
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("library-note").textContent =
        "Refused: " + (body.detail || `-> ${res.status}`);
      return;
    }
    $("library-note").textContent =
      body.task.status === "REFUSED"
        ? body.task.refusal_reason
        : `Task stated from ${t.name} v${t.version}.`;
    refreshTasks();
    openTask(body.task.task_id);
  });
  $("pub-btn").addEventListener("click", async () => {
    let slots, examples;
    try {
      slots = JSON.parse($("pub-slots").value || "[]");
      examples = JSON.parse($("pub-examples").value || "[]");
    } catch (err) {
      $("pub-note").textContent = "Refused: slots and examples " +
        "must be JSON (" + err.message + ")";
      return;
    }
    const res = await fetch("/api/templates", json("POST", {
      operator: asOperator(), name: $("pub-name").value,
      pattern: $("pub-pattern").value,
      description: $("pub-description").value, slots, examples }));
    const body = await res.json().catch(() => ({}));
    $("pub-note").textContent = res.ok
      ? `Published ${body.name} v${body.version}.`
      : "Refused: " + (body.detail || `-> ${res.status}`);
    if (res.ok) await refreshTemplates();
  });

  // Batches: work submitted together, worked one task at a
  // time. The panel states a list of task lines as one batch
  // (template members ride the API), then shows each batch's
  // run sheet — every member's state, the next act it needs,
  // and who may take it — and its report, both folded by the
  // shell from the member records. There is no batch act here
  // either: the acts the sheet names are taken on the member
  // tasks, in the task view, one at a time.
  let batchData = [];
  async function refreshBatches() {
    batchData = await api("/api/batches");
    const div = $("batch-list");
    if (!batchData.length) {
      div.textContent = "No batches yet.";
      return;
    }
    div.innerHTML = "";
    batchData.forEach((b) => {
      const row = document.createElement("div");
      row.className = "field";
      const label = document.createElement("label");
      label.textContent = b.batch_id;
      row.appendChild(label);
      const text = document.createElement("span");
      text.textContent =
        `${b.status} — ${b.members_terminal}/${b.members_total} ` +
        `members in a terminal state · created by ${b.created_by}`;
      row.appendChild(text);
      const btn = document.createElement("button");
      btn.className = "act secondary";
      btn.textContent = "Run sheet & report";
      btn.addEventListener("click", () => openBatch(b.batch_id));
      row.appendChild(btn);
      div.appendChild(row);
    });
  }
  async function openBatch(batchId) {
    const [sheet, report] = await Promise.all([
      api(`/api/batches/${batchId}/run-sheet`),
      api(`/api/batches/${batchId}/report`)]);
    const box = $("batch-sheet");
    box.innerHTML = "";
    const head = document.createElement("h3");
    head.style.marginTop = "16px";
    head.textContent = `Batch ${sheet.batch_id} — ${sheet.status}`;
    box.appendChild(head);
    sheet.members.forEach((m) => {
      const row = document.createElement("div");
      row.className = "field";
      const label = document.createElement("label");
      label.textContent = `#${m.position}`;
      row.appendChild(label);
      const text = document.createElement("span");
      const what = m.kind === "template"
        ? `template ${m.template_name} v${m.template_version}`
        : "task";
      text.textContent =
        `${m.task_id || "(no task)"} · ${m.status} · ${what}: ` +
        `${m.text || ""} — next: ` +
        (m.next_act.action
          ? `${m.next_act.detail} (who: ${m.next_act.who})`
          : m.next_act.detail);
      row.appendChild(text);
      if (m.task_id) {
        const btn = document.createElement("button");
        btn.className = "act secondary";
        btn.textContent = "Open task";
        btn.addEventListener("click", () => openTask(m.task_id));
        row.appendChild(btn);
      }
      box.appendChild(row);
    });
    const rep = document.createElement("div");
    rep.className = "diff";
    rep.style.marginTop = "8px";
    const counts = Object.entries(report.counts_by_state)
      .map(([k, v]) => `${k} ×${v}`).join(" · ");
    rep.textContent =
      `Report: ${report.complete ? "complete" : "open"} — ` +
      `${counts} · total credited (from verification reports) ` +
      `${report.total_credited} · discrepancies open ` +
      `${report.discrepancies_open.length} · failures ` +
      `${report.failures.length}` +
      (report.owing_acts.length
        ? ` · still owing acts: ` +
          report.owing_acts.map((o) => o.task_id).join(", ")
        : "");
    box.appendChild(rep);
  }
  $("batch-create").addEventListener("click", async () => {
    const members = $("batch-lines").value.split("\n")
      .map((line) => line.trim()).filter(Boolean)
      .map((text) => ({ text }));
    const res = await fetch("/api/batches", json("POST", {
      operator: asOperator(), members }));
    const body = await res.json().catch(() => ({}));
    $("batch-note").textContent = res.ok
      ? `Batch ${body.batch_id} created — ${body.members_total} ` +
        `member(s). Confirm and run each member as its own task.`
      : "Refused: " + (body.detail || `-> ${res.status}`);
    if (res.ok) {
      $("batch-lines").value = "";
      await refreshAll();
      openBatch(body.batch_id);
    }
  });

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
      (a.navigations || []).forEach((n) => lines.push(
        `↪ before step ${n.seq}: navigated ${n.from_page} → `
        + `${n.to_page} (${n.driver})`));
      a.state_lines.forEach((l) => lines.push(`  left standing: ${l}`));
      const pre = document.createElement("div");
      pre.className = "diff";
      pre.textContent = lines.join("\n");
      div.appendChild(pre);
    });
  }

  // The execution trace (round 15): the attempts as one
  // timeline, folded by the shell from the records — events in
  // order (a navigation before the step it precedes, steps
  // with their before/after values), each attempt's wall clock
  // from its own stamps, and the ending the taxonomy names.
  // Per-step timings are not shown: neither driver records
  // them, so the trace invents none.
  async function renderTrace(taskId, ending) {
    const div = $("task-trace");
    div.innerHTML = "";
    const trace = await api("/api/tasks/" + taskId + "/trace");
    if (!trace.attempts.length) return;
    const h = document.createElement("h3");
    h.style.marginTop = "16px";
    h.textContent = "Execution trace";
    div.appendChild(h);
    if (ending) {
      const e = document.createElement("p");
      e.className = "note";
      e.textContent = "Ending: " + ending.label +
        (ending.detail && ending.detail.subreason
          ? " (" + ending.detail.subreason + ")" : "") +
        (ending.detail && ending.detail.code
          ? " (" + ending.detail.code + ")" : "");
      div.appendChild(e);
    }
    trace.attempts.forEach((a) => {
      const head = document.createElement("p");
      head.className = "note";
      head.textContent =
        `Attempt ${a.number} (${a.mode}) · ${a.driver} · ` +
        `${a.outcome} · ` +
        (a.duration_seconds != null
          ? `${a.duration_seconds}s wall clock`
          : "still running") +
        ` · steps ${a.budget.steps_consumed}` +
        (a.budget.step_ceiling
          ? ` of ceiling ${a.budget.step_ceiling}` : "") +
        (a.budget.budget_exhausted ? " · BUDGET EXHAUSTED" : "");
      div.appendChild(head);
      const lines = a.events.map((ev) =>
        ev.type === "navigation"
          ? `↪ before step ${ev.seq}: navigated ` +
            `${ev.from_page} → ${ev.to_page}`
          : ev.ok
            ? `✓ step ${ev.seq}: ${ev.detail} ` +
              `[${JSON.stringify(ev.before)} → ` +
              `${JSON.stringify(ev.after)}]`
            : `✗ step ${ev.seq}: FAILED — ${ev.detail}`);
      const pre = document.createElement("div");
      pre.className = "diff";
      pre.textContent = lines.join("\n");
      div.appendChild(pre);
    });
    if (trace.verification) {
      const v = document.createElement("p");
      v.className = "note";
      v.textContent =
        `Verification: ${trace.verification.ok
          ? "passed" : "FAILED"} — ` +
        `${trace.verification.checked} fields checked against ` +
        `plan v${trace.verification.plan_version}.`;
      div.appendChild(v);
    }
  }

  // The daily digest (round 15): the stored snapshots by date,
  // and the fold for the picked day. Generating acts as the
  // operator named at the top of this screen and replaces the
  // day's snapshot at a bumped version.
  function renderDigest(d) {
    const counts = (obj) => Object.entries(obj || {})
      .map(([k, v]) => `${k} ${v}`).join(", ") || "none";
    $("digest-view").textContent = [
      `${d.date} · digest v${d.version} · generated ` +
        `${d.generated_at} by ${d.generated_by}`,
      `tasks: ${d.tasks_total} — by status: ` +
        counts(d.counts_by_status),
      `by taxonomy: ` + counts(d.counts_by_taxonomy),
      `refusals by code: ` + counts(d.refusals_by_code),
      `credited (proven by verification reports): ` +
        `${d.total_credited}`,
      `clarifications: asked ${d.clarifications.asked}, ` +
        `answered ${d.clarifications.answered}, awaiting ` +
        `${d.clarifications.awaiting}`,
      `budget exhaustions: ${d.budget_exhaustions.count}` +
        (d.budget_exhaustions.task_ids.length
          ? ` (${d.budget_exhaustions.task_ids.join(", ")})` : ""),
      `discrepancies open: ` +
        `${d.discrepancies.open.length}` +
        (d.discrepancies.open.length
          ? ` (${d.discrepancies.open.join(", ")})` : "") +
        ` · accepted: ${d.discrepancies.accepted.length}`,
      `executions: ${d.executions.attempts_run} attempts, ` +
        `${d.executions.steps_consumed} steps consumed`,
      `bridge protocol refusals: ${d.protocol_refusals}`,
      `dual control: ` +
        `${(d.dual_control?.required || []).length} plan(s) ` +
        `under dual control, ` +
        `${(d.dual_control?.awaiting_second_confirmer || []).length}` +
        ` awaiting a second confirmer` +
        ((d.dual_control?.awaiting_second_confirmer || []).length
          ? ` (${d.dual_control.awaiting_second_confirmer.join(", ")})`
          : ""),
    ].join("\n");
  }

  async function refreshDigests() {
    const list = await api("/api/digests");
    $("digest-list").textContent = list.length
      ? "Stored digests: " + list.map((d) =>
          `${d.date} (v${d.version})`).join(" · ")
      : "No digests stored yet.";
    if (list.length) renderDigest(list[list.length - 1]);
  }

  $("digest-generate").addEventListener("click", async () => {
    const day = $("digest-date").value;
    if (!day) {
      $("digest-note").textContent = "Pick a day first.";
      return;
    }
    const res = await fetch("/api/digests/" + day, json("POST", {
      operator: $("task-operator").value || "operator" }));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("digest-note").textContent =
        "Refused: " + (data.detail || `-> ${res.status}`);
      return;
    }
    $("digest-note").textContent =
      `Digest for ${day} stored at v${data.version}.`;
    renderDigest(data);
    refreshDigests();
  });

  // The plan in its spoken form (round 13): rendered by the
  // shell from the stored plan — steps in order, figures
  // spelled, dates spoken. When the task's operator prefers
  // the spoken form, the detail carries it and the block leads
  // the task view, ahead of the written plan; otherwise it
  // reads as a plain secondary view under the same heading.
  // Either way it is a view: the stored plan never changes.
  async function renderSpoken(task, plan, detail) {
    const box = $("task-spoken");
    box.innerHTML = "";
    if (!plan) return;
    let view = detail.spoken;
    if (!view) {
      view = await api("/api/tasks/" + task.task_id +
                       "/plan/spoken");
    }
    const head = document.createElement("h3");
    head.style.marginTop = "16px";
    head.textContent = detail.operator_prefers_spoken
      ? "Plan, in spoken form — this operator works by voice, "
        + "so the spoken form leads"
      : "Plan, in spoken form";
    box.appendChild(head);
    const pre = document.createElement("div");
    pre.className = "diff";
    pre.style.whiteSpace = "pre-wrap";
    pre.textContent = view.spoken;
    box.appendChild(pre);
    const note = document.createElement("p");
    note.className = "note";
    note.textContent =
      "Spoken form of plan v" + view.plan_version +
      " (hash " + view.spoken_hash.slice(0, 12) + "…). " +
      "Confirming by read-back binds the confirmation to " +
      "these exact words.";
    box.appendChild(note);
  }

  // Clarification (round 14): a task whose derivation matched
  // a governed shape in more than one way parks here with a
  // question composed in code and options computed from the
  // records. The console offers exactly those options — one
  // button each, posting the option's value under the named
  // operator — and answered questions stay listed, so the
  // choices are visible on the task itself. There is no
  // free-text answer box: the shell never interprets a reply
  // into a value.
  function renderClarification(task) {
    const div = $("task-clarify");
    div.innerHTML = "";
    (task.clarifications || []).forEach((c) => {
      if (c.answer != null) {
        const done = document.createElement("p");
        done.textContent =
          `Clarified: ${c.question} → ${c.answer} ` +
          `(answered by ${c.answerer}).`;
        div.appendChild(done);
      }
    });
    if (task.status !== "AWAITING_CLARIFICATION") return;
    const pending = (task.clarifications || [])
      .find((c) => c.answer == null);
    if (!pending) return;
    const q = document.createElement("p");
    const lead = document.createElement("b");
    lead.textContent = "Clarification needed. ";
    q.appendChild(lead);
    q.appendChild(document.createTextNode(pending.question));
    div.appendChild(q);
    pending.options.forEach((o) => {
      const btn = document.createElement("button");
      btn.className = "act secondary";
      btn.textContent = o.label;
      btn.addEventListener("click", async () => {
        await actOnTask(`/api/tasks/${task.task_id}/clarify`,
          { operator: asOperator(), choice: o.value },
          async () => { refreshTasks(); openTask(task.task_id); });
      });
      div.appendChild(btn);
    });
    const note = document.createElement("p");
    note.className = "note";
    note.textContent =
      "Picking an option re-runs derivation with that choice " +
      "pinned. Nothing is planned until the question is " +
      "answered, and the answer is recorded on the task.";
    div.appendChild(note);
  }

  async function openTask(taskId) {
    const detail = await api("/api/tasks/" + taskId);
    const { task, plan, plan_versions, verification, attempts } =
      detail;
    current.task = task;
    current.readbackToken = "";
    $("task-detail").hidden = false;
    $("task-id-label").textContent = task.task_id;
    $("task-status-chip").innerHTML =
      `<span class="chip ${task.status}">${task.status}</span>`;
    $("task-refusal").textContent =
      task.status === "REFUSED" ? task.refusal_reason : "";
    renderClarification(task);
    // Dual control (round 18): a plan whose stakes reach the
    // governed threshold is confirmed by two distinct people.
    // The badge names the standing exactly — who has confirmed,
    // and whether a second, different confirmer is still owed.
    const dual = detail.dual_control || {};
    $("task-dual").textContent = !dual.required ? ""
      : dual.awaiting_second_confirmer
        ? `Dual control (${dual.reasons.join(" + ")}): ` +
          `${dual.confirmers[0]} has confirmed plan ` +
          `v${task.plan_version} — awaiting a second confirmer: ` +
          `a different confirm-capable operator, not the planner ` +
          `and not ${dual.confirmers[0]}.`
        : task.status === "PLANNED"
          ? `Dual control (${dual.reasons.join(" + ")}): this ` +
            `plan needs two distinct confirmers before it can ` +
            `execute.`
          : `Dual control (${dual.reasons.join(" + ")}): ` +
            `confirmed by ${(dual.confirmers || []).join(" and ")}.`;
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
    // Identity on the task: who planned it stays on the record
    // even after a pickup; orphaned work (its operator stands
    // deactivated) offers the pickup act to a supervisor.
    const pickupDiv = $("task-pickup");
    pickupDiv.innerHTML = "";
    if (task.template_name) {
      pickupDiv.appendChild(document.createTextNode(
        `Stated from the library template ${task.template_name} ` +
        `v${task.template_version} — the plan below is the ` +
        `planner's derivation from its rendered text. `));
    }
    if (task.planned_by && task.planned_by !== task.operator) {
      pickupDiv.appendChild(document.createTextNode(
        `Planned by ${task.planned_by}; the task now belongs to ` +
        `${task.operator}` +
        (task.picked_up_from
          ? ` (picked up from ${task.picked_up_from}). ` : ". ")));
    }
    const owner = roleData.operators.find((o) => o.id === task.operator);
    const orphanState = ["PLANNED", "CONFIRMED", "STALE",
      "EXECUTION_FAILED", "AWAITING_CLARIFICATION"]
      .includes(task.status);
    if (owner && owner.active === false && orphanState) {
      pickupDiv.appendChild(document.createTextNode(
        `Orphaned work: ${task.operator} is deactivated and this ` +
        `task still owes them an act. `));
      const btn = document.createElement("button");
      btn.className = "act secondary";
      btn.textContent = "Pick up this task (supervisor)";
      btn.addEventListener("click", async () => {
        await actOnTask(`/api/tasks/${task.task_id}/pickup`,
          { operator: asOperator() },
          async () => { refreshAll(); openTask(task.task_id); });
      });
      pickupDiv.appendChild(btn);
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
      if (plan.pages && plan.pages.length) {
        const pagesLine = document.createElement("p");
        pagesLine.className = "note";
        pagesLine.textContent =
          `Pages: ${plan.pages.join(" → ")}` +
          (plan.multi_page
            ? " — a multi-page flow: execution navigates between "
              + "these pages, and the navigation is on the attempt."
            : "");
        planDiv.appendChild(pagesLine);
      }
      const ol = document.createElement("ol"); ol.className = "steps";
      plan.steps.forEach((s) => {
        const li = document.createElement("li");
        li.textContent = s.instruction;
        ol.appendChild(li);
      });
      planDiv.appendChild(ol);
    } else { planDiv.innerHTML = ""; }
    renderSpoken(task, plan, detail);
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
    $("task-readback").hidden = task.status !== "PLANNED";
    $("task-readback-confirm").disabled = true;
    $("readback-spoken").textContent = "";
    $("readback-note").textContent = "";
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
    renderTrace(taskId, detail.ending);
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
      task.status === "REFUSED" ? task.refusal_reason
      : task.status === "AWAITING_CLARIFICATION"
        ? "Clarification needed — the task is open below; pick an option."
        : "Plan drafted.";
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
  // Read-back confirmation (round 13): "Read the plan aloud"
  // asks the shell for the spoken form and the token bound to
  // it, issued to the operator named above; "Confirm by
  // read-back" spends that token. The shell enforces every
  // ordinary rule plus the binding — an amendment after the
  // read-back refuses here with the server's reason, like
  // every other refusal in this console.
  $("task-readback-issue").addEventListener("click", async () => {
    const res = await fetch(
      `/api/tasks/${current.task.task_id}/readback`,
      json("POST", { operator: asOperator() }));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("readback-note").textContent =
        "Refused: " + (data.detail || `-> ${res.status}`);
      return;
    }
    current.readbackToken = data.token;
    $("readback-spoken").textContent = data.spoken;
    $("readback-note").textContent =
      `Read-back issued to ${data.issued_to} for plan ` +
      `v${data.plan_version}. Confirming now approves exactly ` +
      `the words above.`;
    $("task-readback-confirm").disabled = false;
  });
  $("task-readback-confirm").addEventListener("click", async () => {
    await actOnTask(
      `/api/tasks/${current.task.task_id}/confirm-readback`,
      { operator: asOperator(), token: current.readbackToken },
      async () => { refreshTasks(); openTask(current.task.task_id); });
  });
  $("task-create-transcript").addEventListener("click", async () => {
    const body = { text: $("task-text").value,
                   operator: $("task-operator").value || "operator" };
    const { task } = await api("/api/tasks/transcript",
                               json("POST", body));
    $("task-create-note").textContent =
      task.status === "REFUSED" ? task.refusal_reason : "Plan drafted.";
    refreshTasks(); openTask(task.task_id);
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

  // The deployment line in the footer: the profile this console
  // is running under — how the agent bundle was delivered, where
  // the model lives — and the Content-Security-Policy the shell
  // serves for it. Read from /api/agent-config, the same served
  // config the bridge constructs the agent from, so what the
  // footer states is what the page is actually running.
  async function refreshDeployment() {
    try {
      const cfg = await api("/api/agent-config");
      const where = cfg.endpointClass === "self_hosted"
        ? "self-hosted model" : "hosted model";
      const model = cfg.configured
        ? `${cfg.model} at ${cfg.baseURL}` : "not configured";
      $("deployment-line").textContent =
        `Deployment profile: ${cfg.profile} — ${cfg.delivery} ` +
        `bundle (${cfg.bundleURL}), ${where}: ${model}. ` +
        `CSP: ${cfg.csp}`;
    } catch (err) {
      $("deployment-line").textContent =
        "Deployment profile: unavailable (" + err.message + ")";
    }
  }

  async function refreshAll() {
    await Promise.all([refreshOrders(), refreshCustomers(),
                       refreshInvoices(), refreshTasks(),
                       refreshRoles(), refreshOrphaned(),
                       refreshReport(), refreshTemplates(),
                       refreshBatches(), refreshDigests()]);
  }
  refreshAll();
  refreshDeployment();

  window.OrderDesk = {
    showScreen, openOrder, openCustomer, openInvoice, openTask,
    refreshAll, current: () => current,
  };
})();
