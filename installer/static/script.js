// ---------------- Setup ----------------

const STEP_DEFS = [
  {
    id: 'firewall',
    title: 'Update system & configure firewall',
    desc: 'Updates Ubuntu packages, installs PAM support so the Terminal tab works, opens SSH/Cockpit/Crafty/Java/Bedrock ports, and stops the machine sleeping if the lid closes. Re-enables SSH first so you can\'t get locked out.',
  },
  {
    id: 'docker',
    title: 'Install Docker',
    desc: 'Required to run Crafty Controller.',
  },
  {
    id: 'crafty',
    title: 'Install Crafty Controller',
    desc: 'Pulls and starts Crafty in Docker on port 8443. Manage your Java and Bedrock servers from its UI. Login info appears below automatically once this finishes. Crafty uses a self-signed certificate, so your browser will show a security warning the first time you open it — that\'s expected, click through it.',
  },
  {
    id: 'cockpit',
    title: 'Install Cockpit',
    desc: 'Web-based system admin panel for the Ubuntu box itself, on port 9090 — includes Cockpit Explorer (a built-in file-manager tab) automatically, no separate step needed.',
  },
];

let currentIndex = 0;
const doneSteps = new Set();
let running = false;

// ---------------- Advanced mode ----------------

function isAdvanced() {
  return document.body.classList.contains('advanced');
}

function onAdvancedToggle(e) {
  if (e.target.checked) {
    const ok = confirm(
      "Advanced mode removes step locks and unlocks all links immediately.\n" +
      "This is for people who know what they're doing and are comfortable running steps out of order.\n\n" +
      "Proceed?"
    );
    if (!ok) { e.target.checked = false; return; }
  }
  document.body.classList.toggle('advanced', e.target.checked);
  localStorage.setItem('anvil-installer-advanced', e.target.checked ? '1' : '0');
  renderRail();
  updateTopbarLinks(lastStatus);
}

function initAdvancedMode() {
  const saved = localStorage.getItem('anvil-installer-advanced') === '1';
  document.getElementById('advanced-toggle').checked = saved;
  document.body.classList.toggle('advanced', saved);
}

// ---------------- Prerequisites ----------------

function toggleInfo(key) {
  const el = document.getElementById('info-' + key);
  const btn = event.currentTarget;
  const opening = !el.classList.contains('open');

  if (opening) {
    el.hidden = false;
    void el.offsetHeight; // force reflow so the transition actually plays
    requestAnimationFrame(() => el.classList.add('open'));
    btn.classList.add('active');
  } else {
    el.classList.remove('open');
    btn.classList.remove('active');
    setTimeout(() => { if (!el.classList.contains('open')) el.hidden = true; }, 300);
  }
}

function copyToClipboard(text) {
  // navigator.clipboard only exists in a "secure context" (https:// or
  // localhost) — this dashboard is plain http://<ip>:8090, so that API is
  // simply undefined here and every copy button silently did nothing.
  // execCommand('copy') via a hidden textarea works fine over plain HTTP.
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error('execCommand copy failed'));
    } catch (e) {
      document.body.removeChild(ta);
      reject(e);
    }
  });
}

function copyPort(id, btn) {
  const text = document.getElementById(id).textContent;
  copyToClipboard(text).then(() => {
    // Clear any pending reset from a previous click so rapid clicks
    // can't race each other and leave the button stuck on "copied".
    clearTimeout(btn._copyResetTimer);
    btn.classList.remove('copied');
    void btn.offsetWidth; // restart the pop animation even on repeat clicks
    btn.textContent = 'copied';
    btn.classList.add('copied');
    btn._copyResetTimer = setTimeout(() => {
      btn.textContent = 'copy';
      btn.classList.remove('copied');
      btn._copyResetTimer = null;
    }, 1200);
  }).catch(() => {
    btn.textContent = 'select manually';
    setTimeout(() => { btn.textContent = 'copy'; }, 1500);
  });
}

// ---------------- Step rail ----------------

function renderRail() {
  const rail = document.getElementById('step-rail');
  rail.innerHTML = '';
  const advanced = isAdvanced();
  STEP_DEFS.forEach((step, i) => {
    const pill = document.createElement('div');
    const isDone = doneSteps.has(step.id);
    const isActive = i === currentIndex;
    const prevDone = i === 0 || doneSteps.has(STEP_DEFS[i - 1].id) || STEP_DEFS[i - 1].optional;
    const isLocked = !advanced && !step.optional && !isDone && !isActive && !prevDone;

    pill.className = 'rail-pill' + (isActive ? ' active' : '') + (isDone ? ' done' : '') + (isLocked ? ' locked' : '') + (step.optional ? ' optional' : '');
    // Staggered entrance, scales to any number of steps (no per-step CSS rule needed).
    pill.style.animationDelay = `${Math.min(i, 12) * 50}ms`;
    if (!isLocked) {
      pill.classList.add('clickable');
      pill.onclick = () => { if (!running) { currentIndex = i; renderActiveStep(); renderRail(); } };
    }

    const num = document.createElement('span');
    num.className = 'rail-num';
    num.textContent = isDone ? '✓' : String(i + 1).padStart(2, '0');
    pill.appendChild(num);

    const label = document.createElement('span');
    label.textContent = step.title;
    pill.appendChild(label);

    if (step.optional) {
      const tag = document.createElement('span');
      tag.className = 'rail-optional-tag';
      tag.textContent = 'optional';
      pill.appendChild(tag);
    }

    rail.appendChild(pill);
  });
}

// ---------------- Active step box ----------------

function renderActiveStep() {
  const step = STEP_DEFS[currentIndex];
  const box = document.getElementById('active-step-box');
  box.classList.remove('running');

  document.getElementById('active-step-title').textContent = step.title;
  document.getElementById('active-step-desc').textContent = step.desc;

  const stateEl = document.getElementById('active-step-state');
  const isDone = doneSteps.has(step.id);
  stateEl.textContent = isDone ? 'done' : (step.optional ? 'optional' : 'pending');
  stateEl.className = 'step-state' + (isDone ? ' done' : '');

  const runBtn = document.getElementById('run-current-btn');
  const nextBtn = document.getElementById('next-btn');
  runBtn.disabled = false;
  runBtn.textContent = isDone ? 'Run again' : 'Run this step';
  nextBtn.hidden = !isDone || currentIndex === STEP_DEFS.length - 1;

  const miniConsole = document.getElementById('mini-console');
  if (!isDone) {
    miniConsole.classList.remove('visible');
    miniConsole.innerHTML = '';
  }
}

function logLine(text, cls) {
  const el = document.createElement('div');
  el.className = 'console-line' + (cls ? ' ' + cls : '');
  el.textContent = text;
  const c = document.getElementById('mini-console');
  c.appendChild(el);
  c.scrollTop = c.scrollHeight;
}

function runCurrentStep() {
  if (running) return;
  running = true;

  const step = STEP_DEFS[currentIndex];
  const box = document.getElementById('active-step-box');
  const stateEl = document.getElementById('active-step-state');
  const runBtn = document.getElementById('run-current-btn');
  const nextBtn = document.getElementById('next-btn');
  const miniConsole = document.getElementById('mini-console');

  box.classList.add('running');
  stateEl.textContent = 'running';
  stateEl.className = 'step-state running';
  runBtn.disabled = true;
  nextBtn.hidden = true;
  miniConsole.classList.add('visible');
  miniConsole.innerHTML = '';
  logLine('$ running step: ' + step.id, 'dim');

  const url = '/api/run/' + step.id + withToken();
  const source = new EventSource(url);

  source.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.line !== undefined) {
      logLine(data.line);
    }
    if (data.done) {
      source.close();
      running = false;
      box.classList.remove('running');
      runBtn.disabled = false;

      if (data.exit_code === 0) {
        doneSteps.add(step.id);
        stateEl.textContent = 'done';
        stateEl.className = 'step-state done';
        logLine('>> step complete.', 'ok');
        nextBtn.hidden = currentIndex === STEP_DEFS.length - 1;

        if (step.id === 'crafty') {
          getCreds();
        }
      } else {
        stateEl.textContent = 'failed — try again';
        stateEl.className = 'step-state';
        logLine('>> step exited with code ' + data.exit_code, 'err');
      }
      renderRail();
    }
  };

  source.onerror = () => {
    logLine('>> connection lost.', 'err');
    running = false;
    box.classList.remove('running');
    runBtn.disabled = false;
    source.close();
  };
}

function goNextStep() {
  // Defense in depth: the button is already hidden via [hidden] while the
  // current step isn't done, but never advance on a stray click either.
  if (!doneSteps.has(STEP_DEFS[currentIndex].id)) return;
  if (currentIndex < STEP_DEFS.length - 1) {
    currentIndex++;
    renderActiveStep();
    renderRail();
  }
}

// ---------------- Tabs ----------------

function moveTabIndicator() {
  const indicator = document.getElementById('tab-indicator-installer');
  const activeBtn = document.querySelector('#tabs-installer .tab-btn.active');
  if (!indicator || !activeBtn) return;
  indicator.style.width = activeBtn.offsetWidth + 'px';
  indicator.style.transform = `translateX(${activeBtn.offsetLeft - 4}px)`;
}

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  moveTabIndicator();
  if (name === 'terminal') checkTerminalStatus();
}
window.addEventListener('load', moveTabIndicator);
window.addEventListener('resize', moveTabIndicator);

// ---------------- Token / status ----------------

function withToken() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  return token ? ('?token=' + encodeURIComponent(token)) : '';
}

let lastStatus = {};

function setLinkDot(id, state) {
  const dot = document.getElementById(id);
  if (!dot) return;
  dot.classList.remove('dot-green', 'dot-red', 'dot-grey');
  dot.classList.add('dot-' + state);
}

function updateTopbarLinks(data) {
  if (!data) return;
  const advanced = isAdvanced();
  const craftyLink = document.getElementById('link-crafty');
  const cockpitLink = document.getElementById('link-cockpit');
  const modLink = document.getElementById('link-mod-manager');
  const serverManagerLink = document.getElementById('link-server-manager');
  const ip = data.local_ip && data.local_ip !== 'unknown' ? data.local_ip.split(' ')[0] : null;

  if (ip) {
    craftyLink.href = `https://${ip}:8443`;
    cockpitLink.href = `https://${ip}:9090`;
    modLink.href = `http://${ip}:5151/`;
    serverManagerLink.href = `http://${ip}:6161/`;
  }
  craftyLink.classList.toggle('disabled', !data.crafty && !advanced);
  cockpitLink.classList.toggle('disabled', !data.cockpit && !advanced);
  const modInfo = data.mod_manager || {};
  modLink.classList.toggle('disabled', !(modInfo.installed && modInfo.running) && !advanced);
  const serverManagerInfo = data.server_manager || {};
  serverManagerLink.classList.toggle('disabled', !(serverManagerInfo.installed && serverManagerInfo.running) && !advanced);

  // Health dots: grey = not installed, red = installed but not reachable right
  // now, green = reachable. Crafty/Cockpit only expose a single "installed and
  // running" boolean today, so those two are grey/green only.
  setLinkDot('dot-crafty', data.crafty ? 'green' : 'grey');
  setLinkDot('dot-cockpit', data.cockpit ? 'green' : 'grey');
  setLinkDot('dot-mod-manager', !modInfo.installed ? 'grey' : (modInfo.running ? 'green' : 'red'));
  setLinkDot('dot-server-manager', !serverManagerInfo.installed ? 'grey' : (serverManagerInfo.running ? 'green' : 'red'));

  const fullCrafty = document.getElementById('link-full-crafty');
  const fullMod = document.getElementById('link-full-mod-manager');
  const fullServer = document.getElementById('link-full-server-manager');
  if (fullCrafty) fullCrafty.textContent = ip ? `https://${ip}:8443` : 'not available yet — install Crafty first';
  if (fullMod) fullMod.textContent = ip ? `http://${ip}:5151/` : 'not available yet';
  if (fullServer) fullServer.textContent = ip ? `http://${ip}:6161/` : 'not available yet';
}

async function refreshStatus() {
  try {
    const res = await fetch('/api/status' + withToken());
    const data = await res.json();
    lastStatus = data;
    ['firewall', 'docker', 'crafty', 'cockpit'].forEach(id => {
      if (data[id]) doneSteps.add(id);
    });
    document.getElementById('local-ip').textContent = data.local_ip || '--';
    document.getElementById('mac-address').textContent = data.mac_address || '--';
    updateTopbarLinks(data);
    // Skip re-rendering the rail/active-step box while a step is actively
    // running: this poll fires every 8s regardless of what's happening on
    // screen, and a component's status can flip to "true" mid-script (e.g.
    // Cockpit's socket comes up almost instantly, long before the rest of
    // that step's work — like installing Explorer — is actually done).
    // Re-rendering here used to stomp the "running" state back to "done"
    // and reveal the "Next" button while the step was still executing,
    // making it look finished when it wasn't. runCurrentStep() already
    // keeps the active-step UI in sync in real time, so it's safe to just
    // skip this while running=true.
    if (!running) {
      renderRail();
      renderActiveStep();
    }
  } catch (e) {
    console.error(e);
  }
}

// ---------------- Crafty creds ----------------

async function getCreds() {
  const box = document.getElementById('creds-box');

  if (!doneSteps.has('crafty')) {
    box.innerHTML = '<p class="creds-warn">Crafty hasn\'t finished installing yet — this will probably come back empty. Trying anyway...</p>';
  } else {
    box.innerHTML = '<p class="dim">Checking Crafty logs...</p>';
  }

  try {
    const res = await fetch('/api/creds' + withToken());
    const data = await res.json();
    if (data.creds) {
      if (data.creds.raw) {
        box.innerHTML = '<pre style="margin:0;color:var(--amber);white-space:pre-wrap;">' +
          escapeHtml(data.creds.raw) + '</pre>';
      } else {
        box.innerHTML =
          '<div class="creds-row"><span class="k">username</span><span class="v">' +
          escapeHtml(data.creds.username) + '</span></div>' +
          '<div class="creds-row"><span class="k">password</span><span class="v">' +
          escapeHtml(data.creds.password) + '</span></div>' +
          '<div class="creds-row"><span class="k">url</span><span class="v">https://' +
          document.getElementById('local-ip').textContent + ':8443</span></div>';
      }
    } else {
      box.innerHTML = '<p class="dim">' + escapeHtml(data.error || 'Not found yet.') + '</p>';
    }
  } catch (e) {
    box.innerHTML = '<p class="dim">Error fetching credentials.</p>';
  }
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ---------------- Preview mode banner ----------------

async function checkPreviewMode() {
  try {
    const res = await fetch('/api/mode' + withToken());
    const data = await res.json();
    if (data.preview) {
      document.getElementById('preview-banner').style.display = 'block';
    }
  } catch (e) {
    // ignore
  }
}

// ---------------- Self-update check (GitHub) ----------------
// Runs once whenever the dashboard is loaded/opened, like Crafty's own
// update notice. Never touches any files by itself — only "Update now"
// does, and even that only runs `git pull` inside the installer's own
// install directory, which holds no server/mod data of any kind.

async function checkSelfUpdate() {
  try {
    const res = await fetch('/api/self_update_check' + withToken());
    const data = await res.json();
    const banner = document.getElementById('update-banner');
    if (!banner) return;
    if (data.update_available) {
      document.getElementById('update-banner-text').textContent =
        `Update available for Anvil Server Installer: ${data.current} → ${data.latest} — "${data.latest_message || ''}"`;
      document.getElementById('update-banner-link').href = data.compare_url;
      banner.style.display = 'flex';
    } else {
      banner.style.display = 'none';
    }
  } catch (e) {
    // ignore — GitHub might be unreachable, that's fine, just skip the notice
  }
}

async function applySelfUpdate() {
  const btn = document.getElementById('update-banner-btn');
  btn.disabled = true;
  btn.textContent = 'Updating...';
  try {
    const res = await fetch('/api/self_update' + withToken(), { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      btn.textContent = 'Restarting service...';
      setTimeout(() => location.reload(), 4000);
    } else {
      btn.disabled = false;
      btn.textContent = 'Update now';
      alert(data.error || 'Update failed.');
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Update now';
  }
}

// ---------------- Terminal ----------------

let term = null;
let termWs = null;

async function checkTerminalStatus() {
  try {
    const res = await fetch('/api/terminal/status' + withToken());
    const data = await res.json();
    if (data.unlocked) {
      document.getElementById('term-user').value = data.username || '';
      startTerminal();
    }
  } catch (e) {
    console.error(e);
  }
}

async function termLogin() {
  const username = document.getElementById('term-user').value;
  const password = document.getElementById('term-pass').value;
  const errEl = document.getElementById('term-error');
  errEl.textContent = '';
  try {
    const res = await fetch('/api/terminal/auth' + withToken(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (data.ok) {
      startTerminal();
    } else {
      errEl.textContent = data.error || 'Authentication failed.';
    }
  } catch (e) {
    errEl.textContent = 'Could not reach the server.';
  }
}

function startTerminal() {
  document.getElementById('term-login').style.display = 'none';
  const container = document.getElementById('term-container');
  container.style.display = 'block';
  container.classList.add('visible');
  document.getElementById('term-logout-wrap').style.display = 'block';

  if (typeof Terminal === 'undefined') {
    container.innerHTML = '<p style="color:var(--text-dim)">Terminal library failed to load. Check your internet connection.</p>';
    return;
  }

  term = new Terminal({
    cursorBlink: true,
    fontFamily: "'JetBrains Mono', monospace",
    fontSize: 13,
    theme: { background: '#060909', foreground: '#d7e6e2', cursor: '#3dffa0' },
  });
  term.open(container);

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  termWs = new WebSocket(proto + '//' + location.host + '/ws/terminal' + withToken());
  termWs.onmessage = (e) => term.write(e.data);
  termWs.onclose = () => term.write('\r\n\x1b[31m[connection closed]\x1b[0m\r\n');
  term.onData((d) => { if (termWs.readyState === 1) termWs.send(JSON.stringify({ input: d })); });
  term.onResize(({ rows, cols }) => {
    if (termWs.readyState === 1) termWs.send(JSON.stringify({ resize: { rows, cols } }));
  });
}

async function termLogout() {
  await fetch('/api/terminal/logout' + withToken(), { method: 'POST' });
  if (termWs) termWs.close();
  location.reload();
}

// ---------------- Danger Zone (uninstall) ----------------

let uninstallRunning = false;

const UNINSTALL_LABELS = {
  docker: 'Docker',
  crafty: 'Crafty Controller',
  cockpit: 'Cockpit + Explorer',
  mod_manager: 'Anvil Mod Manager',
  server_manager: 'Anvil Server Manager',
};

function dangerLogLine(text, cls) {
  const el = document.createElement('div');
  el.className = 'console-line' + (cls ? ' ' + cls : '');
  el.textContent = text;
  const c = document.getElementById('uninstall-console');
  c.classList.add('visible');
  c.appendChild(el);
  c.scrollTop = c.scrollHeight;
}

function setDangerButtonsDisabled(disabled) {
  document.querySelectorAll('.btn-danger').forEach(b => b.disabled = disabled);
}

function buildUninstallUrl(name, purge) {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  const qs = [];
  if (token) qs.push('token=' + encodeURIComponent(token));
  if (purge) qs.push('purge=1');
  return '/api/uninstall/' + name + (qs.length ? '?' + qs.join('&') : '');
}

function runUninstall(name) {
  if (uninstallRunning) return;
  const label = UNINSTALL_LABELS[name] || name;
  const purgeBox = document.getElementById('crafty-purge-data');
  const purge = name === 'crafty' && purgeBox && purgeBox.checked;

  const warning = purge
    ? `This will PERMANENTLY delete ${label} AND all of its data (servers/backups/config). This cannot be undone. Continue?`
    : `Uninstall ${label}? This runs real removal commands on the server.`;
  if (!confirm(warning)) return;

  uninstallRunning = true;
  setDangerButtonsDisabled(true);
  const console_ = document.getElementById('uninstall-console');
  console_.innerHTML = '';
  dangerLogLine('$ uninstalling ' + label, 'dim');

  const source = new EventSource(buildUninstallUrl(name, purge));
  source.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.line !== undefined) dangerLogLine(data.line);
    if (data.done) {
      source.close();
      uninstallRunning = false;
      setDangerButtonsDisabled(false);
      dangerLogLine(
        data.exit_code === 0 ? '>> done.' : '>> exited with code ' + data.exit_code,
        data.exit_code === 0 ? 'ok' : 'err'
      );
      refreshStatus();
    }
  };
  source.onerror = () => {
    dangerLogLine('>> connection lost.', 'err');
    uninstallRunning = false;
    setDangerButtonsDisabled(false);
    source.close();
  };
}

function runUninstallSelf() {
  if (uninstallRunning) return;
  const sure = confirm(
    'This will permanently remove the Anvil Server Installer dashboard from this machine.\n\n' +
    'Docker, Crafty, Cockpit, Anvil Mod Manager, and your Minecraft servers are NOT affected — ' +
    'only this setup wizard goes away, and you will lose access to this page.\n\nContinue?'
  );
  if (!sure) return;
  const typed = prompt('Type UNINSTALL (all caps) to confirm removing this dashboard:');
  if (typed !== 'UNINSTALL') return;

  uninstallRunning = true;
  setDangerButtonsDisabled(true);
  const console_ = document.getElementById('uninstall-console');
  console_.innerHTML = '';
  dangerLogLine('$ removing Anvil Server Installer...', 'dim');

  const source = new EventSource(buildUninstallUrl('installer', false));
  source.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.line !== undefined) dangerLogLine(data.line);
    if (data.done) {
      source.close();
      dangerLogLine('>> This dashboard is shutting itself down now — this page will stop responding.', 'ok');
    }
  };
  source.onerror = () => {
    dangerLogLine('>> Connection closed — the dashboard has likely removed itself.', 'dim');
    source.close();
  };
}

// ---------------- Init ----------------

initAdvancedMode();
renderRail();
renderActiveStep();
checkPreviewMode();
checkSelfUpdate();
refreshStatus();
setInterval(refreshStatus, 8000);
if (window.lucide) lucide.createIcons();
