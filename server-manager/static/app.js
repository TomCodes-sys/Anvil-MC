// ---------------- Token / fetch helper ----------------
function withToken() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  return token ? ('?token=' + encodeURIComponent(token)) : '';
}

async function api(path, opts) {
  const hasQuery = path.includes('?');
  const tok = withToken();
  const url = path + (tok ? (hasQuery ? '&' + tok.slice(1) : tok) : '');
  const res = await fetch(url, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON response */ }
  return { ok: res.ok, status: res.status, data };
}

function icons() { if (window.lucide) lucide.createIcons(); }

// ---------------- Tabs ----------------
function moveTabIndicator() {
  const active = document.querySelector('.tab.active');
  const indicator = document.getElementById('tab-indicator');
  if (!active || !indicator) return;
  indicator.style.width = active.offsetWidth + 'px';
  indicator.style.transform = `translateX(${active.offsetLeft - 4}px)`;
}
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-' + tab.dataset.tab).classList.add('active');
    moveTabIndicator();
    if (tab.dataset.tab === 'fleet') loadFleet();
    if (tab.dataset.tab === 'rcon') { populateServerSelect('rcon-server-select').then(loadRconTargetForSelected); }
    if (tab.dataset.tab === 'players') { populateServerSelect('players-server-select').then(loadAllPlayerLists); }
    if (tab.dataset.tab === 'logs') { populateServerSelect('logs-server-select').then(loadLogTail); }
    if (tab.dataset.tab === 'backups') { loadBackupSettings(); loadBackupHistory(); loadSnapshots(); }
    if (tab.dataset.tab === 'notifications') loadDiscordSettings();
    if (tab.dataset.tab === 'monitoring') { loadHealth(); loadHealthSettings(); loadCrashSettings(); loadCrashEvents(); }
  });
});
window.addEventListener('load', moveTabIndicator);
window.addEventListener('resize', moveTabIndicator);

// ---------------- Crafty setup gate ----------------
// The dashboard is unusable without Crafty's API (Fleet, Crash Recovery,
// and live status all depend on it), so rather than let people wander in
// and wonder why everything's broken, block access until a token is saved.
(async function initCraftyGate() {
  const overlay = document.getElementById('crafty-gate-overlay');
  const scaffold = document.getElementById('main-scaffold');
  const revealDashboard = () => {
    overlay.style.display = 'none';
    scaffold.style.display = '';
    // The 'load' listener above already fired while the scaffold was
    // display:none (0 offsetWidth/offsetLeft), so the tab indicator needs
    // recalculating now that the layout is actually visible.
    moveTabIndicator();
  };

  try {
    const { data: mode } = await api('/api/mode');
    const isPreview = !!(mode && mode.preview);

    const { data } = await api('/api/settings/crafty_api');
    if (data && data.token_set) { revealDashboard(); return; } // already configured

    overlay.style.display = 'flex';

    if (isPreview) {
      const hint = document.createElement('p');
      hint.className = 'hint';
      hint.style.margin = '0';
      hint.innerHTML = '<strong>Preview mode</strong> — nothing here is real, so there\'s no need to connect an actual Crafty instance.';
      document.querySelector('.crafty-gate-card').insertBefore(hint, document.getElementById('gate-crafty-url').closest('label'));

      const skipBtn = document.createElement('button');
      skipBtn.className = 'btn-secondary';
      skipBtn.type = 'button';
      skipBtn.style.marginTop = '10px';
      skipBtn.textContent = 'Skip — continue in preview mode';
      skipBtn.addEventListener('click', revealDashboard);
      document.getElementById('gate-continue-btn').insertAdjacentElement('afterend', skipBtn);
    }

    const urlInput = document.getElementById('gate-crafty-url');
    const tokenInput = document.getElementById('gate-crafty-token');
    const continueBtn = document.getElementById('gate-continue-btn');
    const linkEl = document.getElementById('gate-open-crafty-link');

    if (data && data.url) {
      urlInput.value = data.url;
      linkEl.href = data.url;
    } else {
      const { data: ipData } = await api('/api/network/local_ip');
      if (ipData && ipData.ok) {
        urlInput.value = ipData.suggested_crafty_url;
        linkEl.href = ipData.suggested_crafty_url;
      } else if (isPreview) {
        urlInput.value = 'https://192.168.1.50:8443';
      }
    }
    urlInput.addEventListener('input', () => { if (urlInput.value) linkEl.href = urlInput.value; });

    function refreshContinueState() {
      continueBtn.disabled = !tokenInput.value.trim();
    }
    tokenInput.addEventListener('input', refreshContinueState);
    refreshContinueState();
    icons();
  } catch (e) {
    // Fail open — a broken network check shouldn't permanently lock
    // someone out of their own dashboard.
    revealDashboard();
  }
})();

async function submitCraftyGate() {
  const url = document.getElementById('gate-crafty-url').value.trim();
  const token = document.getElementById('gate-crafty-token').value.trim();
  const statusEl = document.getElementById('gate-status');
  if (!token) return;
  const btn = document.getElementById('gate-continue-btn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const { data } = await api('/api/settings/crafty_api', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, token }),
  });
  if (data && data.ok) {
    document.getElementById('crafty-gate-overlay').style.display = 'none';
    document.getElementById('main-scaffold').style.display = '';
    moveTabIndicator();
    icons();
  } else {
    statusEl.textContent = 'Failed to save — check the URL and try again.';
    btn.disabled = false;
    btn.textContent = 'Save & continue';
  }
}

// ---------------- Preview mode ----------------
(async function initPreview() {
  const { data } = await api('/api/mode');
  if (data.preview) {
    document.getElementById('preview-banner-slot').innerHTML = `
      <div class="preview-banner" style="max-width:1280px;margin:14px auto 0;">
        <i data-lucide="eye"></i>
        <span><strong>Preview mode</strong> — nothing here touches real Docker/apt/Crafty. Every check/apply is simulated.</span>
      </div>`;
    icons();
  }
})();

// ---------------- Home: companion links + status ----------------
async function loadCompanions() {
  const { data } = await api('/api/companions');
  if (!data) return;
  const map = [
    ['installer', data.installer],
    ['mod_manager', data.mod_manager],
  ];
  map.forEach(([key, info]) => {
    if (!info) return;
    const dashKey = key.replace('_', '-');
    const dot = document.getElementById('dot-' + dashKey);
    const topDot = document.getElementById('topdot-' + dashKey);
    const text = document.getElementById('text-' + dashKey);
    const homeCard = document.getElementById('home-card-' + dashKey);
    const topLink = document.getElementById('link-' + dashKey);
    if (dot) dot.className = 'status-dot ' + (info.reachable ? 'ok' : 'down');
    if (topDot) topDot.className = 'status-dot ' + (info.reachable ? 'ok' : 'down');
    if (text) text.textContent = info.reachable ? 'running' : 'not reachable';
    if (homeCard) homeCard.href = info.url;
    if (topLink) {
      topLink.href = info.url;
      topLink.classList.toggle('disabled', !info.reachable);
    }
  });
}
loadCompanions();
setInterval(loadCompanions, 20000);

async function loadHomeSummary() {
  const { data: fleet } = await api('/api/fleet');
  const el = document.getElementById('home-summary');
  if (!fleet || !fleet.available) {
    el.textContent = fleet && fleet.note ? fleet.note : 'Fleet data unavailable.';
    return;
  }
  const totalServers = fleet.servers.length;
  const totalPending = fleet.servers.reduce((sum, s) => sum + (s.updates_pending || 0), 0);
  el.textContent = totalServers
    ? `${totalServers} server${totalServers === 1 ? '' : 's'} tracked · ${totalPending} update${totalPending === 1 ? '' : 's'} pending across the fleet.`
    : 'No servers tracked yet in Anvil Mod Manager.';
}
loadHomeSummary();

// ---------------- Updates ----------------
const UPDATE_LABELS = {
  self: 'Anvil Server Manager', crafty: 'Crafty Controller', docker: 'Docker Engine', cockpit: 'Cockpit',
};
const bannerState = {};

function renderBanners() {
  const container = document.getElementById('update-banners');
  container.innerHTML = '';
  Object.entries(bannerState).forEach(([key, info]) => {
    if (!info || !info.available) return;
    const div = document.createElement('div');
    div.className = 'update-banner';
    div.innerHTML = `
      <i data-lucide="circle-arrow-up"></i>
      <span class="banner-text">${UPDATE_LABELS[key]} update available${info.detail ? ' — ' + info.detail : ''}.</span>
      <button class="btn-secondary" onclick="applyUpdate('${key}')">Update now</button>
      <button class="btn-ghost" onclick="dismissBanner('${key}')">Dismiss</button>`;
    container.appendChild(div);
  });
  icons();
}
function dismissBanner(key) { if (bannerState[key]) bannerState[key].available = false; renderBanners(); }

async function checkUpdate(key) {
  const pill = document.getElementById(key + '-update-pill');
  const detail = document.getElementById(key + '-update-detail');
  const applyBtn = document.getElementById(key + '-apply-btn');
  pill.textContent = '…'; pill.className = 'update-pill unknown';
  detail.textContent = 'Checking…';

  const { data } = await api(`/api/updates/${key}/check`);
  if (!data || (data.checked === false && !data.update_available)) {
    pill.textContent = 'unknown'; pill.className = 'update-pill unknown';
    detail.textContent = (data && data.note) || 'Could not check.';
    applyBtn.hidden = true;
    bannerState[key] = { available: false };
    renderBanners();
    return;
  }
  if (data.update_available) {
    pill.textContent = 'update available'; pill.className = 'update-pill available';
    detail.textContent = data.current && data.latest ? `${data.current} → ${data.latest}` : (data.latest_message || 'A newer version is available.');
    applyBtn.hidden = false;
    bannerState[key] = { available: true, detail: data.current && data.latest ? `${data.current} → ${data.latest}` : '' };
  } else {
    pill.textContent = 'up to date'; pill.className = 'update-pill uptodate';
    detail.textContent = data.current ? `Currently ${data.current}.` : 'No update available.';
    applyBtn.hidden = true;
    bannerState[key] = { available: false };
  }
  renderBanners();
}

async function applyUpdate(key) {
  const applyBtn = document.getElementById(key + '-apply-btn');
  const detail = document.getElementById(key + '-update-detail');
  if (applyBtn) { applyBtn.disabled = true; applyBtn.textContent = 'Updating…'; }
  const { data } = await api(`/api/updates/${key}/apply`, { method: 'POST' });
  if (!data || !data.ok) {
    detail.textContent = 'Update failed: ' + ((data && data.error) || 'unknown error');
    if (applyBtn) { applyBtn.disabled = false; applyBtn.textContent = 'Update now'; }
    return;
  }
  detail.textContent = data.restarting ? 'Updated — restarting…' : 'Updated.';
  dismissBanner(key);
  if (applyBtn) applyBtn.hidden = true;
  setTimeout(() => checkUpdate(key), key === 'self' ? 4000 : 1500);
}

['self', 'crafty', 'docker', 'cockpit'].forEach(key => checkUpdate(key));

// ---------------- Fleet ----------------
let fleetServers = [];

async function loadFleet() {
  const { data } = await api('/api/fleet');
  const table = document.getElementById('fleet-table');
  const empty = document.getElementById('fleet-empty');
  const tbody = document.getElementById('fleet-tbody');
  const noteEl = document.getElementById('fleet-note');
  if (!data || !data.available || !data.servers.length) {
    table.hidden = true;
    empty.hidden = false;
    empty.textContent = (data && data.note) || 'No servers tracked yet — check your Crafty URL/token in Settings.';
    fleetServers = [];
    if (noteEl) noteEl.hidden = true;
    return;
  }
  fleetServers = data.servers;
  empty.hidden = true;
  table.hidden = false;
  if (noteEl) {
    noteEl.hidden = !data.note;
    if (data.note) noteEl.textContent = data.note;
  }
  tbody.innerHTML = '';
  data.servers.forEach(s => {
    const tr = document.createElement('tr');
    const isBedrock = s.platform === 'bedrock';
    tr.innerHTML = `
      <td>${s.name}${isBedrock ? ' <span class="platform-badge">Bedrock</span>' : ''}</td>
      <td>${s.mc_version}</td>
      <td>${s.loader}</td>
      <td>${s.mods}</td>
      <td>${s.plugins}</td>
      <td>${s.datapacks}</td>
      <td class="fleet-pending ${s.updates_pending ? '' : 'zero'}">${s.updates_pending}</td>
      <td style="display:flex; gap:6px; flex-wrap:wrap;">
        ${isBedrock ? '' : `<button class="btn-ghost btn-small" onclick="jumpToServerTab('rcon-server-select', 'rcon', '${s.server_path}')">RCON</button>`}
        <button class="btn-ghost btn-small" onclick="jumpToServerTab('players-server-select', 'players', '${s.server_path}')">Players</button>
        <button class="btn-ghost btn-small" onclick="jumpToServerTab('logs-server-select', 'logs', '${s.server_path}')">Logs</button>
      </td>`;
    tbody.appendChild(tr);
  });
}

function jumpToServerTab(selectId, tabName, serverPath) {
  document.querySelector(`.tab[data-tab="${tabName}"]`).click();
  setTimeout(() => {
    const select = document.getElementById(selectId);
    if (select) select.value = serverPath;
    if (tabName === 'rcon') loadRconTargetForSelected();
    if (tabName === 'players') loadAllPlayerLists();
    if (tabName === 'logs') loadLogTail();
  }, 60);
}

function currentServerPlatform(serverPath) {
  const s = fleetServers.find(x => x.server_path === serverPath);
  return s ? s.platform : 'java';
}

async function populateServerSelect(selectId) {
  if (!fleetServers.length) await loadFleet();
  const select = document.getElementById(selectId);
  const current = select.value;
  select.innerHTML = '';
  fleetServers.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.server_path;
    opt.dataset.platform = s.platform || 'java';
    opt.textContent = s.platform === 'bedrock' ? `${s.name} (Bedrock)` : s.name;
    select.appendChild(opt);
  });
  if (current && fleetServers.some(s => s.server_path === current)) select.value = current;
}

// ---------------- RCON ----------------
async function loadRconTargetForSelected() {
  const path = document.getElementById('rcon-server-select').value;
  const notice = document.getElementById('rcon-bedrock-notice');
  const form = document.getElementById('rcon-form');
  const consoleCard = document.getElementById('rcon-console-card');
  if (currentServerPlatform(path) === 'bedrock') {
    if (notice) notice.hidden = false;
    if (form) form.hidden = true;
    if (consoleCard) consoleCard.hidden = true;
    return;
  }
  if (notice) notice.hidden = true;
  if (form) form.hidden = false;
  if (consoleCard) consoleCard.hidden = false;
  const { data } = await api('/api/rcon/targets');
  const t = data && data[path];
  document.getElementById('rcon-host').value = (t && t.host) || '127.0.0.1';
  document.getElementById('rcon-port').value = (t && t.port) || 25575;
  document.getElementById('rcon-password').placeholder = (t && t.password_set) ? '(saved — leave blank to keep)' : 'from server.properties';
  document.getElementById('rcon-output').textContent = '';
}
document.getElementById('rcon-server-select')?.addEventListener('change', loadRconTargetForSelected);

async function saveRconTarget() {
  const server_path = document.getElementById('rcon-server-select').value;
  const body = {
    server_path,
    host: document.getElementById('rcon-host').value.trim() || '127.0.0.1',
    port: parseInt(document.getElementById('rcon-port').value, 10) || 25575,
  };
  const pw = document.getElementById('rcon-password').value.trim();
  if (pw) body.password = pw;
  const { data } = await api('/api/rcon/targets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  document.getElementById('rcon-target-status').textContent = data && data.ok ? 'Saved.' : 'Failed to save.';
  document.getElementById('rcon-password').value = '';
  loadRconTargetForSelected();
}

async function sendRcon(command) {
  const server_path = document.getElementById('rcon-server-select').value;
  const output = document.getElementById('rcon-output');
  output.textContent += `> ${command}\n`;
  const { data } = await api('/api/rcon/command', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ server_path, command }) });
  output.textContent += (data && data.ok ? data.response : ('Error: ' + ((data && data.error) || 'unknown error'))) + '\n\n';
  output.scrollTop = output.scrollHeight;
}
function sendRconFromInput() {
  const input = document.getElementById('rcon-command-input');
  const cmd = input.value.trim();
  if (!cmd) return;
  input.value = '';
  sendRcon(cmd);
}
function promptBroadcast() {
  const msg = prompt('Message to broadcast to all players:');
  if (msg) sendRcon('say ' + msg);
}

// ---------------- Players (whitelist/ops/bans) ----------------
const PLAYER_KINDS = ['whitelist', 'ops', 'banned-players', 'banned-ips'];
document.getElementById('players-server-select')?.addEventListener('change', loadAllPlayerLists);

async function loadAllPlayerLists() {
  const server_path = document.getElementById('players-server-select').value;
  const isBedrock = currentServerPlatform(server_path) === 'bedrock';
  const notice = document.getElementById('players-bedrock-notice');
  if (notice) notice.hidden = !isBedrock;
  ['ops-card', 'banned-players-card', 'banned-ips-card'].forEach(id => {
    const card = document.getElementById(id);
    if (card) card.hidden = isBedrock;
  });
  const kinds = isBedrock ? ['whitelist'] : PLAYER_KINDS;
  kinds.forEach(loadPlayerList);
}

async function loadPlayerList(kind) {
  const server_path = document.getElementById('players-server-select').value;
  const { data } = await api(`/api/playerfiles/${kind}?server_path=${encodeURIComponent(server_path)}`);
  const el = document.getElementById(kind + '-list');
  if (!data || !data.ok) { el.innerHTML = `<p class="hint" style="margin:0;">${(data && data.error) || 'Could not load.'}</p>`; return; }
  if (!data.entries.length) { el.innerHTML = '<p class="hint" style="margin:0;">Empty.</p>'; return; }
  el.innerHTML = '';
  data.entries.forEach(e => {
    const row = document.createElement('div');
    row.className = 'snapshot-row';
    const label = kind === 'banned-ips' ? e.ip : `${e.name}${e.reason ? ' — ' + e.reason : ''}`;
    const key = kind === 'banned-ips' ? e.ip : e.uuid;
    row.innerHTML = `<span>${label}</span><button class="btn-ghost btn-small" onclick="removePlayer('${kind}', '${key}')">Remove</button>`;
    el.appendChild(row);
  });
}

async function addPlayer(kind) {
  const server_path = document.getElementById('players-server-select').value;
  const body = { server_path };
  if (kind === 'banned-ips') {
    const ip = document.getElementById('banned-ips-ip-input').value.trim();
    if (!ip) return;
    body.ip = ip;
  } else {
    const input = document.getElementById(kind + '-username-input');
    const username = input.value.trim();
    if (!username) return;
    body.username = username;
  }
  const { data } = await api(`/api/playerfiles/${kind}/add`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!data || !data.ok) { alert((data && data.error) || 'Failed to add.'); return; }
  if (kind === 'banned-ips') document.getElementById('banned-ips-ip-input').value = '';
  else document.getElementById(kind + '-username-input').value = '';
  loadPlayerList(kind);
}

async function removePlayer(kind, key) {
  const server_path = document.getElementById('players-server-select').value;
  const body = { server_path };
  if (kind === 'banned-ips') body.ip = key; else body.uuid = key;
  await api(`/api/playerfiles/${kind}/remove`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  loadPlayerList(kind);
}

// ---------------- Log / crash analyzer ----------------
document.getElementById('logs-server-select')?.addEventListener('change', loadLogTail);

async function loadLogTail() {
  const server_path = document.getElementById('logs-server-select').value;
  const { data } = await api(`/api/logs/tail?server_path=${encodeURIComponent(server_path)}`);
  const outputEl = document.getElementById('log-tail-output');
  const findingsCard = document.getElementById('log-findings-card');
  const findingsList = document.getElementById('log-findings-list');
  if (!data || !data.ok) {
    outputEl.textContent = (data && data.error) || 'Could not load log.';
    findingsCard.hidden = true;
    return;
  }
  outputEl.textContent = data.lines.join('\n');
  outputEl.scrollTop = outputEl.scrollHeight;
  if (data.findings && data.findings.length) {
    findingsCard.hidden = false;
    findingsList.innerHTML = '';
    data.findings.forEach(f => {
      const row = document.createElement('div');
      row.className = 'history-row failed';
      row.innerHTML = `<i data-lucide="alert-triangle"></i><span><strong>${f.explanation}</strong><br><code>${f.line}</code></span>`;
      findingsList.appendChild(row);
    });
    icons();
  } else {
    findingsCard.hidden = true;
  }
}

icons();

// ---------------- Backup Manager ----------------
function showRepoFields(type) {
  ['local', 's3', 'b2'].forEach(t => {
    document.getElementById('repo-fields-' + t).classList.toggle('active', t === type);
  });
}
document.getElementById('backup-repo-type').addEventListener('change', (e) => showRepoFields(e.target.value));

function addSourcePathField(value) {
  const list = document.getElementById('source-paths-list');
  const row = document.createElement('div');
  row.className = 'source-path-row';
  row.innerHTML = `<input type="text" value="${value || ''}" placeholder="/opt/crafty/servers">
                    <button class="btn-ghost btn-small" onclick="this.parentElement.remove()"><i data-lucide="x"></i></button>`;
  list.appendChild(row);
  icons();
}

async function loadBackupSettings() {
  const { data: b } = await api('/api/backup/settings');
  if (!b) return;
  document.getElementById('backup-repo-type').value = b.repo_type || 'local';
  showRepoFields(b.repo_type || 'local');
  document.getElementById('backup-repo-path').value = b.repo_path || '';
  document.getElementById('backup-s3-endpoint').value = b.s3_endpoint || '';
  document.getElementById('backup-s3-bucket').value = b.s3_bucket || '';
  document.getElementById('backup-s3-access-key').value = b.s3_access_key || '';
  document.getElementById('backup-s3-secret-key').placeholder = b.s3_secret_key_set ? '(saved — leave blank to keep)' : '';
  document.getElementById('backup-b2-bucket').value = b.b2_bucket || '';
  document.getElementById('backup-b2-account-id').value = b.b2_account_id || '';
  document.getElementById('backup-b2-account-key').placeholder = b.b2_account_key_set ? '(saved — leave blank to keep)' : '';
  document.getElementById('backup-restic-password').placeholder = b.restic_password_set ? '(saved — leave blank to keep)' : 'a strong passphrase';
  document.getElementById('backup-keep-last').value = b.keep_last || 14;
  document.getElementById('backup-schedule-enabled').checked = !!b.schedule_enabled;
  document.getElementById('backup-interval-hours').value = b.schedule_interval_hours || 24;

  const list = document.getElementById('source-paths-list');
  list.innerHTML = '';
  (b.source_paths && b.source_paths.length ? b.source_paths : ['/opt/crafty/servers']).forEach(p => addSourcePathField(p));
}

async function saveBackupSettings() {
  const body = {
    repo_type: document.getElementById('backup-repo-type').value,
    repo_path: document.getElementById('backup-repo-path').value.trim(),
    s3_endpoint: document.getElementById('backup-s3-endpoint').value.trim(),
    s3_bucket: document.getElementById('backup-s3-bucket').value.trim(),
    s3_access_key: document.getElementById('backup-s3-access-key').value.trim(),
    b2_bucket: document.getElementById('backup-b2-bucket').value.trim(),
    b2_account_id: document.getElementById('backup-b2-account-id').value.trim(),
    keep_last: parseInt(document.getElementById('backup-keep-last').value, 10) || 14,
    schedule_enabled: document.getElementById('backup-schedule-enabled').checked,
    schedule_interval_hours: parseInt(document.getElementById('backup-interval-hours').value, 10) || 24,
    source_paths: Array.from(document.querySelectorAll('#source-paths-list input')).map(i => i.value.trim()).filter(Boolean),
  };
  const s3Secret = document.getElementById('backup-s3-secret-key').value.trim();
  if (s3Secret) body.s3_secret_key = s3Secret;
  const b2Key = document.getElementById('backup-b2-account-key').value.trim();
  if (b2Key) body.b2_account_key = b2Key;
  const resticPw = document.getElementById('backup-restic-password').value.trim();
  if (resticPw) body.restic_password = resticPw;

  const { data } = await api('/api/backup/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  document.getElementById('backup-settings-status').textContent = data && data.ok ? 'Saved.' : 'Failed to save.';
  document.getElementById('backup-s3-secret-key').value = '';
  document.getElementById('backup-b2-account-key').value = '';
  document.getElementById('backup-restic-password').value = '';
  loadBackupSettings();
}

async function initBackupRepo() {
  const statusEl = document.getElementById('backup-repo-status');
  statusEl.textContent = 'Initializing…'; statusEl.classList.remove('error');
  await saveBackupSettings();
  const { data } = await api('/api/backup/init', { method: 'POST' });
  if (data && data.ok) {
    statusEl.textContent = data.already_initialized ? 'Repository already initialized.' : 'Repository initialized.';
  } else {
    statusEl.textContent = (data && data.error) || 'Failed to initialize.';
    statusEl.classList.add('error');
  }
}

async function runBackupNow() {
  const btn = document.getElementById('btn-run-backup');
  const statusEl = document.getElementById('backup-run-status');
  btn.disabled = true; btn.textContent = 'Running…';
  statusEl.textContent = 'Backup in progress — this can take a while for a first run.';
  statusEl.classList.remove('error');
  const { data } = await api('/api/backup/run', { method: 'POST' });
  btn.disabled = false;
  btn.innerHTML = '<i data-lucide="play"></i>Run backup now';
  icons();
  if (data && data.ok) {
    statusEl.textContent = `Backup completed in ${data.duration_s}s.`;
  } else {
    statusEl.textContent = 'Backup failed: ' + ((data && data.error) || 'unknown error');
    statusEl.classList.add('error');
  }
  loadBackupHistory();
}

async function loadBackupHistory() {
  const { data } = await api('/api/backup/history');
  const el = document.getElementById('backup-history-list');
  if (!data || !data.length) { el.innerHTML = '<p class="hint" style="margin:0;">No backups run yet.</p>'; return; }
  el.innerHTML = '';
  data.forEach(h => {
    const row = document.createElement('div');
    row.className = 'history-row ' + (h.ok ? 'ok' : 'failed');
    const when = new Date(h.started_at * 1000).toLocaleString();
    row.innerHTML = `<i data-lucide="${h.ok ? 'circle-check' : 'circle-x'}"></i>
      <span>${when} · ${h.trigger}${h.ok ? ` · ${h.duration_s}s` : ' · ' + (h.error || 'failed')}</span>`;
    el.appendChild(row);
  });
  icons();
}

async function loadSnapshots() {
  const { data } = await api('/api/backup/snapshots');
  const el = document.getElementById('snapshot-list');
  if (!data || !data.ok || !data.snapshots.length) {
    el.innerHTML = `<p class="hint" style="margin:0;">${(data && data.error) || 'No snapshots yet.'}</p>`;
    return;
  }
  el.innerHTML = '';
  data.snapshots.forEach(s => {
    const row = document.createElement('div');
    row.className = 'snapshot-row';
    const when = s.time ? new Date(s.time).toLocaleString() : 'unknown time';
    row.innerHTML = `<span><code>${s.id}</code> — ${when}</span>
      <button class="btn-secondary btn-small" onclick="restoreSnapshot('${s.id}')">Restore to staging folder</button>`;
    el.appendChild(row);
  });
}

async function restoreSnapshot(id) {
  if (!confirm(`Restore snapshot ${id} to a new staging folder (/opt/anvil-backups/restore-${id})? This never overwrites a live world directly.`)) return;
  const { data } = await api('/api/backup/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_id: id }) });
  const el = document.getElementById('snapshot-list');
  if (!(data && data.ok)) {
    alert('Restore failed: ' + ((data && data.error) || 'unknown error'));
    return;
  }
  // restic preserves whatever UID/GID was on disk at backup time, which is
  // very often wrong on a different machine (or just wrong for Crafty,
  // which needs root) — offer to fix that immediately rather than making
  // the person discover a permissions error later.
  const box = document.createElement('div');
  box.className = 'restore-result';
  box.innerHTML = `<p class="hint" style="margin:6px 0;">Restored to <code>${data.target_dir}</code>. Files may have the wrong owner if this came from a different machine.</p>
    <button class="btn-secondary btn-small" onclick="chownRestored('${data.target_dir}', this)"><i data-lucide="shield-check"></i>Fix ownership (chown -R root:root)</button>`;
  el.prepend(box);
  icons();
}

async function chownRestored(targetDir, btn) {
  if (!confirm(`Recursively chown root:root on:\n${targetDir}\n\nThis affects every file under that folder.`)) return;
  btn.disabled = true;
  btn.textContent = 'Fixing ownership…';
  const { data } = await api('/api/backup/chown', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target_dir: targetDir, owner: 'root:root' }) });
  if (data && data.ok) {
    btn.textContent = 'Ownership fixed ✓';
  } else {
    btn.disabled = false;
    btn.textContent = 'Fix ownership (chown -R root:root)';
    alert('chown failed: ' + ((data && data.error) || 'unknown error'));
  }
}

// ---------------- Notifications (Discord) ----------------
async function loadDiscordSettings() {
  const { data } = await api('/api/notifications/discord');
  if (!data) return;
  document.getElementById('discord-webhook-url').placeholder = data.webhook_set ? '(saved — leave blank to keep)' : 'https://discord.com/api/webhooks/...';
  document.getElementById('notify-backup-complete').checked = data.notify_backup_complete !== false;
  document.getElementById('notify-backup-failed').checked = data.notify_backup_failed !== false;
  document.getElementById('notify-update-available').checked = data.notify_update_available !== false;
}

async function saveDiscordSettings() {
  const body = {
    notify_backup_complete: document.getElementById('notify-backup-complete').checked,
    notify_backup_failed: document.getElementById('notify-backup-failed').checked,
    notify_update_available: document.getElementById('notify-update-available').checked,
  };
  const url = document.getElementById('discord-webhook-url').value.trim();
  if (url) body.webhook_url = url;
  const { data } = await api('/api/notifications/discord', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  document.getElementById('discord-status').textContent = data && data.ok ? 'Saved.' : 'Failed to save.';
  document.getElementById('discord-webhook-url').value = '';
  loadDiscordSettings();
}

async function testDiscord() {
  const statusEl = document.getElementById('discord-status');
  statusEl.textContent = 'Sending…'; statusEl.classList.remove('error');
  const { data } = await api('/api/notifications/test', { method: 'POST' });
  if (data && data.ok) {
    statusEl.textContent = 'Test message sent.';
  } else {
    statusEl.textContent = 'Failed: ' + ((data && data.error) || 'unknown error');
    statusEl.classList.add('error');
  }
}

// ---------------- Health Monitor ----------------
function _healthDotClass(pct, warn, crit) {
  if (pct === null || pct === undefined) return 'unlinked';
  if (pct >= crit) return 'stopped';   // red
  if (pct >= warn) return 'warn';      // amber
  return 'running';                   // green
}

async function loadHealth() {
  const grid = document.getElementById('health-grid');
  grid.innerHTML = '<p class="hint">Loading…</p>';
  const [{ data: snap }, { data: thresholds }] = await Promise.all([
    api('/api/health/system'),
    api('/api/health/settings'),
  ]);
  if (!snap) { grid.innerHTML = '<p class="hint">Couldn\'t load health data.</p>'; return; }
  const t = thresholds || {};
  grid.innerHTML = '';

  const cpuCard = document.createElement('div');
  cpuCard.className = 'health-card';
  const cpuVal = snap.cpu_temp_c;
  cpuCard.innerHTML = `<span class="status-dot ${_healthDotClass(cpuVal, t.cpu_temp_warn_c, t.cpu_temp_crit_c)}"></span>
    <strong>CPU Temperature</strong><span class="health-value">${cpuVal !== null && cpuVal !== undefined ? cpuVal + '°C' : 'not available on this hardware'}</span>`;
  grid.appendChild(cpuCard);

  const ramCard = document.createElement('div');
  ramCard.className = 'health-card';
  const ram = snap.ram;
  ramCard.innerHTML = `<span class="status-dot ${ram ? _healthDotClass(ram.used_pct, t.ram_warn_pct, t.ram_crit_pct) : 'unlinked'}"></span>
    <strong>RAM</strong><span class="health-value">${ram ? `${ram.used_pct}% of ${(ram.total_mb / 1024).toFixed(1)} GB` : 'unavailable'}</span>`;
  grid.appendChild(ramCard);

  (snap.disks || []).forEach(d => {
    const card = document.createElement('div');
    card.className = 'health-card';
    card.innerHTML = `<span class="status-dot ${_healthDotClass(d.used_pct, t.disk_warn_pct, t.disk_crit_pct)}"></span>
      <strong>Disk ${d.mount}</strong><span class="health-value">${d.used_pct}% of ${d.total_gb} GB</span>`;
    grid.appendChild(card);
  });

  const smart = snap.smart || {};
  if (smart.available) {
    (smart.disks || []).forEach(sd => {
      const card = document.createElement('div');
      card.className = 'health-card';
      const cls = sd.health === 'failed' ? 'stopped' : (sd.health === 'passed' ? 'running' : 'unlinked');
      card.innerHTML = `<span class="status-dot ${cls}"></span>
        <strong>SMART ${sd.device}</strong><span class="health-value">${sd.health}</span>`;
      grid.appendChild(card);
    });
  } else {
    const card = document.createElement('div');
    card.className = 'health-card';
    card.innerHTML = `<span class="status-dot unlinked"></span><strong>SMART</strong><span class="health-value">smartmontools not installed</span>`;
    grid.appendChild(card);
  }

  (snap.network || []).forEach(n => {
    const card = document.createElement('div');
    card.className = 'health-card';
    card.innerHTML = `<span class="status-dot running"></span>
      <strong>${n.iface}</strong><span class="health-value">↓ ${n.rx_kbps} KB/s · ↑ ${n.tx_kbps} KB/s</span>`;
    grid.appendChild(card);
  });

  icons();
}

async function loadHealthSettings() {
  const { data } = await api('/api/health/settings');
  if (!data) return;
  document.getElementById('health-disk-warn').value = data.disk_warn_pct;
  document.getElementById('health-disk-crit').value = data.disk_crit_pct;
  document.getElementById('health-cpu-warn').value = data.cpu_temp_warn_c;
  document.getElementById('health-cpu-crit').value = data.cpu_temp_crit_c;
  document.getElementById('health-ram-warn').value = data.ram_warn_pct;
  document.getElementById('health-ram-crit').value = data.ram_crit_pct;
}

async function saveHealthSettings() {
  const body = {
    disk_warn_pct: document.getElementById('health-disk-warn').value,
    disk_crit_pct: document.getElementById('health-disk-crit').value,
    cpu_temp_warn_c: document.getElementById('health-cpu-warn').value,
    cpu_temp_crit_c: document.getElementById('health-cpu-crit').value,
    ram_warn_pct: document.getElementById('health-ram-warn').value,
    ram_crit_pct: document.getElementById('health-ram-crit').value,
  };
  const { data } = await api('/api/health/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  document.getElementById('health-settings-status').textContent = data && data.ok ? 'Saved.' : 'Failed to save.';
}

// ---------------- Crash Recovery ----------------
async function saveCrashCraftyApi() {
  const body = {};
  const url = document.getElementById('crash-crafty-url').value.trim();
  const token = document.getElementById('crash-crafty-token').value.trim();
  if (url) body.url = url;
  if (token) body.token = token;
  const { data } = await api('/api/settings/crafty_api', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  document.getElementById('crash-crafty-status').textContent = data && data.ok ? 'Saved.' : 'Failed to save.';
  document.getElementById('crash-crafty-token').value = '';
  const { data: current } = await api('/api/settings/crafty_api');
  if (current) {
    document.getElementById('crash-crafty-url').value = current.url || '';
    document.getElementById('crash-crafty-token').placeholder = current.token_set ? '(saved — leave blank to keep)' : 'paste an API token generated in Crafty';
    updateOpenCraftyLink(current.url);
  }
}

async function loadCrashSettings() {
  const [{ data: cr }, { data: apiCfg }] = await Promise.all([
    api('/api/crash_recovery/settings'),
    api('/api/settings/crafty_api'),
  ]);
  if (cr) {
    document.getElementById('crash-enabled').checked = !!cr.enabled;
    document.getElementById('crash-auto-restart').checked = cr.auto_restart !== false;
    document.getElementById('crash-interval').value = cr.check_interval_s || 60;
  }
  if (apiCfg) {
    document.getElementById('crash-crafty-url').value = apiCfg.url || '';
    document.getElementById('crash-crafty-token').placeholder = apiCfg.token_set ? '(saved — leave blank to keep)' : 'paste an API token generated in Crafty';
  }
  // Crafty always runs on this same box, so auto-fill the URL the moment
  // this tab opens if nothing's set yet — one less thing to type in.
  if (!apiCfg || !apiCfg.url) {
    await detectCraftyUrl(/* silent */ true);
  } else {
    updateOpenCraftyLink(apiCfg.url);
  }
}

function updateOpenCraftyLink(url) {
  const link = document.getElementById('crash-open-crafty-link');
  if (link && url) link.href = url;
}

async function detectCraftyUrl(silent) {
  const { data } = await api('/api/network/local_ip');
  const statusEl = document.getElementById('crash-crafty-status');
  if (data && data.ok) {
    document.getElementById('crash-crafty-url').value = data.suggested_crafty_url;
    updateOpenCraftyLink(data.suggested_crafty_url);
    if (!silent) statusEl.textContent = `Detected this machine's IP — filled in ${data.suggested_crafty_url}.`;
  } else if (!silent) {
    statusEl.textContent = (data && data.error) || "Couldn't auto-detect — type the URL in manually.";
  }
}

async function saveCrashSettings() {
  const body = {
    enabled: document.getElementById('crash-enabled').checked,
    auto_restart: document.getElementById('crash-auto-restart').checked,
    check_interval_s: document.getElementById('crash-interval').value,
  };
  const { data } = await api('/api/crash_recovery/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  document.getElementById('crash-settings-status').textContent = data && data.ok ? 'Saved.' : 'Failed to save.';
}

async function loadCrashEvents() {
  const { data } = await api('/api/crash_recovery/events');
  const el = document.getElementById('crash-events-list');
  if (!data || !data.length) {
    el.innerHTML = '<p class="hint" style="margin:0;">No crashes detected yet.</p>';
    return;
  }
  el.innerHTML = '';
  [...data].reverse().forEach(e => {
    const row = document.createElement('div');
    row.className = 'snapshot-row';
    const when = e.time ? new Date(e.time * 1000).toLocaleString() : 'unknown time';
    const bits = [];
    bits.push(e.auto_restarted ? 'restarted automatically' : 'not auto-restarted');
    if (e.corrupt_suspected) bits.push(`⚠️ possible corruption: ${e.corrupt_reasons.join('; ')}`);
    row.innerHTML = `<span><strong>${e.server}</strong> — ${when}<br><span class="hint">${bits.join(' · ')} · logs: <code>${e.log_dir}</code></span></span>`;
    el.appendChild(row);
  });
}

icons();
