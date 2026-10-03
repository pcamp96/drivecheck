"use strict";

const $ = (selector) => document.querySelector(selector);
const elements = {
  loginView: $("#login-view"), appView: $("#app-view"), loginForm: $("#login-form"),
  loginError: $("#login-error"), stream: $("#stream-state"), globalError: $("#global-error"),
  mode: $("#mode-flag"), connection: $("#connection-status"), driveCount: $("#drive-count"),
  activeSummary: $("#active-summary"), version: $("#version"), driveList: $("#drive-list"),
  drivesEmpty: $("#drives-empty"), activeEmpty: $("#active-empty"), activeContent: $("#active-content"),
  activeSubtitle: $("#active-subtitle"), activeDrive: $("#active-drive"), activeSerial: $("#active-serial"),
  activePercent: $("#active-percent"), progressBar: $("#progress-bar"), activeDetail: $("#active-detail"),
  phaseTrack: $("#phase-track"), cancelButton: $("#cancel-button"), runList: $("#run-list"),
  awaitingAction: $("#awaiting-action"), actionCountdown: $("#action-countdown"),
  actionExtended: $("#action-extended"), actionEject: $("#action-eject"),
  report: $("#report"), settingsForm: $("#settings-form"), autoTest: $("#auto-test"),
  autoEject: $("#auto-eject"), autoEjectDelay: $("#auto-eject-delay"), automationNote: $("#automation-note"), automationStatus: $("#automation-status"), platformNotice: $("#platform-notice"),
  notificationsEnabled: $("#notifications-enabled"), provider: $("#notification-provider"),
  discordFields: $("#discord-fields"), discordWebhook: $("#discord-webhook"),
  discordConfigured: $("#discord-configured"), forgetDiscord: $("#forget-discord"),
  telegramFields: $("#telegram-fields"),
  telegramToken: $("#telegram-token"), telegramChat: $("#telegram-chat"), telegramUser: $("#telegram-user"), providerNote: $("#provider-note"),
  telegramConfigured: $("#telegram-configured"), forgetTelegram: $("#forget-telegram"),
  notifyStarted: $("#notify-started"), notifyReady: $("#notify-ready"),
  destructiveSetting: $("#destructive-setting"), hardwareSetting: $("#hardware-setting"),
  settingsStatus: $("#settings-status"), verifyDialog: $("#verify-dialog"),
  verifyForm: $("#verify-form"), verifyDriveName: $("#verify-drive-name"),
  verifyPhrase: $("#verify-phrase"), verifyConfirmation: $("#verify-confirmation"),
  verifyError: $("#verify-error"), toasts: $("#toasts"),
  takeControlDialog: $("#take-control-dialog"), takeControlForm: $("#take-control-form"),
  takeControlDrive: $("#take-control-drive"), takeControlPhrase: $("#take-control-phrase"),
  takeControlConfirmation: $("#take-control-confirmation"), takeControlError: $("#take-control-error"),
  takeControlSubmit: $("#take-control-submit"),
  eraseDialog: $("#erase-dialog"), eraseForm: $("#erase-form"), eraseTitle: $("#erase-title"),
  eraseDrive: $("#erase-drive"), eraseMethod: $("#erase-method"), eraseDeadline: $("#erase-deadline"),
  erasePhrase: $("#erase-phrase"), eraseConfirmation: $("#erase-confirmation"),
  eraseError: $("#erase-error"), eraseSubmit: $("#erase-submit"),
  dashboardView: $("#dashboard-view"), settingsView: $("#settings"), pageTitle: $("#page-title")
};

let snapshot = null;
let events = null;
let selectedRunId = null;
let verifyDrive = null;
let takeControlDrive = null;
let eraseRequest = null;
let eraseDeadlineTimer = null;
let settingsDirty = false;
let automationSaving = false;
let reconnectTimer = null;
let actionCountdownTimer = null;
let driveRenderSignature = null;
let focusActiveRunOnRender = false;
const reportActionPending = new Set();
const reportActionNotes = new Map();
let choosingAction = false;
let chosenActionRunId = null;

function takeAccessFragment() {
  if (!window.location.hash.startsWith("#access=")) return null;
  const values = new URLSearchParams(window.location.hash.slice(1));
  const token = values.get("access");
  const runId = values.get("run") || "";
  history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  return token ? { token, runId } : null;
}

const accessFragment = takeAccessFragment();

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text ?? "—";
  return node;
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "Unknown size";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value;
  let index = 0;
  while (amount >= 1000 && index < units.length - 1) { amount /= 1000; index += 1; }
  const digits = index > 2 && amount < 10 ? 2 : (amount < 100 ? 1 : 0);
  return `${amount.toFixed(digits)} ${units[index]}`;
}

function formatDate(value, withTime = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return new Intl.DateTimeFormat(undefined, withTime
    ? { dateStyle: "medium", timeStyle: "short" }
    : { dateStyle: "medium" }).format(date);
}

function toast(message, kind = "success") {
  const item = textNode("div", message, `toast ${kind === "error" ? "error" : ""}`);
  elements.toasts.append(item);
  window.setTimeout(() => item.remove(), 4500);
}

function showError(message) {
  elements.globalError.textContent = message;
  elements.globalError.hidden = !message;
}

async function api(path, options = {}) {
  const config = { credentials: "same-origin", ...options };
  config.headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, config);
  if (response.status === 401) {
    showLogin();
    throw new Error("Your session has ended. Sign in again.");
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      detail = body.detail || body.message || detail;
      if (typeof detail !== "string") detail = JSON.stringify(detail);
    } catch (_) { /* response did not contain JSON */ }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function showLogin() {
  if (events) events.close();
  events = null;
  elements.appView.hidden = true;
  elements.loginView.hidden = false;
  $("#token").focus();
}

function showDashboard() {
  elements.loginView.hidden = true;
  elements.appView.hidden = false;
  syncDashboardView();
}

function syncDashboardView() {
  const settingsOpen = window.location.hash === "#settings";
  elements.dashboardView.hidden = settingsOpen;
  elements.settingsView.hidden = !settingsOpen;
  elements.pageTitle.textContent = settingsOpen ? "Station settings" : "Drive intake workbench";
  document.querySelectorAll(".station-rail nav a[data-view]").forEach((link) => {
    const current = settingsOpen ? link.dataset.view === "settings" : link.getAttribute("href") === (window.location.hash || "#dashboard");
    if (current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function setStreamState(state) {
  elements.stream.className = `stream-state ${state}`;
  if (state === "live") elements.stream.lastChild.textContent = "Live updates connected";
  else if (state === "offline") elements.stream.lastChild.textContent = "Updates disconnected — retrying";
  else elements.stream.lastChild.textContent = "Connecting to live updates";
}

function connectEvents() {
  if (events) events.close();
  window.clearTimeout(reconnectTimer);
  setStreamState("connecting");
  events = new EventSource("/api/events");
  events.addEventListener("state", (event) => {
    try {
      setStreamState("live");
      applySnapshot(JSON.parse(event.data));
    } catch (_) {
      setStreamState("offline");
    }
  });
  events.addEventListener("expired", () => {
    showLogin();
    elements.loginError.textContent = "Your session has ended. Sign in again.";
  });
  events.onopen = () => setStreamState("live");
  events.onerror = () => {
    setStreamState("offline");
    /* EventSource reconnects itself. The timer refreshes state if a proxy drops named events. */
    window.clearTimeout(reconnectTimer);
    reconnectTimer = window.setTimeout(() => refreshState().catch(() => {}), 4000);
  };
}

function activeRun() {
  if (!snapshot) return null;
  const id = snapshot.system?.active_run_id;
  return snapshot.runs.find((run) => run.id === id) ||
    snapshot.runs.find((run) => run.status === "running" || run.status === "queued") || null;
}

function awaitingActionRun() {
  if (!snapshot) return null;
  return snapshot.runs.find((run) => run.workflow_status === "awaiting_action") || null;
}

function applySnapshot(next) {
  snapshot = next;
  showDashboard();
  const systemErrors = [next.system?.station_error, next.system?.discovery_error, next.system?.notification_error, next.system?.telegram_error].filter(Boolean);
  showError(systemErrors.join(" "));
  renderStation();
  renderActive();
  renderDrives();
  renderRuns();
  renderSettings();
}

function renderStation() {
  const isDemo = snapshot.mode === "demo";
  const capabilities = snapshot.system?.capabilities || {};
  elements.mode.textContent = isDemo ? "Simulated drives · no hardware access" : `${snapshot.system?.platform || "Hardware"} · ${snapshot.settings?.headless ? "Headless intake" : "Hardware mode"}`;
  elements.platformNotice.textContent = isDemo ? "This preview uses simulated drives. Start with --hardware to discover attached external drives." : (capabilities.limitations || []).join(" ");
  elements.mode.classList.toggle("demo", isDemo);
  elements.connection.replaceChildren();
  const dot = document.createElement("span");
  dot.className = `status-dot ${snapshot.connected ? "live" : ""}`;
  elements.connection.append(dot, document.createTextNode(snapshot.connected ? "Online" : "Offline"));
  elements.driveCount.textContent = String(snapshot.drives?.length || 0);
  elements.activeSummary.textContent = awaitingActionRun() ? "Waiting for action" : (activeRun() ? "Running" : "None");
  elements.version.textContent = snapshot.version || "—";
}

const phaseOrder = ["smart_before", "self_test", "benchmark", "surface", "smart_after", "erase"];

function renderActive() {
  const run = activeRun() || awaitingActionRun();
  const awaiting = run?.workflow_status === "awaiting_action";
  const firmwareErase = runEraseMethod(run) === "ata_secure_erase";
  if (!awaiting || chosenActionRunId !== run?.id) chosenActionRunId = null;
  const cancelHadFocus = document.activeElement === elements.cancelButton;
  elements.activeEmpty.hidden = Boolean(run);
  elements.activeContent.hidden = !run;
  elements.cancelButton.hidden = !run || awaiting || firmwareErase;
  elements.awaitingAction.hidden = !awaiting;
  window.clearInterval(actionCountdownTimer);
  if (!run) {
    elements.activeSubtitle.textContent = "The station is ready for a drive.";
    if (cancelHadFocus || focusActiveRunOnRender) {
      focusActiveRunOnRender = false;
      $("#active-title").focus({ preventScroll: true });
    }
    return;
  }
  const drive = run.drive || {};
  elements.activeSubtitle.textContent = awaiting ? "Quick profile · Waiting for your choice" : `${profileLabel(run.profile)} profile · ${statusLabel(run.status)}`;
  elements.activeDrive.textContent = drive.model || run.drive_id || "Unknown drive";
  elements.activeSerial.textContent = drive.serial || "Serial unavailable";
  const progress = Math.max(0, Math.min(100, Number(run.progress) || 0));
  elements.activePercent.textContent = String(Math.round(progress));
  elements.progressBar.style.width = `${progress}%`;
  elements.activeDetail.textContent = run.detail || phaseLabel(run.phase);
  elements.cancelButton.dataset.runId = run.id;
  if (awaiting) {
    updateActionCountdown(run);
    actionCountdownTimer = window.setInterval(() => updateActionCountdown(run), 1000);
  }
  const runPhases = ["quick_erase", "full_erase"].includes(run.profile)
    ? ["erase"]
    : run.profile === "quick"
      ? ["smart_before", "benchmark", "smart_after"]
      : phaseOrder.filter((phase) => phase !== "erase");
  const currentIndex = runPhases.indexOf(run.phase);
  elements.phaseTrack.querySelectorAll("li").forEach((item) => {
    const phase = item.dataset.phase;
    const index = runPhases.indexOf(phase);
    const skipped = index === -1;
    item.classList.toggle("skipped", skipped);
    item.classList.toggle("current", !skipped && phase === run.phase);
    item.classList.toggle("done", !skipped && (currentIndex > index || (progress === 100 && run.results?.[phase])));
    item.title = skipped ? "Not included in the quick profile" : "";
  });
  if (focusActiveRunOnRender) {
    focusActiveRunOnRender = false;
    elements.cancelButton.focus({ preventScroll: true });
  }
}

function updateActionCountdown(run) {
  const deadline = new Date(run.lifecycle?.action_deadline || "");
  const remaining = Math.max(0, Math.ceil((deadline.valueOf() - Date.now()) / 1000));
  if (!Number.isFinite(deadline.valueOf())) {
    elements.actionCountdown.textContent = "Choose an Extended test or safely eject this drive.";
    return;
  }
  const minutes = Math.floor(remaining / 60);
  const seconds = String(remaining % 60).padStart(2, "0");
  elements.actionCountdown.textContent = remaining
    ? `Choose an action within ${minutes}:${seconds}. The drive ejects automatically when time runs out.`
    : "The choice window has ended. Waiting for the station to eject the drive.";
  const unavailable = remaining === 0 || choosingAction || chosenActionRunId === run.id;
  elements.actionExtended.disabled = unavailable;
  elements.actionEject.disabled = unavailable;
}

function renderDrives() {
  const drives = snapshot.drives || [];
  const busy = Boolean(activeRun()) || Boolean(awaitingActionRun()) || Boolean(snapshot.system?.release_in_progress);
  const capabilities = snapshot.system?.capabilities || {};
  const signature = JSON.stringify({
    busy,
    awaitingDriveId: awaitingActionRun()?.drive_id || awaitingActionRun()?.drive?.identity || null,
    allowDestructive: Boolean(snapshot.settings?.allow_destructive),
    capabilities: snapshot.system?.capabilities,
    drives: drives.map((drive) => ({
      id: drive.id, path: drive.path, model: drive.model, serial: drive.serial,
      size_bytes: drive.size_bytes, transport: drive.transport, eligible: drive.eligible,
      reasons: drive.reasons, identity: drive.identity, mounted: drive.mounted,
      ownership: drive.ownership
    }))
  });
  if (signature === driveRenderSignature) return;
  const focusedAction = elements.driveList.contains(document.activeElement)
    ? { driveId: document.activeElement.dataset.driveId, profile: document.activeElement.dataset.profile }
    : null;
  driveRenderSignature = signature;
  elements.driveList.replaceChildren();
  elements.drivesEmpty.hidden = drives.length > 0;
  drives.forEach((drive) => {
    const waiting = awaitingActionRun();
    const awaitingThisDrive = Boolean(waiting && (waiting.drive_id === drive.id || waiting.drive?.identity === drive.identity));
    const card = document.createElement("article");
    card.className = `drive-card ${drive.eligible ? "" : "ineligible"}`;
    const main = document.createElement("div");
    main.className = "drive-main";
    main.append(textNode("strong", drive.model || "Unknown drive"), textNode("span", drive.serial || "No serial reported", "serial"));
    const capacity = document.createElement("div");
    capacity.className = "drive-meta";
    capacity.append(textNode("span", "Capacity"), textNode("strong", formatBytes(drive.size_bytes)));
    const connection = document.createElement("div");
    connection.className = "drive-meta";
    connection.append(textNode("span", drive.path || "Unknown path"), textNode("strong", drive.transport || "Unknown transport"));
    const state = document.createElement("div");
    state.append(textNode("div", drive.eligible ? "Ready to test" : "Unavailable", "eligibility"));
    if (drive.reasons?.length) state.append(textNode("p", drive.reasons.map(reasonLabel).join(" · "), "drive-reasons"));
    const ownership = drive.ownership ? ownershipSummary(drive.ownership) : null;
    const actions = document.createElement("div");
    actions.className = "drive-actions";
    actions.append(
      runButton("Quick test", drive, "quick", busy || capabilities.can_test === false),
      runButton("Extended test", drive, "extended", busy || capabilities.can_test === false),
      runButton("Erase + verify", drive, "verify", busy || !snapshot.settings?.allow_destructive || capabilities.can_verify === false, true)
    );
    if (drive.eligible && capabilities.can_erase !== false) {
      actions.append(
        eraseButton("Quick erase", drive, "quick_erase", busy && !awaitingThisDrive),
        eraseButton("Full erase", drive, "full_erase", busy && !awaitingThisDrive)
      );
    }
    if (drive.mounted && capabilities.can_unmount) actions.append(releaseButton("Unmount for testing", drive, "unmount", busy));
    if (!drive.mounted && drive.eligible && capabilities.can_eject) actions.append(releaseButton("Eject drive", drive, "eject", busy));
    if (!drive.eligible && drive.ownership?.take_control_available) actions.append(takeControlButton(drive, busy));
    const eraseAvailability = !snapshot.settings?.allow_destructive && drive.eligible && capabilities.can_erase !== false
      ? " Quick erase and Full erase are disabled by the station’s destructive-operation setting."
      : "";
    const help = textNode("p", `Quick: SMART snapshots + read benchmark. Extended: adds a long self-test + full read scan. Erase + verify: destructive full-drive write/read checks.${eraseAvailability}`, "profile-help");
    card.append(main, capacity, connection, state, actions);
    if (ownership) card.append(ownership);
    card.append(help);
    elements.driveList.append(card);
  });
  if (focusedAction && !busy) {
    const restored = Array.from(elements.driveList.querySelectorAll("button")).find((button) =>
      button.dataset.driveId === focusedAction.driveId && button.dataset.profile === focusedAction.profile);
    restored?.focus({ preventScroll: true });
  }
}

function reasonLabel(reason) {
  return ({
    mounted: "Mounted volumes: unmount before testing",
    device_in_use: "The kernel has claimed this drive for RAID",
    missing_serial: "The USB bridge did not report a unique drive serial",
    ambiguous_serial: "Multiple serials reported; identity is ambiguous",
    duplicate_identity: "Duplicate drive identity reported by the adapter",
    not_external_usb: "Only external USB drives can be tested",
    system_drive: "System storage is protected",
    swap_in_use: "Drive contains active swap",
    not_physical_whole_disk: "A physical whole drive is required",
    system_state_unknown: "System disk safety could not be verified",
    apfs_state_unknown: "APFS safety could not be verified",
    invalid_size: "Drive capacity could not be verified",
    invalid_logical_sector: "Logical sector size could not be verified"
  })[reason] || reason;
}

function ownershipSummary(ownership) {
  const block = document.createElement("div");
  block.className = "ownership-summary";
  if (ownership.detail) block.append(textNode("p", ownership.detail));
  (Array.isArray(ownership.arrays) ? ownership.arrays : []).forEach((array) => {
    const path = array?.path || "RAID array";
    const state = array?.state || "state unknown";
    const members = Array.isArray(array?.members) ? array.members.join(", ") : (array?.members || "members not reported");
    block.append(textNode("p", `${path} · ${state} · Members: ${members}`));
  });
  return block;
}

function takeControlButton(drive, busy) {
  const button = textNode("button", "Take control", "quiet-button");
  button.type = "button";
  button.disabled = busy;
  button.dataset.driveId = drive.id;
  button.dataset.profile = "take-control";
  button.addEventListener("click", () => openTakeControl(drive));
  return button;
}

function eraseButton(label, drive, profile, busy) {
  const button = textNode("button", label, "danger-outline");
  button.type = "button";
  button.dataset.driveId = drive.id;
  button.dataset.profile = profile;
  button.disabled = busy || !snapshot.settings?.allow_destructive;
  if (!snapshot.settings?.allow_destructive) button.title = "Drive erase is disabled in the station configuration.";
  button.addEventListener("click", () => openErase(drive, profile));
  return button;
}

function runButton(label, drive, profile, extraDisabled, destructive = false) {
  const button = textNode("button", label, destructive ? "danger-outline" : (profile === "extended" ? "quiet-button" : ""));
  button.type = "button";
  button.dataset.driveId = drive.id;
  button.dataset.profile = profile;
  button.disabled = !drive.eligible || extraDisabled;
  if (destructive && !snapshot.settings?.allow_destructive) button.title = "Destructive verification is disabled in the station configuration.";
  button.addEventListener("click", () => destructive ? openVerify(drive) : startRun(drive, profile));
  return button;
}

function releaseButton(label, drive, action, busy) {
  const button = textNode("button", label, "quiet-button");
  button.type = "button";
  button.disabled = busy;
  button.dataset.driveId = drive.id;
  button.dataset.profile = action;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api(`/api/drives/${encodeURIComponent(drive.id)}/${action}`, { method: "POST" });
      toast(result.detail); await refreshState();
    } catch (error) { showError(error.message); toast(error.message, "error"); }
    finally { button.disabled = busy; }
  });
  return button;
}

function profileLabel(profile) {
  return ({ quick: "Quick", extended: "Extended", verify: "Erase + verify", quick_erase: "Quick erase", full_erase: "Full erase" })[profile] || profile || "Unknown";
}

function statusLabel(status) {
  if (!status) return "Unknown";
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function phaseLabel(phase) {
  return ({ queued: "Waiting to start", smart_before: "Checking drive health", self_test: "Running drive self-test", benchmark: "Measuring read performance", surface: "Scanning the full surface", smart_after: "Checking final drive health", erase: "Erasing the drive" })[phase] || phase || "Preparing";
}

function runEraseMethod(run) {
  return run?.erase_method || run?.expected_method || run?.results?.erase?.method || run?.results?.erase?.actual_method || null;
}

function eraseMethodLabel(method) {
  return ({
    ata_secure_erase: "ATA firmware Secure Erase",
    quick_format_exfat: "Quick Format (exFAT)",
    full_overwrite: "Complete overwrite"
  })[method] || method || "Method not reported";
}

function appendEraseEvidence(body, value) {
  const method = value.method || value.actual_method;
  const recoveryState = value.recovery_state || value.recovery || "Not reported";
  const warning = value.recovery_required || method === "quick_format_exfat";
  const overview = document.createElement("div");
  overview.className = `diagnostic-summary ${warning ? "warning" : (value.status || "passed")}`;
  overview.append(
    textNode("strong", eraseMethodLabel(method)),
    textNode("p", warning
      ? "This Quick Format is not secure. Old files may still be recoverable until their blocks are overwritten."
      : (method === "full_overwrite"
        ? "DriveCheck overwrote the complete addressable drive."
        : "The drive performed its firmware Secure Erase command."))
  );
  body.append(overview, evidenceRow("Actual erase method", eraseMethodLabel(method)), evidenceRow("Recovery state", recoveryState));
  if (value.detail) body.append(textNode("p", value.detail, "result-detail"));
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function displayValue(value) {
  if (value == null || value === "") return null;
  if (typeof value === "object") return value.string ?? value.value ?? null;
  return value;
}

function numericValue(value) {
  const displayed = displayValue(value);
  if (displayed == null || displayed === "") return null;
  const number = Number(displayed);
  return Number.isFinite(number) ? number : null;
}

function selfTestRecords(raw) {
  const ataLog = objectValue(raw.ata_smart_self_test_log);
  const ata = objectValue(ataLog.standard).table || objectValue(ataLog.extended).table;
  const scsi = objectValue(raw.scsi_self_test_log).table;
  const nvme = objectValue(raw.nvme_self_test_log).table;
  const table = [ata, scsi, nvme].find(Array.isArray) || [];
  return table.map((entry) => {
    const row = objectValue(entry);
    const status = displayValue(row.status) || displayValue(row.self_test_result) || displayValue(row.result) || "Result not reported";
    const type = displayValue(row.type) || displayValue(row.self_test_code) || displayValue(row.test_type);
    const lba = [row.lba, row.lba_of_first_error, row.failing_lba, row.address_of_first_error, row.first_error_lba]
      .map(numericValue).find((value) => value != null && value >= 0);
    const hours = [row.lifetime_hours, row.power_on_hours].map(numericValue).find((value) => value != null);
    return { status: String(status), type: type ? String(type) : "", lba, hours };
  });
}

function evidenceRow(label, value, note = "") {
  const row = document.createElement("div");
  row.className = "evidence-row";
  const copy = document.createElement("div");
  copy.append(textNode("strong", label), textNode("span", String(value)));
  row.append(copy);
  if (note) row.append(textNode("p", note));
  return row;
}

function selfTestExplanation(status) {
  const lowered = String(status || "").toLowerCase();
  if (lowered.includes("read failure")) return "The drive could not read part of its surface.";
  if (lowered.includes("write failure")) return "The drive could not write part of its surface.";
  if (lowered.includes("electrical")) return "The drive reported an electrical failure category. SMART does not identify the failed component or root cause.";
  if (lowered.includes("servo") || lowered.includes("seek failure")) return "The drive reported a positioning or servo failure category. SMART does not identify the root cause.";
  if (lowered.includes("abort")) return "The self-test stopped before completion, so it did not produce a passing result.";
  if (lowered.includes("interrupt")) return "The self-test was interrupted before completion, so it did not produce a passing result.";
  if (lowered.includes("completed without error") || lowered === "completed" || lowered === "passed") return "The drive completed this self-test without reporting an error.";
  if (lowered.includes("unknown") || lowered.includes("reserved")) return "The drive returned an unknown or nonstandard result code, so the outcome is not conclusive.";
  return "The drive returned this self-test result without enough detail to identify a hardware cause.";
}

function appendSmartEvidence(body, value, key) {
  const raw = objectValue(value.raw);
  const resultStatus = value.status || value.health || "unsupported";
  const overall = objectValue(raw.smart_status).passed;
  const records = selfTestRecords(raw);
  const overview = document.createElement("div");
  overview.className = `diagnostic-summary ${resultStatus}`;
  let heading;
  let explanation;
  if (key === "self_test") {
    heading = resultStatus === "failed" ? "This self-test found a drive error" :
      resultStatus === "passed" ? "This self-test completed without an error" :
        records.length ? "This self-test did not produce a conclusive result" : "No completed self-test result was available";
    explanation = !records.length
      ? "DriveCheck did not receive evidence that a self-test completed. Drive or adapter support, cancellation, or interruption may limit the result."
      : resultStatus === "failed"
        ? `${selfTestExplanation(records[0].status)} The failure belongs to this DriveCheck run; treat the drive as unsafe for trusted data.`
        : selfTestExplanation(records[0].status);
  } else if (overall === false) {
    heading = "SMART reports a current drive failure";
    explanation = "The drive’s current overall health flag is failing. Do not trust it with important data.";
  } else if (overall === true && ["warning", "failed"].includes(resultStatus)) {
    heading = "SMART’s overall check passes now, but recorded errors need attention";
    explanation = "The overall flag describes the drive’s current threshold state. Error logs and failed self-tests are separate evidence and may be historical.";
  } else if (overall === true) {
    heading = "SMART’s overall check passes right now";
    explanation = "No current overall SMART failure was reported in this snapshot.";
  } else {
    heading = "SMART could not provide an overall health result";
    explanation = "Adapter or drive support may limit the evidence available to DriveCheck.";
  }
  overview.append(textNode("strong", heading), textNode("p", explanation));
  if (key !== "self_test") overview.append(textNode("p", "A passing SMART overall check is only one signal. It does not guarantee that a drive is reliable."));
  body.append(overview);

  if (value.detail) body.append(textNode("p", value.detail, "result-detail"));
  if (value.warnings?.length) {
    const warnings = document.createElement("ul");
    warnings.className = "evidence-warnings";
    value.warnings.forEach((warning) => warnings.append(textNode("li", warning)));
    body.append(textNode("h5", "Reported warnings"), warnings);
  }

  const evidence = document.createElement("div");
  evidence.className = "evidence-grid";
  const attributes = objectValue(raw.ata_smart_attributes).table;
  const attributeLabels = {
    5: ["Reallocated sectors", "Sectors already replaced with drive spares."],
    197: ["Pending sectors", "Unstable sectors waiting to be retested or remapped."],
    198: ["Offline uncorrectable sectors", "Sectors the drive could not recover during an offline scan."]
  };
  if (Array.isArray(attributes)) {
    attributes.forEach((attribute) => {
      const row = objectValue(attribute);
      const id = Number(row.id);
      const rawValue = numericValue(objectValue(row.raw).value);
      if (attributeLabels[id] && rawValue != null) evidence.append(evidenceRow(attributeLabels[id][0], rawValue, attributeLabels[id][1]));
      const threshold = numericValue(row.thresh);
      const normalized = numericValue(row.value);
      const whenFailed = String(row.when_failed || "").trim();
      const failed = whenFailed && !["-", "never", "none"].includes(whenFailed.toLowerCase());
      if (failed || (threshold != null && threshold > 0 && normalized != null && normalized <= threshold)) {
        const name = row.name || `Attribute ${id}`;
        evidence.append(evidenceRow(`Failed SMART attribute: ${name}`, rawValue ?? normalized ?? "Reported", whenFailed ? `Drive state: ${whenFailed}` : "At or below its failure threshold."));
      }
    });
  }
  const nvme = objectValue(raw.nvme_smart_health_information_log);
  [["NVMe media errors", nvme.media_errors], ["NVMe error log entries", nvme.num_err_log_entries], ["NVMe percentage used", nvme.percentage_used], ["NVMe available spare", nvme.available_spare]].forEach(([label, entry]) => {
    const count = numericValue(entry);
    if (count != null) evidence.append(evidenceRow(label, label.includes("percentage") || label.includes("spare") ? `${count}%` : count));
  });
  const criticalWarning = numericValue(nvme.critical_warning);
  if (criticalWarning != null) evidence.append(evidenceRow("NVMe critical warning flags", criticalWarning, criticalWarning ? "One or more NVMe health warning flags are active." : "No NVMe critical warning flags are active."));
  const grownDefects = numericValue(raw.scsi_grown_defect_list);
  if (grownDefects != null) evidence.append(evidenceRow("SCSI grown defects", grownDefects));
  Object.entries(objectValue(raw.scsi_error_counter_log)).forEach(([operation, counters]) => {
    Object.entries(objectValue(counters)).forEach(([name, count]) => {
      if (!name.toLowerCase().includes("uncorrect")) return;
      const number = numericValue(count);
      if (number != null) evidence.append(evidenceRow(`SCSI ${operation} uncorrected errors`, number));
    });
  });
  if (evidence.childElementCount) body.append(textNode("h5", "Drive evidence"), evidence);

  if (records.length) {
    const history = document.createElement("div");
    history.className = "self-test-history";
    const title = key === "self_test" ? "Self-test result from this run" : "Recorded self-test history";
    history.append(textNode("h5", title));
    history.append(textNode("p", key === "self_test"
      ? "The first entry is the result DriveCheck observed for this test. Older entries may follow."
      : "These entries are stored by the drive and may predate this DriveCheck run.", "history-note"));
    records.slice(0, 5).forEach((record, index) => {
      const item = document.createElement("div");
      item.className = "self-test-record";
      item.append(textNode("strong", `${key === "self_test" && index === 0 ? "This run: " : ""}${record.type ? `${record.type} — ` : ""}${record.status}`));
      const facts = [];
      if (record.lba != null) facts.push(`Failure location: LBA ${record.lba.toLocaleString()}`);
      if (record.hours != null) facts.push(`Recorded at ${record.hours} power-on hours`);
      if (facts.length) item.append(textNode("span", facts.join(" · ")));
      item.append(textNode("p", selfTestExplanation(record.status)));
      history.append(item);
    });
    body.append(history);
  }
}

function connectedRunDrive(run) {
  const identity = run.drive?.identity;
  return (snapshot.drives || []).find((drive) => identity && drive.identity === identity) || null;
}

function reportActionButton(label, action, run, handler, disabled = false) {
  const button = textNode("button", label, action === "eject" ? "quiet-button" : "");
  button.type = "button";
  button.dataset.reportAction = action;
  button.dataset.runId = run.id;
  button.disabled = disabled || reportActionPending.has(`${run.id}:${action}`);
  button.addEventListener("click", () => handler(run, button));
  return button;
}

function appendReportActions(run) {
  const terminal = ["passed", "warning", "failed", "incomplete", "cancelled"].includes(run.status);
  if (!terminal) return null;
  const panel = document.createElement("section");
  panel.className = "report-actions";
  panel.setAttribute("aria-label", "Drive follow-up actions");
  panel.append(textNode("h4", "Next step"));
  const connected = connectedRunDrive(run);
  const stationBusy = Boolean(activeRun()) || Boolean(snapshot.system?.release_in_progress);
  const ejected = run.lifecycle?.eject_status === "ejected";
  const controls = document.createElement("div");
  controls.className = "report-action-buttons";
  if (ejected || !connected) {
    controls.append(reportActionButton("Reconnect / test again", "retest", run, retestRun, stationBusy));
    panel.append(
      textNode("p", "Reconnect the drive or power-cycle its USB dock, then choose this button again. Safe eject powers down and removes the device; mounting a filesystem does not reconnect it."),
      textNode("p", "If automatic intake is enabled, DriveCheck may queue a Quick test as soon as the drive is rediscovered, then wait for an Extended or Eject choice.", "report-action-note"),
      controls
    );
  } else if (connected.eligible) {
    controls.append(reportActionButton("Test again (read-only)", "retest", run, retestRun, stationBusy));
    if (snapshot.system?.capabilities?.can_eject !== false) {
      controls.append(reportActionButton("Safely eject drive", "eject", run, ejectRunDrive, stationBusy));
    }
    panel.append(textNode("p", "The same drive is connected and eligible. Retesting always queues the read-only Extended profile."), controls);
  } else {
    const reasons = connected.reasons?.length ? connected.reasons.map(reasonLabel).join(" · ") : "The drive did not pass the current safety checks.";
    panel.append(textNode("p", `The drive is connected but cannot be retested: ${reasons}`));
  }
  const note = reportActionNotes.get(run.id);
  if (note) panel.append(textNode("p", note, "report-action-status"));
  return panel;
}

async function retestRun(run, button) {
  const pendingKey = `${run.id}:retest`;
  reportActionPending.add(pendingKey);
  button.disabled = true;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(run.id)}/retest`, { method: "POST" });
    reportActionNotes.set(run.id, result.detail);
    toast(result.detail, result.status === "reconnect_required" ? "error" : "success");
    if (result.run && ["queued", "running"].includes(result.status)) {
      selectedRunId = result.run.id;
      focusActiveRunOnRender = true;
    }
    await refreshState();
  } catch (error) {
    reportActionNotes.set(run.id, error.message);
    showError(error.message);
    toast(error.message, "error");
  } finally {
    reportActionPending.delete(pendingKey);
    if (selectedRunId === run.id) {
      renderReport(snapshot.runs.find((item) => item.id === run.id) || run);
      (elements.report.querySelector('[data-report-action="retest"]') || elements.report.querySelector("h3"))?.focus({ preventScroll: true });
    }
  }
}

async function ejectRunDrive(run, button) {
  const drive = connectedRunDrive(run);
  if (!drive) return;
  const pendingKey = `${run.id}:eject`;
  reportActionPending.add(pendingKey);
  button.disabled = true;
  try {
    const result = await api(`/api/drives/${encodeURIComponent(drive.id)}/eject`, { method: "POST" });
    reportActionNotes.set(run.id, result.detail);
    toast(result.detail);
    await refreshState();
  } catch (error) {
    reportActionNotes.set(run.id, error.message);
    showError(error.message);
    toast(error.message, "error");
  } finally {
    reportActionPending.delete(pendingKey);
    if (selectedRunId === run.id) {
      renderReport(snapshot.runs.find((item) => item.id === run.id) || run);
      (elements.report.querySelector('[data-report-action="eject"]') || elements.report.querySelector("h3"))?.focus({ preventScroll: true });
    }
  }
}

function renderRuns() {
  const focusedRunId = elements.runList.contains(document.activeElement)
    ? document.activeElement.dataset.runId
    : null;
  elements.runList.replaceChildren();
  const runs = snapshot.runs || [];
  if (!runs.length) elements.runList.append(textNode("div", "Completed tests will appear here.", "empty-state"));
  if (selectedRunId && !runs.some((run) => run.id === selectedRunId)) selectedRunId = null;
  runs.forEach((run) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-item ${run.id === selectedRunId ? "selected" : ""}`;
    button.dataset.runId = run.id;
    button.setAttribute("role", "listitem");
    button.append(
      textNode("strong", run.drive?.model || run.drive_id || "Unknown drive"),
      textNode("span", statusLabel(run.status), `status-pill ${run.status || ""}`),
      textNode("small", `${profileLabel(run.profile)} · ${formatDate(run.created_at)}`)
    );
    button.addEventListener("click", () => { selectedRunId = run.id; renderRuns(); renderReport(run); });
    elements.runList.append(button);
  });
  if (focusedRunId) {
    const restored = Array.from(elements.runList.querySelectorAll(".run-item")).find((button) => button.dataset.runId === focusedRunId);
    restored?.focus({ preventScroll: true });
  }
  if (selectedRunId) {
    const selected = runs.find((run) => run.id === selectedRunId);
    if (selected) renderReport(selected);
  }
}

function renderReport(run) {
  const reportHadFocus = elements.report.contains(document.activeElement);
  const focusedAction = document.activeElement?.dataset?.reportAction;
  const sameReport = elements.report.dataset.runId === run.id;
  const openPhases = sameReport
    ? new Set(Array.from(elements.report.querySelectorAll("details[open][data-phase]")).map((details) => details.dataset.phase))
    : new Set();
  elements.report.replaceChildren();
  elements.report.dataset.runId = run.id;
  const header = document.createElement("div");
  header.className = "report-head";
  const title = document.createElement("div");
  const reportTitle = textNode("h3", run.drive?.model || run.drive_id || "Unknown drive");
  reportTitle.tabIndex = -1;
  title.append(reportTitle, textNode("p", `${profileLabel(run.profile)} test · ${run.drive?.serial || "No serial"}`));
  const downloads = document.createElement("div");
  downloads.className = "report-downloads";
  const readableDownload = textNode("a", "Export readable report", "quiet-button");
  readableDownload.href = `/api/runs/${encodeURIComponent(run.id)}/report.txt`;
  readableDownload.setAttribute("download", "");
  const jsonDownload = textNode("a", "Export JSON", "quiet-button");
  jsonDownload.href = `/api/runs/${encodeURIComponent(run.id)}/report`;
  jsonDownload.setAttribute("download", "");
  downloads.append(readableDownload, jsonDownload);
  header.append(title, downloads);
  const summary = document.createElement("div");
  summary.className = "report-summary";
  [["Status", statusLabel(run.status)], ["Progress", `${Math.round(Number(run.progress) || 0)}%`], ["Started", formatDate(run.started_at)], ["Finished", formatDate(run.finished_at)]].forEach(([label, value]) => {
    const cell = document.createElement("div"); cell.append(textNode("span", label), textNode("strong", value)); summary.append(cell);
  });
  const results = document.createElement("div");
  results.className = "result-list";
  const resultMap = run.results || {};
  const labels = { smart_before: "Initial SMART health", self_test: "Drive self-test", benchmark: "Read benchmark", surface: "Surface test", smart_after: "Final SMART health", erase: "Drive erase" };
  Object.keys(labels).forEach((key) => {
    const value = resultMap[key];
    if (value == null) return;
    const details = document.createElement("details");
    details.className = "result-row";
    details.dataset.phase = key;
    const resultStatus = value.status || value.health || "recorded";
    details.open = openPhases.has(key) || (!sameReport && ["failed", "warning"].includes(resultStatus));
    const summaryLine = document.createElement("summary");
    summaryLine.append(textNode("span", labels[key]), textNode("span", statusLabel(resultStatus), `status-pill ${resultStatus}`));
    const body = document.createElement("div");
    body.className = "result-body";
    if (key === "erase") {
      appendEraseEvidence(body, value);
    } else if (key === "smart_before" || key === "smart_after" || key === "self_test") {
      appendSmartEvidence(body, value, key);
    } else {
      if (value.read_mbps != null) body.append(textNode("p", `${Number(value.read_mbps).toFixed(1)} MB/s sequential read`));
      if (value.detail) body.append(textNode("p", value.detail));
      if (value.warnings?.length) body.append(textNode("p", value.warnings.join(" · ")));
    }
    const technical = document.createElement("details");
    technical.className = "technical-details";
    technical.append(textNode("summary", "Technical details"), textNode("pre", JSON.stringify(value, null, 2)));
    body.append(technical); details.append(summaryLine, body); results.append(details);
  });
  elements.report.append(header, summary);
  if (run.detail) elements.report.append(textNode("p", run.detail));
  if (run.lifecycle) {
    const life = run.lifecycle;
    elements.report.append(textNode("p", `Notification: ${life.notification_status || "not requested"} · Eject: ${life.eject_status || "not requested"}`, "configured-note"));
    if (life.eject_detail) elements.report.append(textNode("p", life.eject_detail));
  }
  const actions = appendReportActions(run);
  if (actions) elements.report.append(actions);
  elements.report.append(results);
  if (run.logs?.length) {
    elements.report.append(textNode("h4", "Run log"));
    const log = document.createElement("div"); log.className = "log-block";
    log.textContent = run.logs.map((entry) => `${formatDate(entry.time)}  ${entry.message}`).join("\n");
    elements.report.append(log);
  }
  if (reportHadFocus) {
    const restored = focusedAction
      ? Array.from(elements.report.querySelectorAll("[data-report-action]")).find((button) => button.dataset.reportAction === focusedAction)
      : null;
    (restored && !restored.disabled ? restored : reportTitle).focus({ preventScroll: true });
  }
}

function renderSettings(force = false) {
  const settings = snapshot.settings || {};
  const notifications = settings.notifications || {};
  if (!automationSaving) {
    elements.autoTest.checked = Boolean(settings.auto_test);
    elements.autoEject.checked = Boolean(settings.auto_eject);
    elements.autoEjectDelay.value = String(settings.auto_eject_delay_seconds ?? 180);
  }
  elements.autoTest.disabled = Boolean(settings.headless) || automationSaving;
  elements.autoEject.disabled = Boolean(settings.headless) || automationSaving;
  elements.autoEjectDelay.disabled = automationSaving;
  elements.automationNote.textContent = settings.headless ? "Headless mode: dock → Quick test → choice window → safe eject." : "Automatic intake only starts for unmounted external drives with a unique identity.";
  const managed = Boolean(settings.notifications_from_env);
  if (managed) elements.automationNote.textContent += " Notifications are managed by the station environment.";
  if (settingsDirty && !force) return;
  [elements.notificationsEnabled, elements.provider, elements.discordWebhook, elements.telegramToken, elements.telegramChat, elements.telegramUser, elements.notifyStarted, elements.notifyReady, elements.forgetDiscord, elements.forgetTelegram].forEach((field) => { field.disabled = managed; });
  elements.notificationsEnabled.checked = Boolean(notifications.enabled);
  elements.provider.value = notifications.provider || "none";
  elements.notifyStarted.checked = Boolean(notifications.notify_started);
  elements.notifyReady.checked = notifications.notify_ready !== false;
  elements.telegramChat.value = notifications.telegram_chat_id || "";
  elements.telegramUser.value = notifications.telegram_user_id || "";
  elements.discordWebhook.value = "";
  elements.telegramToken.value = "";
  elements.discordConfigured.textContent = notifications.discord_configured ? "A webhook is saved." : "No webhook saved.";
  elements.telegramConfigured.textContent = notifications.telegram_configured ? "Telegram credentials saved" : "Telegram credentials not configured";
  elements.telegramConfigured.classList.toggle("is-saved", Boolean(notifications.telegram_configured));
  elements.telegramToken.placeholder = notifications.telegram_configured ? "Token saved · leave blank to keep it" : "Enter your Telegram bot token";
  elements.forgetDiscord.hidden = !notifications.discord_configured;
  elements.forgetTelegram.hidden = !notifications.telegram_configured;
  elements.destructiveSetting.textContent = settings.allow_destructive ? "Enabled" : "Disabled";
  elements.hardwareSetting.textContent = snapshot.mode === "demo" ? "Simulated only" : (snapshot.system?.capabilities?.can_test ? "Testing enabled" : "Inventory only");
  showProviderFields();
}

function showProviderFields() {
  elements.discordFields.hidden = elements.provider.value !== "discord";
  elements.telegramFields.hidden = elements.provider.value !== "telegram";
  elements.providerNote.textContent = elements.provider.value === "telegram"
    ? "Telegram supports dashboard sign-in links and interactive test choices."
    : (elements.provider.value === "discord" ? "Discord sends notifications and readable reports." : "Choose a provider to send updates.");
}

async function refreshState() {
  const state = await api("/api/state");
  applySnapshot(state);
}

async function startRun(drive, profile, confirmation) {
  showError("");
  try {
    const body = { drive_id: drive.id, profile };
    if (confirmation) body.confirmation = confirmation;
    const run = await api("/api/runs", { method: "POST", body });
    selectedRunId = run?.id || selectedRunId;
    focusActiveRunOnRender = true;
    toast(`${profileLabel(profile)} test queued for ${drive.model || drive.serial}.`);
    await refreshState();
    document.querySelector("#active").scrollIntoView({ behavior: "smooth" });
  } catch (error) { showError(error.message); toast(error.message, "error"); }
}

function openVerify(drive) {
  verifyDrive = drive;
  const phrase = `ERASE ${drive.serial}`;
  elements.verifyDriveName.textContent = drive.model || drive.serial;
  elements.verifyPhrase.textContent = phrase;
  elements.verifyConfirmation.value = "";
  elements.verifyError.textContent = "";
  elements.verifyDialog.showModal();
  elements.verifyConfirmation.focus();
}

function openTakeControl(drive) {
  takeControlDrive = Object.freeze({
    id: drive.id,
    model: drive.model || "Unknown drive",
    serial: drive.serial || "",
    size_bytes: drive.size_bytes
  });
  const phrase = `TAKE CONTROL ${takeControlDrive.serial}`;
  elements.takeControlDrive.textContent = `${takeControlDrive.model} · ${takeControlDrive.serial || "Serial unavailable"} · ${formatBytes(takeControlDrive.size_bytes)}`;
  elements.takeControlPhrase.textContent = phrase;
  elements.takeControlConfirmation.value = "";
  elements.takeControlError.textContent = "";
  elements.takeControlSubmit.disabled = false;
  elements.takeControlDialog.showModal();
  elements.takeControlConfirmation.focus();
}

function closeTakeControl() {
  elements.takeControlDialog.close();
  elements.takeControlError.textContent = "";
  elements.takeControlConfirmation.value = "";
  takeControlDrive = null;
}

function updateEraseDeadline() {
  if (!eraseRequest) return;
  const waiting = awaitingActionRun();
  const sameDrive = waiting && (waiting.drive_id === eraseRequest.id || waiting.drive?.identity === eraseRequest.identity);
  const deadline = new Date(sameDrive ? waiting.lifecycle?.action_deadline || "" : "");
  if (!sameDrive || !Number.isFinite(deadline.valueOf())) {
    elements.eraseDeadline.textContent = "";
    return;
  }
  const remaining = Math.max(0, Math.ceil((deadline.valueOf() - Date.now()) / 1000));
  elements.eraseDeadline.textContent = remaining
    ? `Quick choice window: ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")} remaining. Opening this dialog does not pause the timer.`
    : "The Quick choice window has ended. The server will refuse a stale erase request.";
}

async function openErase(drive, profile) {
  const request = Object.freeze({
    id: drive.id,
    identity: drive.identity,
    model: drive.model || "Unknown drive",
    serial: drive.serial || "",
    size_bytes: drive.size_bytes,
    profile,
    method: null
  });
  eraseRequest = request;
  const quick = profile === "quick_erase";
  const phrase = `${quick ? "QUICK ERASE" : "FULL ERASE"} ${request.serial}`;
  elements.eraseTitle.textContent = quick ? "Quick erase this drive?" : "Fully erase this drive?";
  elements.eraseDrive.textContent = `${request.model} · ${request.serial || "Serial unavailable"} · ${formatBytes(request.size_bytes)}`;
  elements.erasePhrase.textContent = phrase;
  elements.eraseConfirmation.value = "";
  elements.eraseError.textContent = "";
  elements.eraseMethod.className = "erase-method";
  elements.eraseMethod.textContent = "Checking the available erase method…";
  elements.eraseSubmit.disabled = true;
  elements.eraseSubmit.textContent = quick ? "Quick erase" : "Full erase";
  elements.eraseDialog.showModal();
  updateEraseDeadline();
  window.clearInterval(eraseDeadlineTimer);
  eraseDeadlineTimer = window.setInterval(updateEraseDeadline, 1000);
  try {
    const plan = await api(`/api/drives/${encodeURIComponent(request.id)}/erase-plan`);
    if (eraseRequest !== request) return;
    const option = quick ? plan.quick : plan.full;
    if (!option?.available || !option.method) {
      elements.eraseMethod.textContent = option?.detail || "This erase method is not available for the drive.";
      elements.eraseError.textContent = "Erase is unavailable.";
      return;
    }
    eraseRequest = Object.freeze({ ...request, method: option.method });
    const estimate = option.estimated_minutes != null ? ` Estimated time: about ${option.estimated_minutes} minutes.` : "";
    if (option.method === "quick_format_exfat" || option.secure === false) {
      elements.eraseMethod.className = "erase-method warning";
      elements.eraseMethod.textContent = `${eraseMethodLabel(option.method)}. NOT SECURE: old files may be recoverable.${estimate} ${option.detail || ""}`.trim();
    } else {
      elements.eraseMethod.textContent = `${eraseMethodLabel(option.method)}.${estimate} ${option.detail || ""}`.trim();
    }
    elements.eraseSubmit.disabled = false;
    elements.eraseConfirmation.focus();
  } catch (error) {
    if (eraseRequest === request) elements.eraseError.textContent = error.message;
  }
}

function closeErase() {
  elements.eraseDialog.close();
  window.clearInterval(eraseDeadlineTimer);
  eraseDeadlineTimer = null;
  eraseRequest = null;
  elements.eraseError.textContent = "";
  elements.eraseConfirmation.value = "";
}

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.loginError.textContent = "";
  const button = elements.loginForm.querySelector("button"); button.disabled = true;
  try {
    await api("/api/login", { method: "POST", body: { token: $("#token").value } });
    $("#token").value = "";
    await refreshState(); connectEvents();
  } catch (error) { elements.loginError.textContent = error.message; }
  finally { button.disabled = false; }
});

$("#logout-button").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_) { /* local logout still proceeds */ }
  snapshot = null; selectedRunId = null; showLogin();
});

$("#scan-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await api("/api/scan", { method: "POST" }); await refreshState(); toast("Drive scan complete."); }
  catch (error) { showError(error.message); toast(error.message, "error"); }
  finally { button.disabled = false; }
});

elements.cancelButton.addEventListener("click", async () => {
  const runId = elements.cancelButton.dataset.runId;
  if (!runId) return;
  elements.cancelButton.disabled = true;
  try { await api(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }); toast("Cancellation requested."); await refreshState(); }
  catch (error) { showError(error.message); toast(error.message, "error"); }
  finally { elements.cancelButton.disabled = false; }
});

async function chooseWaitingAction(action) {
  const run = awaitingActionRun();
  if (!run || choosingAction) return;
  choosingAction = true;
  elements.actionExtended.disabled = true;
  elements.actionEject.disabled = true;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(run.id)}/action`, { method: "POST", body: { action } });
    chosenActionRunId = run.id;
    toast(result?.detail || (action === "extended" ? "Extended test requested." : "Safe eject requested."));
    await refreshState();
  } catch (error) {
    showError(error.message);
    toast(error.message, "error");
  } finally {
    choosingAction = false;
    const waiting = awaitingActionRun();
    if (waiting) updateActionCountdown(waiting);
  }
}

elements.actionExtended.addEventListener("click", () => chooseWaitingAction("extended"));
elements.actionEject.addEventListener("click", () => chooseWaitingAction("eject"));

elements.verifyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const phrase = `ERASE ${verifyDrive?.serial || ""}`;
  if (elements.verifyConfirmation.value !== phrase) {
    elements.verifyError.textContent = `Enter “${phrase}” exactly.`;
    elements.verifyConfirmation.focus(); return;
  }
  elements.verifyDialog.close();
  await startRun(verifyDrive, "verify", phrase);
});

$("#verify-close").addEventListener("click", () => elements.verifyDialog.close());
$("#verify-cancel").addEventListener("click", () => elements.verifyDialog.close());

elements.takeControlConfirmation.addEventListener("input", () => {
  elements.takeControlError.textContent = "";
});
elements.takeControlForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!takeControlDrive) return;
  const drive = takeControlDrive;
  const phrase = `TAKE CONTROL ${drive.serial}`;
  if (elements.takeControlConfirmation.value !== phrase) {
    elements.takeControlError.textContent = `Enter “${phrase}” exactly.`;
    elements.takeControlConfirmation.focus();
    return;
  }
  elements.takeControlSubmit.disabled = true;
  elements.takeControlError.textContent = "";
  try {
    const result = await api(`/api/drives/${encodeURIComponent(drive.id)}/take-control`, {
      method: "POST",
      body: { confirmation: phrase }
    });
    selectedRunId = result.run?.id || selectedRunId;
    closeTakeControl();
    focusActiveRunOnRender = true;
    toast(result.detail || "Inactive RAID claim released. Read-only Quick test queued.");
    await refreshState();
    document.querySelector("#active").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    elements.takeControlError.textContent = error.message;
  } finally {
    elements.takeControlSubmit.disabled = false;
  }
});
$("#take-control-close").addEventListener("click", closeTakeControl);
$("#take-control-cancel").addEventListener("click", closeTakeControl);

elements.eraseConfirmation.addEventListener("input", () => {
  elements.eraseError.textContent = "";
});
elements.eraseForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!eraseRequest?.method) return;
  const request = eraseRequest;
  const phrase = `${request.profile === "quick_erase" ? "QUICK ERASE" : "FULL ERASE"} ${request.serial}`;
  if (elements.eraseConfirmation.value !== phrase) {
    elements.eraseError.textContent = `Enter “${phrase}” exactly.`;
    elements.eraseConfirmation.focus();
    return;
  }
  elements.eraseSubmit.disabled = true;
  elements.eraseError.textContent = "";
  try {
    const result = await api(`/api/drives/${encodeURIComponent(request.id)}/erase`, {
      method: "POST",
      body: { profile: request.profile, confirmation: phrase, expected_method: request.method }
    });
    selectedRunId = result.run?.id || selectedRunId;
    closeErase();
    focusActiveRunOnRender = Boolean(result.run);
    toast(result.detail || `${profileLabel(request.profile)} queued.`);
    await refreshState();
    document.querySelector("#active").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    elements.eraseError.textContent = error.message;
    updateEraseDeadline();
  } finally {
    if (eraseRequest) elements.eraseSubmit.disabled = false;
  }
});
$("#erase-close").addEventListener("click", closeErase);
$("#erase-cancel").addEventListener("click", closeErase);
elements.eraseDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeErase();
});

elements.settingsForm.addEventListener("input", (event) => {
  if (![elements.autoTest, elements.autoEject, elements.autoEjectDelay].includes(event.target)) settingsDirty = true;
});

async function saveAutomation(field, key, desired = field.checked) {
  automationSaving = true;
  elements.automationStatus.textContent = "Saving automation settings…";
  renderSettings();
  try {
    const saved = await api("/api/settings", { method: "PUT", body: { [key]: desired } });
    snapshot.settings[key] = saved[key];
    elements.automationStatus.textContent = "Automation settings saved.";
  } catch (error) {
    elements.automationStatus.textContent = `Could not save: ${error.message}`;
    toast(error.message, "error");
  } finally {
    automationSaving = false;
    renderSettings();
  }
}

elements.autoTest.addEventListener("change", () => saveAutomation(elements.autoTest, "auto_test"));
elements.autoEject.addEventListener("change", () => saveAutomation(elements.autoEject, "auto_eject"));
elements.autoEjectDelay.addEventListener("change", () => {
  if (!elements.autoEjectDelay.reportValidity()) return;
  saveAutomation(elements.autoEjectDelay, "auto_eject_delay_seconds", Number(elements.autoEjectDelay.value));
});
elements.provider.addEventListener("change", showProviderFields);
elements.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const saveButton = elements.settingsForm.querySelector("button[type=submit]"); saveButton.disabled = true;
  elements.settingsStatus.textContent = "Saving…";
  const notificationSettings = {
    provider: elements.provider.value,
    enabled: elements.notificationsEnabled.checked,
    discord_webhook: elements.discordWebhook.value,
    telegram_token: elements.telegramToken.value,
    telegram_chat_id: elements.telegramChat.value,
    telegram_user_id: elements.telegramUser.value,
    notify_started: elements.notifyStarted.checked,
    notify_ready: elements.notifyReady.checked
  };
  try {
    await api("/api/settings", { method: "PUT", body: { ...(snapshot.settings?.notifications_from_env ? {} : { notifications: notificationSettings }) } });
    settingsDirty = false; elements.settingsStatus.textContent = "Settings saved.";
    await refreshState(); renderSettings(true); toast("Settings saved.");
  } catch (error) { elements.settingsStatus.textContent = error.message; toast(error.message, "error"); }
  finally { saveButton.disabled = false; }
});

$("#test-notification").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  elements.settingsStatus.textContent = "Sending test message…";
  try { await api("/api/notifications/test", { method: "POST" }); elements.settingsStatus.textContent = "Test message sent."; toast("Test notification sent."); }
  catch (error) { elements.settingsStatus.textContent = error.message; toast(error.message, "error"); }
  finally { button.disabled = false; }
});

async function forgetCredentials(provider, button) {
  button.disabled = true;
  const label = provider === "discord" ? "Discord webhook" : "Telegram credentials";
  try {
    await api("/api/settings", {
      method: "PUT",
      body: { notifications: { enabled: false, [provider === "discord" ? "clear_discord" : "clear_telegram"]: true } }
    });
    settingsDirty = false;
    await refreshState();
    renderSettings(true);
    elements.settingsStatus.textContent = `${label} removed.`;
    toast(`${label} removed.`);
  } catch (error) {
    elements.settingsStatus.textContent = error.message;
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

elements.forgetDiscord.addEventListener("click", () => forgetCredentials("discord", elements.forgetDiscord));
elements.forgetTelegram.addEventListener("click", () => forgetCredentials("telegram", elements.forgetTelegram));
window.addEventListener("hashchange", syncDashboardView);

$("#date-line").textContent = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" }).format(new Date());

async function exchangeAccessLink(link) {
  try {
    const response = await fetch("/api/access", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ token: link.token })
    });
    if (!response.ok) throw new Error("This sign-in link has expired or was already used.");
    const result = await response.json();
    selectedRunId = result.run_id || link.runId || null;
    await refreshState();
    connectEvents();
    if (selectedRunId) document.querySelector("#history").scrollIntoView();
  } catch (error) {
    showLogin();
    elements.loginError.textContent = `${error.message} Enter the station access token to continue.`;
  }
}

if (accessFragment) {
  exchangeAccessLink(accessFragment);
} else {
  refreshState().then(connectEvents).catch((error) => {
    if (!elements.loginView.hidden) elements.loginError.textContent = "Enter your station token to continue.";
    else showError(error.message);
  });
}
