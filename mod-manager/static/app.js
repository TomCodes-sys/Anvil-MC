// ---------------- Toasts ----------------
// Lightweight, dependency-free toast notifications. Used for errors/results
// from actions that don't have one obvious inline status line nearby (e.g.
// "Install" on a result card, "Update all"), so failures are never silent
// and never a blocking alert() popup.
function toast(message, type = "error", duration = 6000, action = null) {
  const stack = document.getElementById("toast-stack");
  if (!stack) { console.warn("toast:", message); return; }
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  const iconName = type === "error" ? "circle-alert" : type === "success" ? "circle-check" : "info";
  el.innerHTML = `<i data-lucide="${iconName}" class="toast-icon" style="width:16px;height:16px;"></i>
    <div class="toast-body"></div>
    ${action ? `<button class="toast-action"></button>` : ""}
    <button class="toast-close" aria-label="Dismiss"><i data-lucide="x" style="width:14px;height:14px;"></i></button>`;
  el.querySelector(".toast-body").textContent = message;
  const remove = () => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  };
  el.querySelector(".toast-close").addEventListener("click", remove);
  if (action) {
    const actionBtn = el.querySelector(".toast-action");
    actionBtn.textContent = action.label;
    actionBtn.addEventListener("click", () => { action.onClick(); remove(); });
  }
  stack.appendChild(el);
  icons();
  if (duration) setTimeout(remove, duration);
  return el;
}

const MOD_LOADERS = [
  { id: "fabric", label: "Fabric" },
  { id: "quilt", label: "Quilt" },
  { id: "forge", label: "Forge" },
  { id: "neoforge", label: "NeoForge" },
];
const PLUGIN_PLATFORMS = [
  { id: "paper", label: "Paper" },
  { id: "purpur", label: "Purpur" },
  { id: "spigot", label: "Spigot" },
  { id: "bukkit", label: "Bukkit" },
  { id: "folia", label: "Folia" },
];
const PAGE_SIZE = 20;

const state = {
  browsePath: null,
  selectedServer: null,     // full server data object
  searchType: "mod",        // mod | plugin | datapack
  searchSource: "modrinth",
  previewMode: false,
  selectedLoaders: new Set(),   // loader/platform filter checkboxes
  selectedVersions: new Set(),  // game version filter checkboxes
  allVersions: [],
  page: 1,
  totalResults: 0,
  lastCheckedResults: [],   // most recent /api/check_updates results, for changelog/update-all
  serverStatuses: {},       // server_path -> {linked, crafty_name, running}
  craftyServers: [],        // live list from /api/crafty/servers, for the link picker
  installBlocked: false,    // true when the active server is confirmed running in Crafty
};

function icons() { if (window.lucide) lucide.createIcons(); }

// ---------------- Preview mode ----------------
async function loadPreviewMode() {
  const res = await fetch("/api/settings/preview_mode");
  const data = await res.json();
  state.previewMode = data.preview_mode;
  document.getElementById("preview-toggle").checked = state.previewMode;
  await refreshCraftyNagBanner();
}
async function setPreviewMode(enabled) {
  const res = await fetch("/api/settings/preview_mode", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ enabled }),
  });
  const data = await res.json();
  state.previewMode = data.preview_mode;
  document.getElementById("preview-toggle").checked = state.previewMode;
  await refreshCraftyNagBanner();
}
document.getElementById("preview-toggle").addEventListener("change", (e) => {
  setPreviewMode(e.target.checked);
  document.getElementById("preview-status").textContent = e.target.checked
    ? "On — installs/updates/removals are simulated."
    : "Off — actions will write to disk normally.";
});
document.getElementById("btn-exit-preview").addEventListener("click", () => {
  setPreviewMode(false);
  document.getElementById("preview-toggle").checked = false;
});

// Preview Mode is a persisted setting (unlike a one-off "just let me look
// around" click), so without this, someone who tried "Preview Mode" once
// from the Crafty gate would sail past that gate on every future visit too
// — silently doing everything in simulation with no nudge to ever actually
// connect their real server. This keeps nagging (banner stays up, worded to
// say exactly why) for as long as preview mode is on AND no real Crafty URL
// + token are saved, and clears itself the moment they are.
async function refreshCraftyNagBanner() {
  const banner = document.getElementById("preview-banner");
  const text = document.getElementById("preview-banner-text");
  const gotoBtn = document.getElementById("btn-goto-crafty-from-banner");
  if (!state.previewMode) { banner.style.display = "none"; return; }

  let configured = false;
  try {
    const res = await fetch("/api/crafty/health");
    const data = await res.json();
    configured = !!data.configured;
  } catch (e) { /* treat as not configured */ }

  banner.style.display = "flex";
  if (configured) {
    text.innerHTML = "<strong>Preview mode</strong> — search is live, but installs, updates and removals are simulated. Nothing is written to or deleted from disk.";
    gotoBtn.style.display = "none";
  } else {
    text.innerHTML = "<strong>Preview mode</strong> — and no Crafty Controller is connected yet, so this dashboard can't manage a real server until you add one in Settings.";
    gotoBtn.style.display = "inline-flex";
  }
}
document.getElementById("btn-goto-crafty-from-banner")?.addEventListener("click", () => {
  document.querySelector('.tab[data-tab="settings"]').click();
  document.getElementById("crafty-token")?.focus();
});
loadPreviewMode();

// ---------------- Crafty API-key gate ----------------
// Blocks the whole app behind a "connect Crafty" screen until either a
// Crafty URL+token are saved, or the person picks Preview Mode instead.
// Skipped entirely when launched via the "Preview Demo" shortcut
// (?autopreview=1) — that shortcut's whole purpose is a no-setup demo.
function showCraftyGate() {
  document.getElementById("crafty-gate").classList.add("visible");
}
function unlockApp() {
  document.getElementById("crafty-gate").classList.remove("visible");
  document.querySelector(".scaffold").classList.remove("gate-pending");
  // moveTabIndicator() ran on page load while the scaffold was still
  // display:none (0 offsetWidth/offsetLeft), so recalculate it now that the
  // layout is actually visible.
  moveTabIndicator(document.querySelector(".tab.active"));
}
async function prefillCraftyGate() {
  try {
    const res = await fetch("/api/network/local_ip");
    const data = await res.json();
    if (data.ok) document.getElementById("gate-crafty-url").value = data.suggested_url;
  } catch (e) { /* Auto-detect button still works manually */ }
}
async function evaluateCraftyGate() {
  const params = new URLSearchParams(location.search);
  if (params.get("autopreview") === "1") {
    unlockApp();
    return;
  }
  try {
    const [previewRes, healthRes] = await Promise.all([
      fetch("/api/settings/preview_mode"),
      fetch("/api/crafty/health"),
    ]);
    const previewData = await previewRes.json();
    const healthData = await healthRes.json();
    if (previewData.preview_mode || healthData.configured) {
      unlockApp();
    } else {
      showCraftyGate();
      prefillCraftyGate();
    }
  } catch (e) {
    // Fail open to the gate, not to an unlocked app, if the status checks
    // themselves couldn't be reached.
    showCraftyGate();
  }
}
document.getElementById("gate-btn-detect-crafty-url")?.addEventListener("click", async () => {
  const btn = document.getElementById("gate-btn-detect-crafty-url");
  btn.disabled = true;
  btn.textContent = "Detecting…";
  try {
    const res = await fetch("/api/network/local_ip");
    const data = await res.json();
    if (data.ok) document.getElementById("gate-crafty-url").value = data.suggested_url;
  } finally {
    btn.disabled = false;
    btn.textContent = "Auto-detect";
  }
});
document.getElementById("gate-btn-save-crafty").addEventListener("click", async () => {
  const url = document.getElementById("gate-crafty-url").value.trim();
  const token = document.getElementById("gate-crafty-token").value.trim();
  const status = document.getElementById("gate-crafty-status");
  if (!url || !token) {
    status.textContent = "Enter both the Crafty URL and API token.";
    return;
  }
  status.textContent = "Checking…";
  await fetch("/api/settings/crafty", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ url, token }),
  });
  const healthRes = await fetch("/api/crafty/health");
  const healthData = await healthRes.json();
  // Also reflect the saved URL on the real Settings tab so it isn't blank
  // the first time the person looks there.
  const settingsUrlInput = document.getElementById("crafty-url");
  const settingsTokenInput = document.getElementById("crafty-token");
  if (settingsUrlInput) settingsUrlInput.value = url;
  if (settingsTokenInput) settingsTokenInput.placeholder = "(saved — leave blank to keep)";
  if (healthData.configured) {
    if (!healthData.reachable) {
      status.textContent = "Saved — but Crafty isn't reachable at that URL right now. Double-check it later in Settings.";
    }
    unlockApp();
  } else {
    status.textContent = "Something went wrong saving those — try again.";
  }
});
document.getElementById("gate-btn-preview").addEventListener("click", async () => {
  unlockApp();
  await loadDemoServer();
  document.querySelector('.tab[data-tab="browse"]').click();
});
evaluateCraftyGate();

async function loadDemoServer() {
  const res = await fetch("/api/demo/seed", { method: "POST" });
  const data = await res.json();
  applySelectedServer(data);
  state.previewMode = true;
  document.getElementById("preview-toggle").checked = true;
  document.getElementById("preview-banner").style.display = "flex";
  document.getElementById("detect-hint").textContent = "Demo server loaded — no real folder needed. Head to Browse & Install.";
  document.getElementById("setup-status").textContent = `Preview Server ready on Minecraft ${data.mc_version} (${data.loader}). Nothing will be written to disk.`;
  loadSavedServers();
  return data;
}
document.getElementById("btn-load-demo").addEventListener("click", loadDemoServer);

// ---------------- Tabs ----------------
function moveTabIndicator(tabEl) {
  const indicator = document.getElementById("tab-indicator");
  if (!indicator || !tabEl) return;
  indicator.style.width = tabEl.offsetWidth + "px";
  indicator.style.transform = `translateX(${tabEl.offsetLeft - 4}px)`;
}

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("panel-" + btn.dataset.tab).classList.add("active");
    moveTabIndicator(btn);
    if (btn.dataset.tab === "installed") refreshInstalled();
    if (btn.dataset.tab === "modpack") updateModpackServerLabel();
  });
});
window.addEventListener("load", () => moveTabIndicator(document.querySelector(".tab.active")));
window.addEventListener("resize", () => moveTabIndicator(document.querySelector(".tab.active")));

// ---------------- Directory browser ----------------
async function loadDir(path) {
  const res = await fetch("/api/browse" + (path ? `?path=${encodeURIComponent(path)}` : ""));
  const data = await res.json();
  if (data.error) { document.getElementById("dir-list").innerHTML = `<div class="dir-row">${data.error}</div>`; return; }
  state.browsePath = data.current;
  document.getElementById("path-input").value = data.current;
  const list = document.getElementById("dir-list");
  list.innerHTML = "";
  data.entries.forEach(e => {
    const row = document.createElement("div");
    row.className = "dir-row";
    row.textContent = e.name;
    row.addEventListener("click", () => loadDir(e.path));
    row.addEventListener("dblclick", () => selectServerFolder(e.path));
    list.appendChild(row);
  });
  document.getElementById("btn-select-server").disabled = false;
}
document.getElementById("btn-up").addEventListener("click", async () => {
  const res = await fetch(`/api/browse?path=${encodeURIComponent(state.browsePath)}`);
  const data = await res.json();
  loadDir(data.parent);
});
document.getElementById("btn-go").addEventListener("click", () => loadDir(document.getElementById("path-input").value));
document.getElementById("btn-select-server").addEventListener("click", () => selectServerFolder(state.browsePath));
loadDir(null);

async function selectServerFolder(path) {
  document.getElementById("path-input").value = path;
  state.browsePath = path;
  document.getElementById("server-name").value = path.split("/").filter(Boolean).pop();
  const res = await fetch(`/api/server/detect?path=${encodeURIComponent(path)}`);
  const info = await res.json();
  if (info.error) {
    document.getElementById("detect-hint").textContent = info.error;
    return;
  }
  document.getElementById("mc-version").value = info.mc_version || "";
  document.getElementById("loader-select").value = info.loader || "vanilla";
  document.getElementById("world-name").value = info.world_name || "world";
  document.getElementById("detect-hint").textContent = mcVersionHint(info.mc_version, info.mc_version_source, info.loader);
}

// Turns a detected mc_version + its source into an honest status line — the
// "guess" source (a version-shaped number pulled from a jar filename, used as
// a last resort for Forge/NeoForge setups that don't embed version.json) gets
// called out explicitly so it isn't trusted the same as a solid detection.
function mcVersionHint(mcVersion, source, loader) {
  if (!mcVersion) return "Couldn't auto-detect the version — please enter it manually.";
  if (source === "guess") {
    return `Best guess: Minecraft ${mcVersion} (read from a jar filename — Forge/NeoForge don't expose this as ` +
      `reliably as other loaders). Loader/platform: ${loader}. Please confirm this is correct before saving.`;
  }
  return `Detected Minecraft ${mcVersion}, loader/platform: ${loader}. Double-check before saving.`;
}

document.getElementById("btn-save-config").addEventListener("click", async () => {
  const server_path = document.getElementById("path-input").value.trim();
  if (!server_path) return;
  const body = {
    server_path,
    name: document.getElementById("server-name").value || server_path,
    mc_version: document.getElementById("mc-version").value.trim(),
    loader: document.getElementById("loader-select").value,
    world_name: document.getElementById("world-name").value.trim() || "world",
  };
  const res = await fetch("/api/server/config", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  const data = await res.json();
  applySelectedServer(data);
  document.getElementById("setup-status").textContent = `Saved. ${data.name} is now targeting Minecraft ${data.mc_version} (${data.loader}).`;
  loadSavedServers();
});

// Applies a server as the active one everywhere in the UI, persists it
// server-side so it's remembered next time the dashboard is opened, and
// refreshes the loader/platform filter checkboxes to match what that
// server actually supports.
function applySelectedServer(s) {
  state.selectedServer = s;
  document.getElementById("path-input").value = s.server_path;
  document.getElementById("server-name").value = s.name || "";
  document.getElementById("mc-version").value = s.mc_version || "";
  document.getElementById("loader-select").value = s.loader || "vanilla";
  document.getElementById("world-name").value = s.world_name || "world";
  document.getElementById("active-server-label").textContent = `${s.name} — MC ${s.mc_version} / ${s.loader}`;
  updateModpackServerLabel();
  // Auto-check this server's own MC version in the sidebar filter so search
  // results are safely narrowed by default — previously this had to be
  // ticked by hand every time, and forgetting to do so was the easiest way
  // to install a mod built for the wrong Minecraft version.
  state.selectedVersions.clear();
  if (s.mc_version) state.selectedVersions.add(s.mc_version);
  if (state.allVersions) {
    const q = document.getElementById("version-search-input").value.trim().toLowerCase();
    renderVersionChecks(q ? state.allVersions.filter(v => v.toLowerCase().includes(q)) : state.allVersions);
  }
  fetch("/api/settings/active_server", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ server_path: s.server_path }),
  });
  // Auto-pick a sensible content type for this server: Bukkit-family
  // platforms browse plugins by default, everything else browses mods.
  const isPlugin = PLUGIN_PLATFORMS.some(p => p.id === s.loader);
  setSearchType(isPlugin ? "plugin" : "mod");
  updateDownloadingToBanner();
  populateCraftyLinkSelect();
}

function updateModpackServerLabel() {
  const el = document.getElementById("modpack-server-label");
  if (!el) return;
  el.textContent = state.selectedServer ? (state.selectedServer.name || state.selectedServer.server_path) : "no server selected";
}

// ---------------- Crafty status: badges, "downloading to" banner, gating ----------------

function statusDotClass(status) {
  if (!status || status.running === null || status.running === undefined) return "unlinked";
  return status.running ? "running" : "stopped";
}

function statusLabel(status) {
  if (!status || !status.linked) return "not linked to Crafty";
  if (status.running === true) return `running (${status.crafty_name || "Crafty"})`;
  if (status.running === false) return `stopped (${status.crafty_name || "Crafty"})`;
  return `linked to ${status.crafty_name || "Crafty"} — status unknown`;
}

async function refreshServerStatuses() {
  try {
    const res = await fetch("/api/servers/status");
    state.serverStatuses = await res.json();
  } catch (e) { /* ignore — badges just stay neutral */ }
}

function updateDownloadingToBanner() {
  const banner = document.getElementById("downloading-to-banner");
  const nameEl = document.getElementById("downloading-to-name");
  const statusEl = document.getElementById("downloading-to-status");
  if (!banner) return;
  if (!state.selectedServer) {
    nameEl.textContent = "no server selected";
    statusEl.textContent = "";
    banner.classList.remove("blocked");
    state.installBlocked = false;
    return;
  }
  const status = state.serverStatuses[state.selectedServer.server_path];
  nameEl.textContent = state.selectedServer.name || state.selectedServer.server_path;
  const running = status && status.running === true;
  state.installBlocked = !!running;
  statusEl.textContent = status ? `· ${statusLabel(status)}` : "";
  banner.classList.toggle("blocked", running);
  banner.querySelector("span:not(#downloading-to-status)").innerHTML =
    running
      ? `⛔ Won't download here — <strong id="downloading-to-name">${nameEl.textContent}</strong> is running`
      : `Downloading to: <strong id="downloading-to-name">${nameEl.textContent}</strong>`;
}

async function loadSavedServers() {
  const localRes = await fetch("/api/servers");
  const localServers = await localRes.json();
  await refreshServerStatuses();

  const hint = document.getElementById("crafty-pick-hint");
  const notConfigured = document.getElementById("crafty-pick-not-configured");
  const configured = document.getElementById("crafty-pick-configured");
  const grid = document.getElementById("crafty-pick-buttons");
  const status = document.getElementById("crafty-pick-status");

  // Source the list from Crafty directly so every server Crafty knows about
  // shows up here — not just ones that were previously walked through the
  // manual detect/save flow. Locally-tracked servers are merged in for their
  // version/loader info; anything Crafty knows about that hasn't been
  // imported yet still shows up, just dashed with an "import" affordance
  // instead of being missing entirely.
  let craftyList = null;
  let craftyError = null;
  try {
    const res = await fetch("/api/crafty/servers");
    const data = await res.json();
    if (data.ok) { craftyList = data.servers; state.craftyServers = data.servers; }
    else craftyError = data.error;
  } catch (e) { craftyError = "Couldn't reach Crafty."; }

  if (!craftyList) {
    notConfigured.style.display = "block";
    configured.style.display = "none";
    hint.style.display = "block";
    hint.textContent = craftyError || "Crafty isn't connected yet.";
    return;
  }
  notConfigured.style.display = "none";
  configured.style.display = "block";
  hint.style.display = "none";

  const byPath = {};
  localServers.forEach(s => { byPath[s.server_path] = s; });

  const entries = craftyList.map(cs => {
    const local = cs.existing_path ? byPath[cs.existing_path] : null;
    return {
      crafty: cs,
      local,
      server_path: local ? local.server_path : null,
      name: (local && local.name) || cs.name,
      mc_version: local ? local.mc_version : "",
      loader: local ? local.loader : "",
      imported: !!local,
    };
  });

  grid.innerHTML = "";
  if (!entries.length) {
    status.textContent = "Crafty didn't report any servers yet — create one in Crafty first.";
  } else {
    status.textContent = "";
  }

  entries.forEach(e => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "server-pick-btn"
      + (state.selectedServer && e.server_path && state.selectedServer.server_path === e.server_path ? " active" : "")
      + (e.imported ? "" : " server-pick-btn-unimported");
    const nameRow = document.createElement("span");
    nameRow.className = "server-pick-btn-name";
    const statusInfo = e.imported ? state.serverStatuses[e.server_path] : (e.crafty ? { linked: true, running: e.crafty.running } : null);
    const dot = document.createElement("span");
    dot.className = "status-dot " + statusDotClass(statusInfo);
    dot.title = statusInfo ? statusLabel(statusInfo) : "not imported yet";
    nameRow.appendChild(dot);
    nameRow.appendChild(document.createTextNode(e.name || e.server_path || "unnamed"));
    const meta = document.createElement("span");
    meta.className = "server-pick-btn-meta";
    meta.textContent = e.imported
      ? (e.mc_version ? `MC ${e.mc_version} · ${e.loader || "vanilla"}` : "no version set")
      : "click to import from Crafty";
    btn.appendChild(nameRow);
    btn.appendChild(meta);
    btn.addEventListener("click", async () => {
      if (e.imported) {
        applySelectedServer(e.local);
        loadSavedServers();
      } else if (e.crafty) {
        btn.classList.add("loading");
        try {
          const res = await fetch("/api/crafty/select_server", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ crafty_server_id: e.crafty.id, crafty_server_name: e.crafty.name }),
          });
          const data = await res.json();
          if (data.ok) {
            applySelectedServer(data.server);
            status.textContent = data.server.mc_version
              ? mcVersionHint(data.server.mc_version, data.server.mc_version_source, data.server.loader)
              : `Ready — ${data.server.name}, but the version couldn't be auto-detected. Enter it manually below.`;
            if (data.server.mc_version_source === "guess") {
              toast(`Minecraft version for ${data.server.name} is a best guess (${data.server.mc_version}) — please confirm it in Server Setup before installing mods.`, "info", 9000);
            }
            if (data.version_changed) {
              toast(`Detected Minecraft ${data.previous_mc_version} → ${data.server.mc_version} for ${data.server.name} — someone updated the server jar outside this app. Want to check every mod for updates against the new version?`,
                "info", 12000, { label: "Check updates", onClick: () => { document.querySelector('.tab[data-tab="installed"]')?.click(); runCheckUpdates(); } });
            }
          } else {
            toast(data.error || "Couldn't import this server.");
          }
        } finally {
          loadSavedServers();
        }
      }
    });
    grid.appendChild(btn);
  });
  updateDownloadingToBanner();
}

// ---------------- Crafty server linking ----------------

async function populateCraftyLinkSelect() {
  const select = document.getElementById("crafty-link-select");
  const hint = document.getElementById("crafty-link-hint");
  if (!select || !state.selectedServer) return;

  if (!state.craftyServers.length) {
    try {
      const res = await fetch("/api/crafty/servers");
      const data = await res.json();
      if (data.ok) state.craftyServers = data.servers;
      else hint.textContent = data.error || "";
    } catch (e) { /* ignore */ }
  }

  select.innerHTML = '<option value="">— not linked —</option>';
  state.craftyServers.forEach(cs => {
    const opt = document.createElement("option");
    opt.value = cs.id;
    opt.textContent = cs.name + (cs.running === true ? " (running)" : cs.running === false ? " (stopped)" : "");
    select.appendChild(opt);
  });
  select.value = state.selectedServer.crafty_server_id || "";
}

document.getElementById("crafty-link-select")?.addEventListener("change", async (e) => {
  if (!state.selectedServer) return;
  const id = e.target.value;
  const match = state.craftyServers.find(cs => cs.id === id);
  await fetch("/api/server/link_crafty", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      server_path: state.selectedServer.server_path,
      crafty_server_id: id,
      crafty_server_name: match ? match.name : "",
    }),
  });
  document.getElementById("crafty-link-hint").textContent = id ? "Linked." : "Unlinked.";
  await loadSavedServers();
});

document.getElementById("btn-auto-detect-crafty")?.addEventListener("click", async () => {
  const btn = document.getElementById("btn-auto-detect-crafty");
  const hint = document.getElementById("crafty-link-hint");
  btn.disabled = true;
  btn.textContent = "Detecting…";
  try {
    const res = await fetch("/api/crafty/auto_match", { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      hint.textContent = data.error || "Couldn't reach Crafty.";
    } else {
      state.craftyServers = data.crafty_servers || [];
      hint.textContent = data.matched.length
        ? `Matched ${data.matched.length} server${data.matched.length === 1 ? "" : "s"} by name.`
        : "No new exact name matches found — link manually above if needed.";
      await loadSavedServers();
      populateCraftyLinkSelect();
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Auto-detect all";
  }
});

// On first load, auto-select whichever server was last active — so if
// you're managing several Crafty servers, the dashboard picks up right
// where you left off instead of making you re-select every visit.
async function restoreActiveServer() {
  const res = await fetch("/api/settings/active_server");
  const data = await res.json();
  if (data.server && data.server.mc_version) {
    applySelectedServer(data.server);
  }
  loadSavedServers();
}
restoreActiveServer();

document.getElementById("btn-goto-crafty-settings")?.addEventListener("click", () => {
  document.querySelector('.tab[data-tab="settings"]').click();
});

// ---------------- Browse & install: content type / source toggles ----------------
function setSearchType(type) {
  state.searchType = type;
  state.page = 1;
  document.querySelectorAll("#type-toggle .seg").forEach(x => x.classList.toggle("active", x.dataset.type === type));
  renderLoaderFilterGroup();
  runSearch();
}
document.querySelectorAll("#type-toggle .seg").forEach(b => b.addEventListener("click", () => setSearchType(b.dataset.type)));

document.querySelectorAll("#source-toggle .seg").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll("#source-toggle .seg").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  state.searchSource = b.dataset.source;
  state.page = 1;
  runSearch();
}));

// ---------------- Loader / platform filter (sidebar) ----------------
function renderLoaderFilterGroup() {
  const group = document.getElementById("loader-filter-group");
  const title = document.getElementById("loader-filter-title");
  const container = document.getElementById("loader-checks");
  state.selectedLoaders.clear();

  if (state.searchType === "datapack") {
    group.style.display = "none";
    return;
  }
  group.style.display = "";
  const list = state.searchType === "plugin" ? PLUGIN_PLATFORMS : MOD_LOADERS;
  title.textContent = state.searchType === "plugin" ? "Platform" : "Loader";
  container.innerHTML = "";
  list.forEach(({ id, label }) => {
    const row = document.createElement("label");
    row.className = "filter-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = id;
    // Pre-check the active server's own loader/platform so results default
    // to what will actually work on it.
    if (state.selectedServer && state.selectedServer.loader === id) {
      cb.checked = true;
      state.selectedLoaders.add(id);
      row.classList.add("active-label");
    }
    cb.addEventListener("change", () => {
      if (cb.checked) state.selectedLoaders.add(id); else state.selectedLoaders.delete(id);
      row.classList.toggle("active-label", cb.checked);
      state.page = 1;
      runSearch();
    });
    const span = document.createElement("span");
    span.textContent = label;
    row.appendChild(cb);
    row.appendChild(span);
    container.appendChild(row);
  });
}

// ---------------- Game version filter (sidebar) ----------------
async function loadGameVersions() {
  const res = await fetch("/api/game_versions");
  const data = await res.json();
  state.allVersions = Array.isArray(data) ? data : [];
  if (!Array.isArray(data) && data.error) {
    document.getElementById("search-status").textContent = data.error;
    document.getElementById("search-status").classList.add("error");
  }
  renderVersionChecks(state.allVersions);
}
function renderVersionChecks(versions) {
  const container = document.getElementById("version-checks");
  container.innerHTML = "";
  versions.slice(0, 60).forEach(v => {
    const row = document.createElement("label");
    row.className = "filter-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = v;
    cb.checked = state.selectedVersions.has(v);
    if (cb.checked) row.classList.add("active-label");
    cb.addEventListener("change", () => {
      if (cb.checked) state.selectedVersions.add(v); else state.selectedVersions.delete(v);
      row.classList.toggle("active-label", cb.checked);
      state.page = 1;
      runSearch();
    });
    const span = document.createElement("span");
    span.textContent = v;
    row.appendChild(cb);
    row.appendChild(span);
    container.appendChild(row);
  });
}
document.getElementById("version-search-input").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  renderVersionChecks(q ? state.allVersions.filter(v => v.toLowerCase().includes(q)) : state.allVersions);
});
document.getElementById("version-filter-toggle").addEventListener("click", () => {
  document.getElementById("version-filter-toggle").closest(".filter-group").classList.toggle("collapsed");
});
loadGameVersions();

// ---------------- Search ----------------
document.getElementById("btn-search").addEventListener("click", () => { state.page = 1; runSearch(); });
document.getElementById("search-input").addEventListener("keydown", e => { if (e.key === "Enter") { state.page = 1; runSearch(); } });

// Live search as you type, Modrinth-style — debounced so it doesn't fire an
// API call on every keystroke.
let searchDebounce = null;
document.getElementById("search-input").addEventListener("input", () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => { state.page = 1; runSearch(); }, 400);
});

function showResultSkeletons(count = 6) {
  const grid = document.getElementById("results-grid");
  grid.innerHTML = "";
  for (let i = 0; i < count; i++) {
    const card = document.createElement("div");
    card.className = "skeleton-card";
    card.innerHTML = `
      <div class="skeleton-block skeleton-icon"></div>
      <div class="skeleton-lines">
        <div class="skeleton-block"></div>
        <div class="skeleton-block"></div>
      </div>`;
    grid.appendChild(card);
  }
}

function addTag(container, text, extraClass, icon) {
  const tpl = document.getElementById("tpl-tag");
  const node = tpl.content.cloneNode(true);
  const chip = node.querySelector(".tag-chip");
  if (extraClass) chip.classList.add(extraClass);
  node.querySelector(".tag-icon").textContent = icon || "";
  node.querySelector(".tag-label").textContent = text;
  container.appendChild(node);
}

function renderResultTags(container, r) {
  container.innerHTML = "";
  // Every result on screen was already filtered to this version server-side —
  // shown here too so it's obvious at a glance without hovering anything.
  if (state.lastSearchedVersion) addTag(container, `MC ${state.lastSearchedVersion}`, "tag-version");
  // Environment: client / server / both — the same info Modrinth shows as
  // "Client or server", "Client", or "Server" pills.
  if (r.client_side && r.server_side) {
    const clientOn = r.client_side !== "unsupported";
    const serverOn = r.server_side !== "unsupported";
    let envText = "Client or server";
    if (clientOn && !serverOn) envText = "Client";
    else if (!clientOn && serverOn) envText = "Server";
    addTag(container, envText, "tag-env", "\u{1F5A5}");
  }
  // Loader / platform tags actually reported by Modrinth for this project.
  (r.loaders || []).forEach(l => addTag(container, l.charAt(0).toUpperCase() + l.slice(1), "tag-" + l));
  // Everything else Modrinth categorizes it under (Library, Optimization, ...).
  (r.categories || []).slice(0, 3).forEach(c => addTag(container, c.charAt(0).toUpperCase() + c.slice(1)));
}

async function runSearch() {
  const statusEl = document.getElementById("search-status");
  const grid = document.getElementById("results-grid");
  const pagination = document.getElementById("pagination");
  if (!state.selectedServer || !state.selectedServer.mc_version) {
    statusEl.textContent = "Select and save a server profile in Server Setup first.";
    statusEl.classList.add("error");
    grid.innerHTML = "";
    pagination.style.display = "none";
    return;
  }
  statusEl.classList.remove("error");
  statusEl.textContent = "Searching...";
  showResultSkeletons();
  const query = document.getElementById("search-input").value;
  // Selected version checkboxes narrow the search; with none checked, fall
  // back to the server's own configured version (the common case).
  const mcVersion = state.selectedVersions.size ? [...state.selectedVersions][0] : state.selectedServer.mc_version;
  const loaders = state.selectedLoaders.size ? [...state.selectedLoaders] : (state.selectedServer.loader && state.selectedServer.loader !== "vanilla" ? [state.selectedServer.loader] : []);
  state.lastSearchedVersion = mcVersion;
  const offset = (state.page - 1) * PAGE_SIZE;
  const params = new URLSearchParams({
    source: state.searchSource,
    query,
    type: state.searchType,
    mc_version: mcVersion,
    loaders: loaders.join(","),
    offset: String(offset),
  });
  const res = await fetch(`/api/search?${params}`);
  const data = await res.json();
  if (data.error) { grid.innerHTML = ""; statusEl.textContent = data.error; statusEl.classList.add("error"); pagination.style.display = "none"; return; }
  if (data.note) { grid.innerHTML = ""; statusEl.textContent = data.note; pagination.style.display = "none"; return; }
  state.totalResults = data.total || 0;
  const totalPages = Math.max(1, Math.ceil(state.totalResults / PAGE_SIZE));
  statusEl.textContent = `${data.total} result${data.total === 1 ? "" : "s"} for Minecraft ${mcVersion}` +
    (loaders.length ? ` / ${loaders.join(", ")}` : "") +
    (totalPages > 1 ? ` — page ${state.page} of ${totalPages}` : "");
  grid.innerHTML = "";
  const tpl = document.getElementById("tpl-result-card");
  data.results.forEach((r, i) => {
    const node = tpl.content.cloneNode(true);
    const card = node.querySelector(".result-card");
    card.style.setProperty("--stagger", `${Math.min(i, 10) * 35}ms`);
    const icon = node.querySelector(".result-icon");
    icon.src = r.icon_url || "";
    icon.alt = r.title;
    node.querySelector(".result-title").textContent = r.title;
    node.querySelector(".result-desc").textContent = r.description || "";
    node.querySelector(".result-author").textContent = r.author || "";
    node.querySelector(".result-downloads").textContent = r.downloads != null ? `${r.downloads.toLocaleString()} downloads` : "";
    renderResultTags(node.querySelector(".result-tags"), r);
    const btn = node.querySelector(".btn-install");
    btn.addEventListener("click", () => installItem(r, btn));
    grid.appendChild(node);
  });
  renderPagination(totalPages);
  icons();
}

function renderPagination(totalPages) {
  const pagination = document.getElementById("pagination");
  if (totalPages <= 1) { pagination.style.display = "none"; return; }
  pagination.style.display = "flex";
  document.getElementById("page-prev").disabled = state.page <= 1;
  document.getElementById("page-next").disabled = state.page >= totalPages;

  const numbers = document.getElementById("page-numbers");
  numbers.innerHTML = "";
  // Show up to 7 page buttons centered on the current page, Modrinth-style.
  let start = Math.max(1, state.page - 3);
  let end = Math.min(totalPages, start + 6);
  start = Math.max(1, end - 6);
  for (let p = start; p <= end; p++) {
    const btn = document.createElement("button");
    btn.className = "page-num" + (p === state.page ? " active" : "");
    btn.textContent = String(p);
    btn.addEventListener("click", () => { state.page = p; runSearch(); document.querySelector(".browse-main").scrollIntoView({ behavior: "smooth", block: "start" }); });
    numbers.appendChild(btn);
  }
}
document.getElementById("page-prev").addEventListener("click", () => { if (state.page > 1) { state.page--; runSearch(); } });
document.getElementById("page-next").addEventListener("click", () => { state.page++; runSearch(); });

async function installItem(r, btn) {
  if (state.installBlocked) {
    btn.textContent = "Server is running";
    btn.title = "Stop the server in Crafty before installing/updating mods.";
    setTimeout(() => { btn.textContent = "Install"; }, 2200);
    return;
  }
  btn.disabled = true;
  btn.textContent = "Installing...";
  const res = await fetch("/api/install", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      server_path: state.selectedServer.server_path,
      source: r.source, project_id: r.project_id, title: r.title, type: state.searchType,
      icon_url: r.icon_url,
    }),
  });
  const data = await res.json();
  if (data.error) {
    btn.textContent = "Failed";
    btn.title = data.error;
    toast(data.error);
    setTimeout(() => { btn.disabled = false; btn.textContent = "Install"; }, 2200);
    return;
  }
  btn.textContent = data.preview ? "Simulated \u2713" : "Installed";
  if (data.suggested_dependencies && data.suggested_dependencies.length) {
    promptInstallDependencies(r.title, data.suggested_dependencies);
  }
}

// Asks before installing anything extra — never silent. Loops the same
// /api/install call the person would've made by hand for each required
// dependency that isn't already on the server.
function promptInstallDependencies(parentTitle, deps) {
  const names = deps.map(d => d.title).join(", ");
  const label = deps.length === 1 ? "Install required dependency" : `Install ${deps.length} required dependencies`;
  toast(`${parentTitle} needs ${names} to work — install ${deps.length === 1 ? "it" : "them"} too?`, "info", 15000, {
    label,
    onClick: async () => {
      const t = toast(`Installing ${names}…`, "info", 0);
      const failed = [];
      for (const dep of deps) {
        const res = await fetch("/api/install", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            server_path: state.selectedServer.server_path,
            source: dep.source, project_id: dep.project_id, title: dep.title, type: "mod",
          }),
        });
        const data = await res.json();
        if (data.error) failed.push(dep.title);
        else if (data.suggested_dependencies && data.suggested_dependencies.length) {
          // Dependencies can have their own dependencies — chain the prompt.
          promptInstallDependencies(dep.title, data.suggested_dependencies);
        }
      }
      if (t) t.remove();
      toast(failed.length ? `Installed ${deps.length - failed.length}/${deps.length} — couldn't install: ${failed.join(", ")}`
                           : `Installed ${names}.`, failed.length ? "error" : "success");
      if (document.getElementById("panel-installed").classList.contains("active")) refreshInstalled();
    },
  });
}

// ---------------- Installed / updates ----------------
// preserveStatuses=true (the default) re-applies whatever /api/check_updates
// last told us about each item, so refreshing the list after updating (or
// reverting) ONE item doesn't blank out every other item's "update
// available" pill and Update button — that was the bug where clicking one
// item's Update button made every other item's button vanish: this used to
// always render with no statuses at all, which is indistinguishable from
// "nothing has an update".
async function refreshInstalled(preserveStatuses = true) {
  if (!state.selectedServer) return;
  document.getElementById("installed-server-label").textContent = state.selectedServer.name || state.selectedServer.server_path;
  document.getElementById("installed-mc-version").textContent = state.selectedServer.mc_version || "unset";
  document.getElementById("installed-loader").textContent = state.selectedServer.loader || "unset";
  const res = await fetch(`/api/installed?server_path=${encodeURIComponent(state.selectedServer.server_path)}`);
  const data = await res.json();
  const statuses = (preserveStatuses && state.lastCheckedResults && state.lastCheckedResults.length)
    ? state.lastCheckedResults : null;
  renderInstalledList("installed-mods", data.mods, statuses);
  renderInstalledList("installed-plugins", data.plugins || [], statuses);
  renderInstalledList("installed-datapacks", data.datapacks, statuses);
  renderUntrackedList(data.untracked_mods || [], data.untracked_plugins || []);
  if (statuses) {
    // Recompute from the preserved statuses, filtered to what's still
    // actually installed, rather than blindly hiding the button.
    const installedKeys = new Set([...data.mods, ...(data.plugins || []), ...data.datapacks]
      .map(i => `${i.source}:${i.project_id}`));
    const remainingUpdates = statuses.filter(r => r.status === "update_available" && installedKeys.has(`${r.source}:${r.project_id}`)).length;
    document.getElementById("btn-update-all").style.display = remainingUpdates > 0 ? "inline-flex" : "none";
  } else {
    document.getElementById("btn-update-all").style.display = "none";
  }
  icons();
}

function renderUntrackedList(untrackedMods, untrackedPlugins) {
  const section = document.getElementById("untracked-section");
  const list = document.getElementById("untracked-list");
  const combined = [
    ...untrackedMods.map(u => ({ ...u, type: "mod" })),
    ...untrackedPlugins.map(u => ({ ...u, type: "plugin" })),
  ];
  if (!combined.length) { section.style.display = "none"; list.innerHTML = ""; return; }
  section.style.display = "block";
  list.innerHTML = "";
  combined.forEach(u => {
    const row = document.createElement("div");
    row.className = "installed-row";
    const name = document.createElement("span");
    name.className = "installed-row-name";
    name.textContent = u.identified ? `${u.identified.title} (${u.file_name})` : u.file_name;
    const meta = document.createElement("span");
    meta.className = "installed-row-meta";
    meta.textContent = u.identified ? `Matched on Modrinth — ${u.identified.version_number || u.identified.version_id}` : "Not recognized — will be added without update tracking";
    const btn = document.createElement("button");
    btn.className = "btn-secondary btn-small";
    btn.textContent = "Add to tracking";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Adding…";
      const res = await fetch("/api/adopt", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          server_path: state.selectedServer.server_path, type: u.type,
          file_name: u.file_name, identified: u.identified,
        }),
      });
      const data = await res.json();
      if (data.ok) { toast(`Added ${data.adopted.title} to tracking.`, "success"); refreshInstalled(); }
      else { toast(data.error || "Couldn't add that file.", "error"); btn.disabled = false; btn.textContent = "Add to tracking"; }
    });
    row.appendChild(name);
    row.appendChild(meta);
    row.appendChild(btn);
    list.appendChild(row);
  });
  icons();
}

function renderInstalledList(elId, items, statuses) {
  const el = document.getElementById(elId);
  el.innerHTML = "";
  if (!items.length) { el.innerHTML = `<p class="hint">Nothing installed yet.</p>`; return; }
  const tpl = document.getElementById("tpl-installed-row");
  items.forEach(item => {
    const node = tpl.content.cloneNode(true);
    const icon = node.querySelector(".installed-icon");
    icon.src = item.icon_url || "";
    icon.alt = item.title;
    icon.style.visibility = item.icon_url ? "visible" : "hidden";
    node.querySelector(".installed-title").textContent = item.title;
    node.querySelector(".installed-version").textContent = item.version_number || item.file_name;
    node.querySelector(".source-badge").textContent = item.source;
    if (item.preview) node.querySelector(".preview-badge").style.display = "inline-block";
    if (item.modpack) {
      const mb = node.querySelector(".modpack-badge");
      mb.style.display = "inline-block";
      mb.textContent = item.modpack;
      mb.title = `Imported from modpack: ${item.modpack}`;
    }
    const status = statuses ? statuses.find(s => s.project_id === item.project_id && s.source === item.source) : null;
    const pill = node.querySelector(".status-pill");
    const msg = node.querySelector(".status-message");
    const updateBtn = node.querySelector(".btn-update");
    const changelogBtn = node.querySelector(".btn-changelog");
    const conflictBadge = node.querySelector(".conflict-badge");
    if (status) {
      pill.classList.add(status.status);
      msg.textContent = status.message;
      if (status.conflicts && status.conflicts.length) {
        conflictBadge.style.display = "inline-flex";
        conflictBadge.title = `Known to conflict with: ${status.conflicts.join(", ")}`;
      }
      if (status.status === "update_available") {
        updateBtn.style.display = "inline-block";
        updateBtn.addEventListener("click", () => applyUpdate(item, updateBtn));
        changelogBtn.style.display = "inline-block";
        changelogBtn.addEventListener("click", () => openChangelog(item, status.latest, updateBtn));
      }
    } else {
      pill.classList.add("unknown");
      msg.textContent = `Targeting Minecraft ${item.mc_version}`;
    }
    if (item.last_backup) {
      const revertBtn = node.querySelector(".btn-revert");
      revertBtn.style.display = "inline-block";
      revertBtn.addEventListener("click", () => revertItem(item, revertBtn));
    }
    node.querySelector(".btn-uninstall").addEventListener("click", async (e) => {
      await fetch("/api/uninstall", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ server_path: state.selectedServer.server_path, source: item.source, project_id: item.project_id, type: item.type }),
      });
      refreshInstalled();
    });
    el.appendChild(node);
  });
  icons();
}

async function runCheckUpdates(only) {
  const statusEl = document.getElementById("update-status");
  const retryBtn = document.getElementById("btn-retry-failed");
  if (!only) statusEl.textContent = "Checking every installed mod, plugin and datapack against the current server version...";
  const res = await fetch("/api/check_updates", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(only
      ? { server_path: state.selectedServer.server_path, only }
      : { server_path: state.selectedServer.server_path }),
  });
  const data = await res.json();

  if (only) {
    // Partial re-check (a retry): merge the re-checked entries back into the
    // last full result set rather than replacing it, so items that weren't
    // part of this retry keep showing their previous status.
    const merged = new Map(state.lastCheckedResults.map(r => [`${r.source}:${r.project_id}`, r]));
    data.results.forEach(r => merged.set(`${r.source}:${r.project_id}`, r));
    state.lastCheckedResults = [...merged.values()];
  } else {
    state.lastCheckedResults = data.results;
  }

  const allResults = state.lastCheckedResults;
  const mods = allResults.filter(r => r.type === "mod");
  const plugins = allResults.filter(r => r.type === "plugin");
  const datapacks = allResults.filter(r => r.type === "datapack");
  renderInstalledList("installed-mods", mods, allResults);
  renderInstalledList("installed-plugins", plugins, allResults);
  renderInstalledList("installed-datapacks", datapacks, allResults);

  const updates = allResults.filter(r => r.status === "update_available").length;
  const stuck = allResults.filter(r => r.status === "no_compatible_version").length;
  const failed = allResults.filter(r => r.status === "check_failed");
  const conflicts = allResults.filter(r => r.conflicts && r.conflicts.length).length;
  statusEl.textContent = `${updates} update${updates === 1 ? "" : "s"} available. ` +
    (stuck ? `${stuck} item${stuck === 1 ? "" : "s"} have no build for Minecraft ${data.mc_version || state.selectedServer.mc_version} yet. ` : "") +
    (failed.length ? `${failed.length} item${failed.length === 1 ? "" : "s"} couldn't be checked. ` : "") +
    (conflicts ? `${conflicts} known conflict${conflicts === 1 ? "" : "s"} flagged.` : (stuck || failed.length ? "" : "Everything else has a matching build."));
  document.getElementById("btn-update-all").style.display = updates > 0 ? "inline-flex" : "none";

  state.lastFailedItems = failed.map(f => ({ source: f.source, project_id: f.project_id }));
  retryBtn.style.display = failed.length ? "inline-flex" : "none";
  retryBtn.innerHTML = `<i data-lucide="rotate-ccw"></i>Retry failed items (${failed.length})`;
  icons();

  if (only) {
    const stillFailing = data.results.filter(r => r.status === "check_failed").length;
    if (stillFailing === 0) toast(`Rechecked ${data.results.length} item${data.results.length === 1 ? "" : "s"} — all clear now.`, "success");
    else toast(`Rechecked ${data.results.length} item${data.results.length === 1 ? "" : "s"} — ${stillFailing} still couldn't be reached.`);
  } else if (failed.length) {
    toast(`${failed.length} item${failed.length === 1 ? "" : "s"} couldn't be checked (network issue) — click "Retry failed items" to try again.`);
  }
}

document.getElementById("btn-check-updates").addEventListener("click", () => runCheckUpdates());
document.getElementById("btn-retry-failed").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (!state.lastFailedItems || !state.lastFailedItems.length) return;
  btn.disabled = true;
  const label = btn.innerHTML;
  btn.innerHTML = `<i data-lucide="loader-2" class="spin"></i>Retrying...`;
  icons();
  await runCheckUpdates(state.lastFailedItems);
  btn.disabled = false;
});

document.getElementById("btn-update-all").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const statusEl = document.getElementById("update-status");
  if (state.installBlocked) {
    statusEl.textContent = "Server is running in Crafty — stop it first, then update.";
    return;
  }
  btn.disabled = true;
  btn.textContent = "Updating all...";
  const res = await fetch("/api/update_all", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ server_path: state.selectedServer.server_path }),
  });
  const data = await res.json();
  if (data.error) {
    statusEl.textContent = data.error;
    btn.disabled = false;
    btn.innerHTML = '<i data-lucide="download"></i>Update all';
    icons();
    return;
  }
  statusEl.textContent = `Updated ${data.updated.length} item${data.updated.length === 1 ? "" : "s"}.` +
    (data.failed.length ? ` ${data.failed.length} failed.` : "");
  btn.disabled = false;
  btn.innerHTML = '<i data-lucide="download"></i>Update all';
  await refreshInstalled();
  document.getElementById("btn-check-updates").click();
});

document.getElementById("btn-restart-server").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  const original = btn.innerHTML;
  btn.textContent = "Restarting...";
  try {
    const res = await fetch("/api/crafty/restart", { method: "POST" });
    const data = await res.json();
    document.getElementById("update-status").textContent = data.ok
      ? (data.preview ? "Restart simulated (preview mode)." : "Restart triggered through Crafty.")
      : (data.error || "Restart failed.");
  } catch (e2) {
    document.getElementById("update-status").textContent = "Couldn't reach the dashboard to trigger a restart.";
  }
  btn.disabled = false;
  btn.innerHTML = original;
  icons();
});

async function applyUpdate(item, btn) {
  if (state.installBlocked) {
    btn.textContent = "Server running";
    btn.title = "Stop the server in Crafty before updating.";
    setTimeout(() => { btn.textContent = "Update"; }, 2200);
    return;
  }
  btn.disabled = true;
  btn.textContent = "Updating...";
  const res = await fetch("/api/update_mod", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ server_path: state.selectedServer.server_path, source: item.source, project_id: item.project_id, type: item.type }),
  });
  const data = await res.json();
  if (data.error) {
    btn.disabled = false;
    btn.textContent = "Failed";
    btn.title = data.error;
    setTimeout(() => { btn.textContent = "Update"; }, 2200);
    return;
  }
  await refreshInstalled();
  // Re-check just this one item so its own pill/button reflects reality
  // (it's no longer "update available") without re-hitting Modrinth/
  // CurseForge for everything else that hasn't changed.
  await runCheckUpdates([{ source: item.source, project_id: item.project_id }]);
}

async function revertItem(item, btn) {
  btn.disabled = true;
  btn.textContent = "Reverting...";
  const res = await fetch("/api/revert", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ server_path: state.selectedServer.server_path, source: item.source, project_id: item.project_id, type: item.type }),
  });
  const data = await res.json();
  if (data.error) { btn.textContent = "Failed"; btn.title = data.error; return; }
  await refreshInstalled();
  await runCheckUpdates([{ source: item.source, project_id: item.project_id }]);
}

// ---------------- Changelog preview modal ----------------
let pendingChangelogUpdate = null;
function openChangelog(item, latest, updateBtn) {
  const modal = document.getElementById("changelog-modal");
  document.getElementById("changelog-title").textContent = `${item.title} — changelog`;
  document.getElementById("changelog-body").textContent = "Loading...";
  modal.style.display = "flex";
  pendingChangelogUpdate = { item, updateBtn };
  const params = new URLSearchParams({ source: item.source, project_id: item.project_id, version_id: latest.version_id });
  fetch(`/api/changelog?${params}`).then(r => r.json()).then(data => {
    document.getElementById("changelog-body").textContent = data.changelog || "No changelog was provided for this version.";
  }).catch(() => {
    document.getElementById("changelog-body").textContent = "Couldn't load the changelog.";
  });
}
function closeChangelogModal() {
  document.getElementById("changelog-modal").style.display = "none";
  pendingChangelogUpdate = null;
}
document.getElementById("changelog-close").addEventListener("click", closeChangelogModal);
document.getElementById("changelog-cancel").addEventListener("click", closeChangelogModal);
document.getElementById("changelog-modal").addEventListener("click", (e) => { if (e.target.id === "changelog-modal") closeChangelogModal(); });
document.getElementById("changelog-confirm-update").addEventListener("click", () => {
  if (!pendingChangelogUpdate) return;
  const { item, updateBtn } = pendingChangelogUpdate;
  closeChangelogModal();
  applyUpdate(item, updateBtn);
});

// ---------------- Modpack import / export ----------------
document.getElementById("btn-modpack-import").addEventListener("click", async () => {
  const statusEl = document.getElementById("modpack-import-status");
  const resultsEl = document.getElementById("modpack-import-results");
  resultsEl.innerHTML = "";
  if (!state.selectedServer) { statusEl.textContent = "Select a server in Server Setup first."; statusEl.classList.add("error"); return; }
  if (state.installBlocked) { statusEl.textContent = "This server is running in Crafty — stop it first, then import."; statusEl.classList.add("error"); return; }
  const url = document.getElementById("modpack-url").value.trim();
  const fileInput = document.getElementById("modpack-file");
  const file = fileInput.files[0];
  if (!url && !file) { statusEl.textContent = "Paste a modpack URL or choose a .mrpack file."; statusEl.classList.add("error"); return; }

  statusEl.classList.remove("error");
  statusEl.textContent = "Importing — downloading and installing every server-compatible mod...";

  const form = new FormData();
  form.append("server_path", state.selectedServer.server_path);
  if (url) form.append("url", url);
  if (file) form.append("file", file);

  try {
    const res = await fetch("/api/modpack/import", { method: "POST", body: form });
    const data = await res.json();
    if (data.error) { statusEl.textContent = data.error; statusEl.classList.add("error"); return; }
    statusEl.textContent = `Imported "${data.modpack_name || "modpack"}" — ${data.installed.length} installed` +
      (data.skipped.length ? `, ${data.skipped.length} skipped.` : ".") +
      (data.preview ? " (preview mode — simulated)" : "");
    if (data.skipped.length) {
      resultsEl.innerHTML = "<p class='hint'>Skipped: " + data.skipped.map(s => `${s.file} (${s.reason})`).join(", ") + "</p>";
    }
    document.getElementById("modpack-url").value = "";
    fileInput.value = "";
  } catch (e) {
    statusEl.textContent = "Import failed — check the URL/file and try again.";
    statusEl.classList.add("error");
  }
});

document.getElementById("btn-modpack-export").addEventListener("click", () => {
  if (!state.selectedServer) { toast("Select a server in Server Setup first.", "info"); return; }
  const params = new URLSearchParams({ server_path: state.selectedServer.server_path });
  window.location.href = `/api/modpack/export?${params}`;
});

// ---------------- Settings: CurseForge key ----------------
(async function loadKey() {
  const res = await fetch("/api/settings/curseforge_key");
  const data = await res.json();
  document.getElementById("cf-key").value = data.key || "";
})();
document.getElementById("btn-save-key").addEventListener("click", async () => {
  const key = document.getElementById("cf-key").value.trim();
  await fetch("/api/settings/curseforge_key", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ key }) });
  document.getElementById("settings-status").textContent = "Saved.";
});

// ---------------- Settings: Crafty API ----------------
(async function loadCraftySettings() {
  const res = await fetch("/api/settings/crafty");
  const data = await res.json();
  document.getElementById("crafty-url").value = data.url || "";
  document.getElementById("crafty-token").placeholder = data.token_set ? "(saved — leave blank to keep)" : "paste token here";
  document.getElementById("crafty-servers-root").value = data.servers_root || "/opt/crafty/servers";
  // Crafty always runs on this same box in the standard Anvil setup, so
  // auto-fill the URL the moment Settings loads if nothing's saved yet —
  // one less manual step.
  if (!data.url) {
    try {
      const ipRes = await fetch("/api/network/local_ip");
      const ipData = await ipRes.json();
      if (ipData.ok) {
        document.getElementById("crafty-url").value = ipData.suggested_url;
        document.getElementById("crafty-settings-status").textContent = `Detected ${ipData.ip} — double-check this is the machine running Crafty, then save.`;
      }
    } catch (e) { /* silent — Auto-detect button still works manually */ }
  }
})();
document.getElementById("btn-save-crafty").addEventListener("click", async () => {
  const url = document.getElementById("crafty-url").value.trim();
  const token = document.getElementById("crafty-token").value.trim();
  const servers_root = document.getElementById("crafty-servers-root").value.trim();
  const body = { url, servers_root };
  if (token) body.token = token;
  await fetch("/api/settings/crafty", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  document.getElementById("crafty-settings-status").textContent = "Saved.";
  document.getElementById("crafty-token").value = "";
  loadSavedServers();
  refreshCraftyNagBanner();
});

document.getElementById("btn-detect-crafty-url")?.addEventListener("click", async () => {
  const btn = document.getElementById("btn-detect-crafty-url");
  const status = document.getElementById("crafty-settings-status");
  btn.disabled = true;
  btn.textContent = "Detecting…";
  try {
    const res = await fetch("/api/network/local_ip");
    const data = await res.json();
    if (data.ok) {
      document.getElementById("crafty-url").value = data.suggested_url;
      status.textContent = `Detected ${data.ip} — double-check this is the machine running Crafty, then save.`;
    } else {
      status.textContent = data.error || "Couldn't auto-detect — type the URL in manually.";
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Auto-detect";
  }
});

// ---------------- Self-update check (GitHub) ----------------
async function checkSelfUpdate() {
  try {
    const res = await fetch("/api/self_update_check");
    const data = await res.json();
    const banner = document.getElementById("update-banner");
    if (data.update_available) {
      document.getElementById("update-banner-text").textContent =
        `Update available for Anvil Mod Manager: ${data.current} → ${data.latest} — "${data.latest_message || ""}"`;
      document.getElementById("update-banner-link").href = data.compare_url;
      banner.style.display = "flex";
    } else {
      banner.style.display = "none";
    }
  } catch (e) { /* ignore — GitHub might be unreachable */ }
}
async function applySelfUpdate() {
  const btn = document.getElementById("update-banner-btn");
  btn.disabled = true;
  btn.textContent = "Updating...";
  try {
    const res = await fetch("/api/self_update", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      btn.textContent = "Restarting service...";
      setTimeout(() => location.reload(), 4000);
    } else {
      btn.disabled = false;
      btn.textContent = "Update now";
      toast(data.error || "Update failed.");
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Update now";
  }
}
checkSelfUpdate();

// ---------------- Auto-preview (launched via "Preview Demo" shortcut) ----------------
(async function autoPreviewBootstrap() {
  const params = new URLSearchParams(location.search);
  if (params.get("autopreview") !== "1") return;
  await loadDemoServer();
  document.querySelector('.tab[data-tab="browse"]').click();
  document.getElementById("search-input").value = "sodium";
  runSearch();
})();

renderLoaderFilterGroup();
icons();

// Keep the running/stopped badges and the install gate fresh even if the
// person starts/stops the server in Crafty while this dashboard stays open.
setInterval(() => { loadSavedServers(); }, 20000);

// ---------------- Proactive version-drift check ----------------
// Previously, "the server jar got updated outside this app" was only ever
// caught when someone re-clicked the server in the picker. This polls the
// currently active server in the background so it gets caught either way.
// notifiedDrifts tracks which (path, version) pairs we've already toasted
// about this page load, so a dismissed toast doesn't reappear every poll.
const notifiedDrifts = new Set();
async function checkVersionDrift() {
  const server = state.selectedServer;
  if (!server || !server.server_path || server.server_path.startsWith("demo://")) return;
  try {
    const res = await fetch(`/api/server/version_drift?path=${encodeURIComponent(server.server_path)}`);
    const data = await res.json();
    if (!data.version_changed) return;
    const key = `${server.server_path}:${data.previous_mc_version}->${data.current_mc_version}`;
    if (notifiedDrifts.has(key)) return;
    notifiedDrifts.add(key);

    // The endpoint already corrected the stored version server-side —
    // mirror that locally so the UI (and a follow-up "check updates"
    // click) isn't comparing against the now-stale value in memory.
    if (state.selectedServer && state.selectedServer.server_path === server.server_path) {
      state.selectedServer.mc_version = data.current_mc_version;
      applySelectedServer(state.selectedServer);
    }
    toast(`Detected Minecraft ${data.previous_mc_version} → ${data.current_mc_version} for ${server.name} — someone updated the server jar outside this app. Want to check every mod for updates against the new version?`,
      "info", 15000, { label: "Check updates", onClick: () => { document.querySelector('.tab[data-tab="installed"]')?.click(); runCheckUpdates(); } });
    loadSavedServers();
  } catch (e) { /* transient — next poll will retry */ }
}
setInterval(checkVersionDrift, 60000);

// ---------------- Topbar Crafty health dot ----------------
async function loadCraftyHealth() {
  const dot = document.getElementById("dot-crafty-health");
  const wrap = document.getElementById("crafty-health");
  if (!dot) return;
  try {
    const res = await fetch("/api/crafty/health");
    const data = await res.json();
    if (!data.configured) {
      dot.className = "link-dot dot-grey";
      wrap.title = "Crafty isn't connected yet — set it up in Settings.";
    } else if (data.reachable) {
      dot.className = "link-dot dot-green";
      wrap.title = data.preview ? "Crafty connection OK (preview mode)." : "Connected to Crafty.";
    } else {
      dot.className = "link-dot dot-red";
      wrap.title = "Crafty is configured but not reachable right now.";
    }
  } catch (e) {
    dot.className = "link-dot dot-red";
  }
}
loadCraftyHealth();
setInterval(loadCraftyHealth, 20000);
