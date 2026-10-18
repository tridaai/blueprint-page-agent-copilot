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

  async function openTask(taskId) {
    const { task, plan } = await api("/api/tasks/" + taskId);
    current.task = task;
    $("task-detail").hidden = false;
    $("task-id-label").textContent = task.task_id;
    $("task-status-chip").innerHTML =
      `<span class="chip ${task.status}">${task.status}</span>`;
    $("task-refusal").textContent =
      task.status === "REFUSED" ? task.refusal_reason : "";
    const planDiv = $("task-plan");
    if (plan) {
      planDiv.innerHTML = "";
      const head = document.createElement("p");
      head.innerHTML = `<b>Plan v${plan.version}</b> — ${plan.summary}`;
      planDiv.appendChild(head);
      const ol = document.createElement("ol"); ol.className = "steps";
      plan.steps.forEach((s) => {
        const li = document.createElement("li");
        li.textContent = s.instruction;
        ol.appendChild(li);
      });
      planDiv.appendChild(ol);
    } else { planDiv.innerHTML = ""; }
    $("task-confirm").disabled = task.status !== "PLANNED";
    const canRun = task.status === "CONFIRMED" && !task.driver;
    $("task-run-offline").disabled = !canRun;
    $("task-run-live").disabled = !canRun;
    const audit = await api("/api/tasks/" + taskId + "/audit");
    $("task-audit").textContent = audit.map((a) =>
      `${a.ts}  ${a.event}  by ${a.actor}  ${JSON.stringify(a.detail)}`).join("\n");
    const failed = audit.filter((a) => a.event === "verification_failed");
    $("task-verify").innerHTML = failed.length
      ? `<span class="err">Verification failed — differing fields: ` +
        failed[failed.length - 1].detail.diffs.map(
          (d) => `${d.record_kind} ${d.record_id}.${d.field}: planned ` +
                 `'${d.planned}', actual '${d.actual}'`).join("; ") + "</span>"
      : "";
  }

  $("task-create").addEventListener("click", async () => {
    const body = { text: $("task-text").value,
                   operator: $("task-operator").value || "operator" };
    const { task } = await api("/api/tasks", json("POST", body));
    $("task-create-note").textContent =
      task.status === "REFUSED" ? task.refusal_reason : "Plan drafted.";
    refreshTasks(); openTask(task.task_id);
  });
  $("task-confirm").addEventListener("click", async () => {
    await api(`/api/tasks/${current.task.task_id}/confirm`,
      json("POST", { operator: $("task-operator").value || "operator" }));
    refreshTasks(); openTask(current.task.task_id);
  });
  $("task-run-offline").addEventListener("click", async () => {
    await api(`/api/tasks/${current.task.task_id}/execute`,
      json("POST", { driver: "offline" }));
    refreshAll(); openTask(current.task.task_id);
  });

  async function refreshAll() {
    await Promise.all([refreshOrders(), refreshCustomers(),
                       refreshInvoices(), refreshTasks()]);
  }
  refreshAll();

  window.OrderDesk = {
    showScreen, openOrder, openCustomer, openInvoice, openTask,
    refreshAll, current: () => current,
  };
})();
