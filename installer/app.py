#!/usr/bin/env python3
"""
Anvil Server Installer — a click-through setup dashboard for a Minecraft Java + Bedrock
server on Ubuntu, alongside Crafty Controller and Cockpit.

Runs as root (via systemd) so it can execute the real setup commands.
Protected by a token generated at install time (see install.sh).

GitHub: https://github.com/TomCodes-sys/Anvil-Server-Installer
Companion app: https://github.com/TomCodes-sys/Anvil-Mod-Manager
"""

import json
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from functools import wraps

from flask import Flask, Response, jsonify, render_template, request, session

from sudo_session import clear_session_creds, load_session_creds, save_session_creds

# fcntl/pty/termios are POSIX-only stdlib modules used solely by the real
# SSH-less Terminal tab (a genuine PTY spawned via pty.fork()). That feature
# only ever runs on the real Ubuntu server anyway — but these were imported
# unconditionally at module level, which meant `python app.py` failed to
# even start on Windows (ModuleNotFoundError) before ever reaching PREVIEW
# mode or any other guard. That's what broke "Start Preview Demo.bat":
# the whole process crashed on import, nowhere near the code that actually
# checks PREVIEW. Guarding the import instead lets the rest of the app
# (including the entire preview demo) run fine on Windows; only the real
# Terminal tab becomes unavailable there, which is correct since it needs
# a real Unix PTY regardless.
try:
    import fcntl
    import pty
    import termios
    _PTY_AVAILABLE = True
except ImportError:
    _PTY_AVAILABLE = False

app = Flask(__name__)

TOKEN_PATH = "/etc/anvilmc/token"
LEGACY_TOKEN_PATH = "/etc/anvil-installer/token"  # pre-monorepo installs
SESSION_SECRET_PATH = "/etc/anvilmc/session_secret_installer"


def get_or_create_secret_key(path):
    """A secret_key generated fresh every process start means a plain
    `systemctl restart` (e.g. after the self-updater, or just a reboot)
    silently logs everyone out and forces the token/PAM sign-in again —
    surprising when nothing about the token itself changed. Persisting it
    to disk (root-only, same trust tier as the token file) makes sessions
    survive restarts, and only ever changes if this file is deleted."""
    try:
        with open(path) as f:
            existing = f.read().strip()
            if existing:
                return existing
    except FileNotFoundError:
        pass
    new_secret = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(new_secret)
        os.chmod(path, 0o600)
    except OSError:
        pass  # e.g. preview mode on Windows with no write access to /etc — fall back to in-memory only
    return new_secret


app.secret_key = get_or_create_secret_key(SESSION_SECRET_PATH)

# 30-minute idle timeout, same reasoning as Anvil Server Manager: sliding
# expiration via Flask's default SESSION_REFRESH_EACH_REQUEST, so it's idle
# time, not a hard cutoff — resets on every request.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)

# Preview mode: no real ufw/apt/docker/systemctl commands are ever run.
PREVIEW = os.environ.get("ANVIL_INSTALLER_PREVIEW", "0") == "1"

_preview_state = {"firewall": False, "docker": False, "crafty": False, "cockpit": False,
                   "ssh": True, "mod_manager": False, "server_manager": False}

# Set by the firewall step if it just installed PAM support for the first
# time — api_run() checks this after the step finishes and restarts the
# service so the Terminal tab works without a manual re-run of install.sh.
# (PAM login no longer needs an install step or a restart to pick it up —
# see the ctypes-based _pam_authenticate() below for why.)

# ---------------------------------------------------------------------------
# Self-update (GitHub) + Mod Manager companion app
# ---------------------------------------------------------------------------
GITHUB_REPO = "TomCodes-sys/Anvil-MC"
INSTALL_DIR = "/opt/anvilmc/installer"
MONOREPO_DIR = "/opt/anvilmc"  # the single shared clone all three apps live inside

MOD_MANAGER_DIR = "/opt/anvilmc/mod-manager"
MOD_MANAGER_SERVICE = "anvil-mod-manager"
MOD_MANAGER_PORT = 5151

SERVER_MANAGER_DIR = "/opt/anvilmc/server-manager"
SERVER_MANAGER_SERVICE = "anvil-server-manager"
SERVER_MANAGER_PORT = 6161

# PAM login (used both by the Terminal tab and as a fallback dashboard
# sign-in when the token's lost). Binds directly to libpam via ctypes
# instead of the `python-pam` pip package — that package needs to compile a
# C extension against libpam headers (gcc + python3-dev + libpam0g-dev), and
# in practice this kept failing or ending up half-installed. libpam.so.0
# itself is a base-system library present on every real Ubuntu install
# already (PAM is how `login`/`su`/`sudo` themselves authenticate), so this
# needs nothing extra installed at all.
#
# It also authenticates against a dedicated "anvil-auth" PAM service
# (written to /etc/pam.d/anvil-auth on first use, root-only) instead of the
# generic "login" service — "login" pulls in pam_securetty/pam_nologin/
# pam_lastlog, which assume a real interactive TTY and session, and reliably
# fail authentication from a non-interactive process like this one even with
# a correct password. A minimal service that just includes common-auth/
# common-account (the same building blocks Debian/Ubuntu's own "passwd" and
# "sudo" services use) is exactly the same idea as Cockpit's own dedicated
# PAM service file, and sidesteps that whole class of failure.
import ctypes
import ctypes.util

_PAM_SERVICE_NAME = "anvilmc-auth"
_PAM_SERVICE_PATH = f"/etc/pam.d/{_PAM_SERVICE_NAME}"
_PAM_SERVICE_CONTENTS = """# Managed by Anvil — minimal PAM service for the dashboard's username/
# password sign-in fallback. Deliberately skips login-specific checks
# (pam_nologin, pam_securetty, pam_lastlog) that assume a real interactive
# terminal session and otherwise cause a correct password to still fail.
auth    include common-auth
account include common-account
"""


def _ensure_pam_service_file():
    try:
        if not os.path.exists(_PAM_SERVICE_PATH):
            with open(_PAM_SERVICE_PATH, "w") as f:
                f.write(_PAM_SERVICE_CONTENTS)
            os.chmod(_PAM_SERVICE_PATH, 0o644)
    except OSError:
        pass  # e.g. no /etc/pam.d on this machine at all (Windows preview) — PAM login just won't be offered


_libpam = None
_PAM_UNAVAILABLE_REASON = None
try:
    _libpam_path = ctypes.util.find_library("pam")
    if not _libpam_path:
        raise OSError("libpam not found on this system")
    _libpam = ctypes.CDLL(_libpam_path, use_errno=True)

    class _PamMessage(ctypes.Structure):
        _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]

    class _PamResponse(ctypes.Structure):
        _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]

    _PAM_CONV_FUNC = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.POINTER(_PamMessage)),
        ctypes.POINTER(ctypes.POINTER(_PamResponse)), ctypes.c_void_p)

    class _PamConv(ctypes.Structure):
        _fields_ = [("conv", _PAM_CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]

    _libpam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(_PamConv), ctypes.POINTER(ctypes.c_void_p)]
    _libpam.pam_start.restype = ctypes.c_int
    _libpam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpam.pam_authenticate.restype = ctypes.c_int
    _libpam.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpam.pam_acct_mgmt.restype = ctypes.c_int
    _libpam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpam.pam_end.restype = ctypes.c_int
except OSError as e:
    _libpam = None
    _PAM_UNAVAILABLE_REASON = str(e)

_PAM = _libpam is not None  # replaces the old "_pam is not None" check used throughout


def _pam_authenticate(username, password, service=_PAM_SERVICE_NAME):
    if _libpam is None:
        return False
    _ensure_pam_service_file()
    password_bytes = password.encode("utf-8")
    PAM_PROMPT_ECHO_OFF = 1
    PAM_SUCCESS = 0

    def _conv(n_messages, messages, p_response, app_data):
        addr = _libpam.calloc(n_messages, ctypes.sizeof(_PamResponse))
        p_response[0] = ctypes.cast(addr, ctypes.POINTER(_PamResponse))
        for i in range(n_messages):
            if messages[i].contents.msg_style == PAM_PROMPT_ECHO_OFF:
                dst = _libpam.calloc(len(password_bytes) + 1, 1)
                ctypes.memmove(dst, password_bytes, len(password_bytes))
                p_response[0][i].resp = ctypes.cast(dst, ctypes.c_char_p)
                p_response[0][i].resp_retcode = 0
        return 0

    handle = ctypes.c_void_p()
    conv = _PamConv(_PAM_CONV_FUNC(_conv), None)
    retval = _libpam.pam_start(service.encode("utf-8"), username.encode("utf-8"), ctypes.byref(conv), ctypes.byref(handle))
    if retval != PAM_SUCCESS:
        return False
    try:
        if _libpam.pam_authenticate(handle, 0) != PAM_SUCCESS:
            return False
        return _libpam.pam_acct_mgmt(handle, 0) == PAM_SUCCESS
    except Exception:
        return False
    finally:
        _libpam.pam_end(handle, retval)

try:
    from flask_sock import Sock
    sock = Sock(app)
except ImportError:
    Sock = None
    sock = None


def get_token():
    for path in (TOKEN_PATH, LEGACY_TOKEN_PATH):
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

# Shown instead of a bare 401 when someone lands on the dashboard without a
# valid token — gives them two ways back in instead of a dead end: paste the
# token (with a reminder of exactly how to go get it over SSH), or log in
# with the same username/password they use to SSH into the box (checked via
# PAM, same mechanism the Terminal tab already uses).
UNAUTHORIZED_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anvil Server Installer &mdash; sign in</title>
<style>
  body {{ background:#0f1512; color:#e7ede9; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:20px; }}
  .box {{ background:#161d19; border:1px solid #26332c; border-radius:14px; padding:28px; width:100%; max-width:420px; }}
  h1 {{ font-size:19px; margin:0 0 4px; color:#7fe0a0; }}
  p.sub {{ color:#9db3a8; font-size:13.5px; margin:0 0 20px; }}
  .tabs {{ display:flex; gap:6px; margin-bottom:16px; }}
  .tabs button {{ flex:1; background:#1d2621; color:#cfe0d6; border:1px solid #2b3a32; border-radius:8px;
                  padding:8px; cursor:pointer; font-size:13px; }}
  .tabs button.active {{ background:#234a34; border-color:#3f9e64; color:#e9fff0; }}
  label {{ display:block; font-size:12.5px; color:#9db3a8; margin:10px 0 4px; }}
  input {{ width:100%; box-sizing:border-box; background:#0f1512; border:1px solid #2b3a32; border-radius:8px;
           color:#e7ede9; padding:9px 10px; font-size:14px; }}
  button.go {{ margin-top:16px; width:100%; background:#3f9e64; color:#04140a; border:none; border-radius:8px;
               padding:10px; font-weight:600; cursor:pointer; font-size:14px; transition:transform .12s ease, background .15s ease; }}
  button.go:hover {{ background:#4bb875; transform:translateY(-1px); }}
  button.go:active {{ transform:translateY(0) scale(.98); }}
  .err {{ color:#ff9d8a; font-size:12.5px; margin-top:10px; min-height:14px; }}
  .hint {{ color:#7f9689; font-size:12px; margin-top:18px; line-height:1.5; }}
  code {{ background:#0f1512; border:1px solid #26332c; border-radius:5px; padding:1px 5px; }}
  .pane {{ display:none; }} .pane.active {{ display:block; }}
</style></head>
<body>
  <div class="box">
    <h1>Unauthorized</h1>
    <p class="sub">This dashboard needs a token or a server login to continue.</p>
    <div class="tabs">
      <button id="tab-token" class="active" onclick="showPane('token')">I have my token</button>
      <button id="tab-pam" onclick="showPane('pam')">Use my server login</button>
    </div>
    <div id="pane-token" class="pane active">
      <label>Token</label>
      <input id="in-token" type="text" placeholder="paste the token from install.sh" autofocus>
      <button class="go" onclick="submitToken()">Unlock</button>
    </div>
    <div id="pane-pam" class="pane">
      <label>Username</label>
      <input id="in-user" type="text" placeholder="the Linux user you SSH in as">
      <label>Password</label>
      <input id="in-pass" type="password">
      <button class="go" onclick="submitPam()">Unlock</button>
    </div>
    <p class="err" id="err"></p>
    <p class="hint">Lost the token? SSH into the server and run <code>sudo /etc/anvilmc/get-link.sh</code>
      to print the links for all three AnvilMC dashboards again (or <code>sudo cat /etc/anvilmc/token</code> for just the token).
      {pam_hint}</p>
  </div>
<script>
function showPane(which) {{
  document.getElementById('pane-token').classList.toggle('active', which === 'token');
  document.getElementById('pane-pam').classList.toggle('active', which === 'pam');
  document.getElementById('tab-token').classList.toggle('active', which === 'token');
  document.getElementById('tab-pam').classList.toggle('active', which === 'pam');
  document.getElementById('err').textContent = '';
}}
async function post(body) {{
  const res = await fetch('/api/auth/login', {{ method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(body) }});
  const data = await res.json();
  if (data.ok) {{ window.location.href = '/'; }}
  else {{ document.getElementById('err').textContent = data.error || 'Unauthorized.'; }}
}}
function submitToken() {{ post({{ mode: 'token', token: document.getElementById('in-token').value }}); }}
function submitPam() {{ post({{ mode: 'pam', username: document.getElementById('in-user').value, password: document.getElementById('in-pass').value }}); }}
</script>
</body></html>"""

FIREWALL_CONFIRM_FILE = "/etc/anvilmc/firewall_confirmed"


def _confirm_firewall_reachable():
    """Called on every successful authenticated request. If the firewall
    step's 3-minute auto-revert timer is currently armed, this cancels it —
    reaching this code at all proves the dashboard is reachable again after
    a fresh connection (e.g. a page reload), which is exactly what the
    safety net is waiting to see."""
    if PREVIEW:
        return
    try:
        os.makedirs(os.path.dirname(FIREWALL_CONFIRM_FILE), exist_ok=True)
        open(FIREWALL_CONFIRM_FILE, "a").close()
    except Exception:
        pass


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if PREVIEW:
            return f(*args, **kwargs)
        token = get_token()
        if token is None:
            _confirm_firewall_reachable()
            return f(*args, **kwargs)
        if session.get("authed"):
            _confirm_firewall_reachable()
            return f(*args, **kwargs)
        supplied = request.args.get("token") or request.headers.get("X-Dashboard-Token")
        if supplied and secrets.compare_digest(supplied, token):
            session["authed"] = True
            session.permanent = True  # start the 30-minute idle timeout
            _confirm_firewall_reachable()
            return f(*args, **kwargs)
        pam_hint = ("No token handy at all? Use the \"Use my server login\" tab to sign in with your "
                    "Linux username/password instead." if _PAM else
                    "This server's PAM library couldn't be loaded, so username/password sign-in isn't "
                    "available here — you'll need the token.")
        return Response(UNAUTHORIZED_PAGE.format(pam_hint=pam_hint), status=401, mimetype="text/html")
    return wrapped


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Alternate way into the dashboard when the token's been lost, used by
    the login page require_auth() serves on a 401. Two modes:
      "token" — same check require_auth does, just via POST instead of ?token=
      "pam"   — falls back to the box's own Linux login (checked via PAM),
                for when nobody can find the token file at all.
    Either way, success just sets the same session["authed"] flag require_auth
    already understands — no new trust model, just a second door into it."""
    if PREVIEW:
        return jsonify({"ok": True})
    body = request.get_json(force=True) or {}
    mode = body.get("mode")
    if mode == "token":
        expected = get_token()
        supplied = (body.get("token") or "").strip()
        if expected and supplied and secrets.compare_digest(supplied, expected):
            session["authed"] = True
            session.permanent = True  # start the 30-minute idle timeout
            _confirm_firewall_reachable()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "That token doesn't match."}), 401
    elif mode == "pam":
        if not _PAM:
            return jsonify({"ok": False, "error": "PAM isn't available on this server (couldn't load libpam)."}), 500
        username, password = body.get("username", ""), body.get("password", "")
        if not username or not password:
            return jsonify({"ok": False, "error": "Username and password required."}), 400
        if verify_credentials(username, password):
            session["authed"] = True
            session.permanent = True  # start the 30-minute idle timeout
            _confirm_firewall_reachable()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Invalid username or password."}), 401
    return jsonify({"ok": False, "error": "Unknown login mode."}), 400


def verify_credentials(username, password):
    return _pam_authenticate(username, password)


# ---------------------------------------------------------------------------
# Step definitions — each is a fixed, predefined shell script.
# The dashboard never executes arbitrary user-supplied commands.
# ---------------------------------------------------------------------------

# Prepended to every script that touches apt/dpkg. Ubuntu Server runs
# unattended-upgrades right after boot, which can be mid-`apt-get` the moment
# our own bootstrap fires — that's the "Could not get lock
# /var/lib/dpkg/lock-frontend" error. apt_get_retry() just retries the exact
# same command with backoff instead of dying on the first collision.
APT_RETRY_HELPER = r"""
apt_get_retry() {
  local n=0
  local max=30
  until DEBIAN_FRONTEND=noninteractive "$@"; do
    n=$((n+1))
    if [ "$n" -ge "$max" ]; then
      echo ">> apt/dpkg is still locked after $((max*10))s — another process (unattended-upgrades?) won't let go. Giving up."
      return 1
    fi
    echo ">> apt/dpkg is locked by another process (likely automatic updates running on first boot) — waiting 10s and retrying ($n/$max)..."
    sleep 10
  done
}
"""

FIREWALL_SCRIPT = APT_RETRY_HELPER + r"""
set -e
echo ">> Updating package lists..."
apt_get_retry apt-get update -qq
echo ">> Upgrading installed packages (this can take a few minutes)..."
apt_get_retry apt-get upgrade -y -qq
echo ">> Preventing sleep on lid close, and making the power button a clean shutdown (server stays up unless the power button is pressed or a shutdown command is run)..."
sed -i \
  -e 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' \
  -e 's/^#\?HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' \
  -e 's/^#\?HandleLidSwitchDocked=.*/HandleLidSwitchDocked=ignore/' \
  -e 's/^#\?HandlePowerKey=.*/HandlePowerKey=poweroff/' \
  -e 's/^#\?HandlePowerKeyLongPress=.*/HandlePowerKeyLongPress=poweroff/' \
  /etc/systemd/logind.conf
if ! grep -q "^HandleLidSwitch=" /etc/systemd/logind.conf; then
  echo "HandleLidSwitch=ignore" >> /etc/systemd/logind.conf
fi
if ! grep -q "^HandlePowerKey=" /etc/systemd/logind.conf; then
  echo "HandlePowerKey=poweroff" >> /etc/systemd/logind.conf
fi
if ! grep -q "^HandlePowerKeyLongPress=" /etc/systemd/logind.conf; then
  echo "HandlePowerKeyLongPress=poweroff" >> /etc/systemd/logind.conf
fi
systemctl restart systemd-logind
# Note: this deliberately makes the physical power button the ONE physical
# way to shut this box down (a clean poweroff, not a suspend) — lid close is
# ignored above, so besides this button, shutting down otherwise means
# running an actual shutdown/reboot command (Terminal tab or SSH).
echo ">> Making sure SSH is enabled and running FIRST, so a firewall reset can't lock you out..."
systemctl enable ssh
systemctl start ssh
echo ">> Allowing THIS dashboard's own port (8090/tcp) — otherwise the firewall would lock you out of the installer itself..."
ufw allow 8090/tcp
echo ">> Allowing SSH (22/tcp)..."
ufw allow 22/tcp
echo ">> Allowing Cockpit (9090/tcp)..."
ufw allow 9090/tcp
echo ">> Allowing Crafty Controller web UI (8443/tcp)..."
ufw allow 8443/tcp
echo ">> Allowing Anvil Mod Manager (5151/tcp)..."
ufw allow 5151/tcp
echo ">> Allowing Anvil Server Manager (6161/tcp)..."
ufw allow 6161/tcp
echo ">> Allowing Minecraft Java Edition server ports (25565-25600/tcp) — Crafty runs its servers on the host network directly, so this whole range is opened up front instead of one port at a time..."
ufw allow 25565:25600/tcp
echo ">> Allowing Minecraft Bedrock Edition (19132/udp)..."
ufw allow 19132/udp
echo ">> Allowing additional Bedrock/companion server ports (19134/udp, 19135/udp)..."
ufw allow 19134/udp
ufw allow 19135/udp
echo ">> Allowing Simple Voice Chat (24454/udp)..."
ufw allow 24454/udp
echo ">> Arming a 3-minute safety net: if this dashboard isn't reached again within 3 minutes, this firewall change reverts itself automatically — no reboot needed..."
mkdir -p /etc/anvilmc
rm -f /etc/anvilmc/firewall_confirmed
nohup bash -c '
  sleep 180
  if [ ! -f /etc/anvilmc/firewall_confirmed ]; then
    ufw disable
    echo "$(date): auto-reverted firewall — dashboard was not reached again within 3 minutes" >> /etc/anvilmc/firewall_autorevert.log
  fi
' >/dev/null 2>&1 &
disown
echo ">> Enabling firewall (non-interactive)..."
ufw --force enable
ufw reload
echo ">> Confirming SSH is still allowed and running..."
systemctl is-active ssh
ufw status verbose
echo ">> Firewall configured. Reload this dashboard within 3 minutes to confirm access — doing so automatically cancels the safety-net revert above. If you don't reload in time, the firewall disables itself again on its own, no reboot required."
"""

COCKPIT_SCRIPT = APT_RETRY_HELPER + r"""
set -e
echo ">> Installing Cockpit..."
apt_get_retry apt-get update -qq
apt_get_retry apt-get install -y -qq cockpit cockpit-storaged
systemctl enable --now cockpit.socket
echo ">> Cockpit installed and running on port 9090."

# Cockpit Explorer (package: cockpit-files) is the official cockpit-project
# file manager and the actively maintained successor to the old 45Drives
# "Navigator" plugin, which is no longer developed. Explorer is bundled in
# here — not a separate optional step — so every Cockpit install always
# comes with a working file manager out of the box.
if dpkg -s cockpit-navigator >/dev/null 2>&1; then
  echo ">> Removing the old Cockpit Navigator (unmaintained, replaced by Explorer)..."
  apt-get purge -y -qq cockpit-navigator 2>/dev/null || true
fi

echo ">> Installing Cockpit Explorer (web-based file manager)..."
if dpkg -s cockpit-files >/dev/null 2>&1; then
  echo ">> Cockpit Explorer is already installed."
elif apt_get_retry apt-get install -y -qq cockpit-files; then
  echo ">> Cockpit Explorer installed from apt."
else
  echo ">> Not available directly from apt on this Ubuntu release — trying the backports pocket..."
  . /etc/os-release
  if apt_get_retry apt-get install -y -qq -t "${VERSION_CODENAME}-backports" cockpit-files 2>/dev/null; then
    echo ">> Cockpit Explorer installed from ${VERSION_CODENAME}-backports."
  else
    echo ">> Not in backports either — fetching the latest release straight from GitHub instead..."
    apt_get_retry apt-get install -y -qq curl tar
    ASSET_URL=$(curl -fsSL https://api.github.com/repos/cockpit-project/cockpit-files/releases/latest \
      | grep -oE '"browser_download_url": *"[^"]+cockpit-files-[0-9]+\.tar\.xz"' \
      | head -n1 | cut -d'"' -f4)
    if [ -n "$ASSET_URL" ]; then
      echo ">> Downloading $ASSET_URL ..."
      rm -rf /tmp/cockpit-files-dl
      mkdir -p /tmp/cockpit-files-dl
      curl -fsSL "$ASSET_URL" -o /tmp/cockpit-files-dl/cockpit-files.tar.xz
      tar -xJf /tmp/cockpit-files-dl/cockpit-files.tar.xz -C /tmp/cockpit-files-dl
      rm -rf /usr/share/cockpit/files
      mkdir -p /usr/share/cockpit/files
      cp -r /tmp/cockpit-files-dl/*/dist/. /usr/share/cockpit/files/
      rm -rf /tmp/cockpit-files-dl
      echo ">> Cockpit Explorer installed from the official GitHub release."
    else
      echo ">> Couldn't find a Cockpit Explorer release to install for this system — Cockpit itself is still installed and usable, just without a file-manager tab."
    fi
  fi
fi

echo ">> Restarting Cockpit so Explorer shows up immediately..."
systemctl restart cockpit.socket 2>/dev/null || true
systemctl try-restart cockpit.service 2>/dev/null || true
echo ">> Done. Log in with your normal Ubuntu username and password, and look for 'Explorer' in the sidebar (hard-refresh with Ctrl+Shift+R if your browser cached the old sidebar)."
"""

DOCKER_SCRIPT = APT_RETRY_HELPER + r"""
set -e
if command -v docker &> /dev/null; then
  echo ">> Docker is already installed."
else
  echo ">> Making sure the package manager isn't locked before installing Docker..."
  apt_get_retry apt-get update -qq
  echo ">> Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
  echo ">> Docker installed."
fi
docker --version
"""

CRAFTY_SCRIPT = r"""
set -e
echo ">> Making sure the Docker daemon is ready..."
docker_ready=false
for i in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then
    docker_ready=true
    break
  fi
  echo ">> Waiting for Docker to be ready ($i/30)..."
  sleep 2
done
if [ "$docker_ready" != "true" ]; then
  echo ">> Docker never became ready. Run the 'Install Docker' step (again if needed), then retry this step."
  exit 1
fi

mkdir -p /opt/crafty/backups /opt/crafty/logs /opt/crafty/servers /opt/crafty/config /opt/crafty/import
docker rm -f crafty 2>/dev/null || true

echo ">> Pulling the Crafty Controller image (first run can take a few minutes)..."
docker pull registry.gitlab.com/crafty-controller/crafty-4:latest

echo ">> Starting Crafty Controller..."
# IMPORTANT: uses --network host instead of individually mapped -p ports.
# Crafty lets you run a Minecraft server on ANY port via server.properties,
# and with individually mapped ports, any port outside whatever narrow range
# was published (e.g. only 25565-25570) is unreachable from outside the
# container even though Crafty itself shows the server as running fine —
# this was the exact cause of "I changed the port and now I can't connect."
# --network host removes that whole class of bug: whatever port Crafty (or
# you) picks is immediately reachable, exactly like a normal bare-metal
# Crafty install, with no port list to keep in sync. The tradeoff (no
# container network isolation) is standard practice for Crafty's Docker
# deployment and is why the firewall step opens the actual ports directly
# on the host instead of relying on Docker's port publishing.
docker run -d --name crafty \
  --network host \
  -v /opt/crafty/backups:/crafty/backups \
  -v /opt/crafty/logs:/crafty/logs \
  -v /opt/crafty/servers:/crafty/servers \
  -v /opt/crafty/config:/crafty/app/config \
  -v /opt/crafty/import:/crafty/import \
  --restart unless-stopped \
  registry.gitlab.com/crafty-controller/crafty-4:latest

echo ">> Waiting for the container to report as running..."
container_up=false
for i in $(seq 1 30); do
  state=$(docker inspect -f '{{.State.Running}}' crafty 2>/dev/null || echo false)
  if [ "$state" = "true" ]; then
    container_up=true
    break
  fi
  sleep 2
done
if [ "$container_up" != "true" ]; then
  echo ">> Crafty's container isn't staying up. Recent logs:"
  docker logs --tail 40 crafty || true
  exit 1
fi
echo ">> Container is running."

echo ">> Waiting for Crafty's web UI to answer on port 8443 (first boot generates a TLS cert and can take 1-2 minutes)..."
web_up=false
for i in $(seq 1 90); do
  code=$(curl -k -s -o /dev/null -w '%{http_code}' --max-time 3 https://localhost:8443 2>/dev/null || echo 000)
  if [ "$code" != "000" ]; then
    web_up=true
    break
  fi
  sleep 2
done
if [ "$web_up" != "true" ]; then
  echo ">> Crafty's web UI never answered on port 8443 after 3 minutes. Recent logs:"
  docker logs --tail 60 crafty || true
  echo ">> This usually means the container crashed or is still mid-boot. Check the logs above, then retry this step."
  exit 1
fi

SERVER_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
if [ -z "$SERVER_IP" ]; then
  SERVER_IP=$(hostname -I | awk '{print $1}')
fi
echo ">> Crafty is up. Web UI: https://$SERVER_IP:8443"
echo ">> Note: Crafty uses a self-signed certificate, so your browser WILL show a security warning the first time you open it — this is expected. Click 'Advanced' -> 'Proceed anyway' (wording differs per browser) to reach the login page. If the page still looks totally blank/unreachable rather than showing a cert warning, that's a real problem — re-run this step and check the logs above."
echo ">> Use the 'Get Crafty login' button to see the generated admin password (give it a few more seconds if it's not there yet)."
echo ">> Heads up: the web UI answering doesn't mean Crafty's background task queue is fully warmed up yet — the"
echo "   first thing you try in there (e.g. Import Server) can sit looking stuck for a minute or two while that"
echo "   finishes starting. If that happens, just wait a bit and retry rather than assuming it failed."
"""

MOD_MANAGER_SCRIPT = APT_RETRY_HELPER + r"""
set -e

# In the AnvilMC monorepo, Mod Manager's source already sits right next to
# this dashboard's own — no separate download needed, just set up its venv
# and systemd service. INSTALL_USER matches whoever this Installer itself is
# running as context for (falls back to the invoking user if unset).
SOURCE_DIR=/opt/anvilmc/mod-manager
if [ ! -f "$SOURCE_DIR/app.py" ]; then
  echo ">> Couldn't find Anvil Mod Manager's source at $SOURCE_DIR — this AnvilMC checkout looks incomplete."
  exit 1
fi

INSTALL_USER="${SUDO_USER:-$(logname 2>/dev/null || whoami)}"
[ "$INSTALL_USER" = "root" ] && INSTALL_USER="root"

mkdir -p "$SOURCE_DIR/data"
chown -R "$INSTALL_USER":"$INSTALL_USER" "$SOURCE_DIR"

echo ">> Setting up Anvil Mod Manager's Python environment..."
python3 -m venv "$SOURCE_DIR/venv"
"$SOURCE_DIR/venv/bin/pip" install -q -r "$SOURCE_DIR/requirements.txt"

echo ">> Installing the systemd service (runs as '$INSTALL_USER')..."
sed "s/__INSTALL_USER__/$INSTALL_USER/" "$SOURCE_DIR/anvil-mod-manager.service" > /etc/systemd/system/anvil-mod-manager.service
systemctl daemon-reload
systemctl enable anvil-mod-manager > /dev/null 2>&1

if [ "$INSTALL_USER" != "root" ]; then
  SYSTEMCTL_PATH="$(command -v systemctl)"
  echo "$INSTALL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart anvil-mod-manager" > /etc/sudoers.d/anvil-mod-manager-restart
  chmod 440 /etc/sudoers.d/anvil-mod-manager-restart
  visudo -c -f /etc/sudoers.d/anvil-mod-manager-restart > /dev/null || \
    echo "  WARNING: the generated sudoers rule failed validation — Mod Manager's restart-on-update may not work."
fi

systemctl restart anvil-mod-manager
sleep 2
echo ">> Anvil Mod Manager is installed and running on port 5151, sharing this dashboard's login."
SERVER_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
if [ -z "$SERVER_IP" ]; then
  SERVER_IP=$(hostname -I | awk '{print $1}')
fi
echo ">> Open it at: http://$SERVER_IP:5151/ — same token, no separate sign-in."
"""

SERVER_MANAGER_SCRIPT = APT_RETRY_HELPER + r"""
set -e

# Same story as Mod Manager above: source already sits in this AnvilMC
# checkout, so this step only ever sets up the venv + systemd service.
SOURCE_DIR=/opt/anvilmc/server-manager
if [ ! -f "$SOURCE_DIR/app.py" ]; then
  echo ">> Couldn't find Anvil Server Manager's source at $SOURCE_DIR — this AnvilMC checkout looks incomplete."
  exit 1
fi

mkdir -p "$SOURCE_DIR/data"

echo ">> Installing prerequisites (restic, smartmontools, lm-sensors)..."
apt_get_retry apt-get update -qq
apt_get_retry apt-get install -y -qq restic smartmontools lm-sensors

echo ">> Setting up Anvil Server Manager's Python environment..."
python3 -m venv "$SOURCE_DIR/venv"
"$SOURCE_DIR/venv/bin/pip" install -q -r "$SOURCE_DIR/requirements.txt"

echo ">> Installing the systemd service (runs as root — it needs to for apt/docker updates)..."
cp "$SOURCE_DIR/anvil-server-manager.service" /etc/systemd/system/anvil-server-manager.service
systemctl daemon-reload
systemctl enable anvil-server-manager > /dev/null 2>&1
systemctl restart anvil-server-manager
sleep 2
echo ">> Anvil Server Manager is installed and running on port 6161, sharing this dashboard's login."
SERVER_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
if [ -z "$SERVER_IP" ]; then
  SERVER_IP=$(hostname -I | awk '{print $1}')
fi
echo ">> Open it at: http://$SERVER_IP:6161/ — same token, no separate sign-in."
"""

# ---------------------------------------------------------------------------
# Uninstall scripts — Danger Zone. Each is intentionally independent of the
# others (e.g. uninstalling Docker doesn't touch /opt/crafty's actual data;
# uninstalling Crafty leaves Docker itself installed) so a single button only
# does what it says on the tin. `|| true` / `2>/dev/null` throughout because
# these must succeed even if the component was already partially removed or
# never fully installed.
# ---------------------------------------------------------------------------

UNINSTALL_DOCKER_SCRIPT = r"""
set -e
echo ">> Stopping and removing the Crafty container (it depends on Docker)..."
docker rm -f crafty 2>/dev/null || true
echo ">> Uninstalling Docker..."
apt-get purge -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin 2>/dev/null || true
apt-get autoremove -y -qq 2>/dev/null || true
rm -rf /var/lib/docker /var/lib/containerd /etc/docker
echo ">> Docker removed. Crafty's data in /opt/crafty was left untouched — use 'Uninstall Crafty Controller' separately if you want that gone too."
"""

UNINSTALL_CRAFTY_SCRIPT = r"""
set -e
echo ">> Stopping and removing the Crafty container..."
docker rm -f crafty 2>/dev/null || true
docker rmi registry.gitlab.com/crafty-controller/crafty-4:latest 2>/dev/null || true
if [ "$ANVIL_PURGE_DATA" = "1" ]; then
  echo ">> Deleting all Crafty data (servers, backups, config) — this cannot be undone..."
  rm -rf /opt/crafty
else
  echo ">> Crafty's data in /opt/crafty (servers, backups, config) was left in place. Delete it by hand later if you don't need it."
fi
echo ">> Crafty removed."
"""

UNINSTALL_COCKPIT_SCRIPT = r"""
set -e
echo ">> Removing Cockpit and Cockpit Explorer..."
systemctl disable --now cockpit.socket 2>/dev/null || true
apt-get purge -y -qq cockpit cockpit-files cockpit-navigator cockpit-storaged cockpit-bridge cockpit-ws cockpit-system 2>/dev/null || true
rm -rf /usr/share/cockpit/files
apt-get autoremove -y -qq 2>/dev/null || true
echo ">> Cockpit and Explorer removed."
"""

UNINSTALL_MOD_MANAGER_SCRIPT = r"""
set -e
echo ">> Stopping Anvil Mod Manager..."
systemctl disable --now anvil-mod-manager 2>/dev/null || true
rm -f /etc/systemd/system/anvil-mod-manager.service /etc/sudoers.d/anvil-mod-manager-restart
systemctl daemon-reload
# Its own source lives inside this shared AnvilMC checkout now, not a
# separate clone — only the venv and its tracked-server data are removed, so
# reinstalling later from the wizard is instant (no re-download) rather than
# wiping the code out from under its sibling apps.
rm -rf /opt/anvilmc/mod-manager/venv /opt/anvilmc/mod-manager/data
echo ">> Anvil Mod Manager removed (its source stays put as part of the shared AnvilMC checkout — reinstalling from this wizard is instant)."
"""

UNINSTALL_SERVER_MANAGER_SCRIPT = r"""
set -e
echo ">> Stopping Anvil Server Manager..."
systemctl disable --now anvil-server-manager 2>/dev/null || true
rm -f /etc/systemd/system/anvil-server-manager.service
systemctl daemon-reload
rm -rf /opt/anvilmc/server-manager/venv /opt/anvilmc/server-manager/data
echo ">> Anvil Server Manager removed (its source stays put as part of the shared AnvilMC checkout — reinstalling from this wizard is instant)."
"""

UNINSTALL_SCRIPTS = {
    "docker": UNINSTALL_DOCKER_SCRIPT,
    "crafty": UNINSTALL_CRAFTY_SCRIPT,
    "cockpit": UNINSTALL_COCKPIT_SCRIPT,
    "mod_manager": UNINSTALL_MOD_MANAGER_SCRIPT,
    "server_manager": UNINSTALL_SERVER_MANAGER_SCRIPT,
}

UNINSTALL_PREVIEW_LINES = {
    "docker": [">> [PREVIEW] would remove the Crafty container, then purge Docker."],
    "crafty": [">> [PREVIEW] would remove the Crafty container (data left in place unless purge is checked)."],
    "cockpit": [">> [PREVIEW] would purge Cockpit and Cockpit Explorer."],
    "mod_manager": [">> [PREVIEW] would stop and remove Anvil Mod Manager."],
    "server_manager": [">> [PREVIEW] would stop and remove Anvil Server Manager."],
}


def find_crafty_creds():
    """Best-effort scrape of Crafty's first-boot admin credentials."""
    if PREVIEW:
        if not _preview_state["crafty"]:
            return None, "Install Crafty first, then click below. [PREVIEW]"
        return {"username": "admin", "password": "preview-only-not-real"}, None

    if not shutil.which("docker"):
        return None, "Docker isn't installed yet."

    # Primary source of truth: on a genuinely fresh install, Crafty writes its
    # one-time generated admin password to this file (mounted from the
    # container's /crafty/app/config). This is far more reliable than
    # scraping docker logs — Crafty never actually prints "username: ..." /
    # "password: ..." lines, it just points at this file, so a previous
    # version of this check that regex-matched the raw log text could latch
    # onto unrelated words near "password" (e.g. the phrase "password found")
    # and return garbage, or simply find nothing.
    creds_file = "/opt/crafty/config/default-creds.txt"
    if os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                raw = f.read().strip()
            if raw:
                return {"raw": raw}, None
        except Exception:
            pass

    try:
        logs = subprocess.run(
            ["docker", "logs", "--tail", "300", "crafty"],
            capture_output=True, text=True, timeout=10
        )
        text = logs.stdout + logs.stderr
    except Exception as e:
        return None, f"Couldn't read Crafty logs: {e}"

    if "Fresh Install Detected" in text:
        return None, (
            "Crafty looks like a fresh install but hasn't written its credentials file yet — "
            "this can take up to a minute after the container starts. Wait a bit and click refresh again."
        )

    # No creds file AND no "fresh install" marker in the logs almost always
    # means this container's /opt/crafty/config was left over from an
    # earlier attempt (e.g. a previous partial/failed setup), so Crafty
    # doesn't consider this a first boot and never regenerates the file —
    # the old generated password (if any) may also have already been
    # rotated out once someone logged in and changed it.
    return None, (
        "No default-creds.txt found, and Crafty's logs don't show a fresh install. This usually means "
        "Crafty (or its /opt/crafty config folder) was already set up before, so it didn't generate a new "
        "password this time. Try logging in with whatever admin username/password was set previously. "
        "If you've genuinely lost it, use 'Uninstall Crafty Controller' in the Danger Zone below with "
        "'also delete data' checked, then reinstall for a clean first-boot password."
    )


STEPS = {
    "firewall": FIREWALL_SCRIPT,
    "cockpit": COCKPIT_SCRIPT,
    "docker": DOCKER_SCRIPT,
    "crafty": CRAFTY_SCRIPT,
    "mod_manager": MOD_MANAGER_SCRIPT,
    "server_manager": SERVER_MANAGER_SCRIPT,
}

# Steps here are optional — they don't lock/gate the rest of the wizard.
OPTIONAL_STEPS = {"mod_manager", "server_manager"}

PREVIEW_LINES = {
    "firewall": [
        ">> Updating package lists...",
        ">> Upgrading installed packages (this can take a few minutes)...",
        ">> Preventing sleep on lid close...",
        ">> Making sure SSH is enabled and running FIRST, so a firewall reset can't lock you out...",
        ">> Allowing THIS dashboard's own port (8090/tcp)...",
        ">> Allowing SSH (22/tcp)...",
        ">> Allowing Cockpit (9090/tcp)...",
        ">> Allowing Crafty Controller web UI (8443/tcp)...",
        ">> Allowing Anvil Mod Manager (5151/tcp)...",
        ">> Allowing Anvil Server Manager (6161/tcp)...",
        ">> Allowing Minecraft Java Edition server ports (25565-25600/tcp)...",
        ">> Allowing Minecraft Bedrock Edition (19132/udp)...",
        ">> Allowing additional Bedrock/companion server ports (19134/udp, 19135/udp)...",
        ">> Allowing Simple Voice Chat (24454/udp)...",
        ">> Arming a 3-minute safety-net auto-revert...",
        ">> Enabling firewall (non-interactive)...",
        ">> Confirming SSH is still allowed and running...",
        "active",
        "Status: active",
        ">> Firewall configured successfully. [PREVIEW — no real changes made]",
    ],
    "docker": [
        ">> Installing Docker...",
        ">> [PREVIEW] pretending to run get.docker.com installer...",
        "Docker version 27.0.0, build abc1234 [PREVIEW]",
    ],
    "crafty": [
        ">> Making sure the Docker daemon is ready... [PREVIEW]",
        ">> Pulling the Crafty Controller image... [PREVIEW]",
        ">> Starting Crafty Controller... [PREVIEW]",
        ">> Waiting for the container to report as running... [PREVIEW]",
        ">> Container is running. [PREVIEW]",
        ">> Waiting for Crafty's web UI to finish its first boot... [PREVIEW]",
        ">> Crafty is up. Web UI: https://<your-server-ip>:8443 [PREVIEW]",
    ],
    "cockpit": [
        ">> Installing Cockpit... [PREVIEW]",
        ">> Cockpit installed and running on port 9090. [PREVIEW]",
        ">> Installing Cockpit Explorer (file manager)... [PREVIEW]",
        ">> Cockpit Explorer installed. [PREVIEW]",
        ">> Restarting Cockpit so Explorer shows up... [PREVIEW]",
    ],
    "mod_manager": [
        ">> Installing git (if needed)... [PREVIEW]",
        ">> Cloning Anvil Mod Manager into a staging directory... [PREVIEW]",
        ">> Handing off to Anvil Mod Manager's own installer... [PREVIEW]",
        ">> Anvil Mod Manager is installed and running on port 5151. [PREVIEW]",
    ],
    "server_manager": [
        ">> Installing git (if needed)... [PREVIEW]",
        ">> Cloning Anvil Server Manager into a staging directory... [PREVIEW]",
        ">> Handing off to Anvil Server Manager's own installer... [PREVIEW]",
        ">> Anvil Server Manager is installed and running on port 6161. [PREVIEW]",
    ],
}


# ---------------------------------------------------------------------------
# Status checks
# ---------------------------------------------------------------------------

def check_status():
    if PREVIEW:
        status = dict(_preview_state)
        status["mod_manager"] = detect_mod_manager()
        status["server_manager"] = detect_server_manager()
        status["local_ip"] = "192.168.1.50 (preview)"
        status["mac_address"] = "DE:AD:BE:EF:CA:FE (preview)"
        return status

    status = {}

    try:
        ufw = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        # NOTE: must check "status: active" specifically — "inactive" contains
        # the substring "active", so a plain `"active" in output` check was
        # always true, even on a completely fresh box with ufw off.
        status["firewall"] = "status: active" in ufw.stdout.lower()
    except Exception:
        status["firewall"] = False

    try:
        cockpit = subprocess.run(
            ["systemctl", "is-active", "cockpit.socket"], capture_output=True, text=True, timeout=5
        )
        status["cockpit"] = cockpit.stdout.strip() == "active"
    except Exception:
        status["cockpit"] = False

    status["docker"] = shutil.which("docker") is not None

    try:
        crafty = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "crafty"],
            capture_output=True, text=True, timeout=5
        )
        status["crafty"] = crafty.stdout.strip() == "true"
    except Exception:
        status["crafty"] = False

    try:
        status["ssh"] = subprocess.run(
            ["systemctl", "is-active", "ssh"], capture_output=True, text=True, timeout=5
        ).stdout.strip() == "active"
    except Exception:
        status["ssh"] = False

    status["mod_manager"] = detect_mod_manager()
    status["server_manager"] = detect_server_manager()
    status["local_ip"] = get_local_ip()
    status["mac_address"] = get_mac_address() or "unknown"
    return status


def detect_mod_manager():
    """Is the Anvil Mod Manager companion app installed and running?"""
    if PREVIEW:
        installed = _preview_state["mod_manager"]
        return {"installed": installed, "running": installed}
    installed = os.path.isdir(MOD_MANAGER_DIR)
    running = False
    if installed:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", MOD_MANAGER_SERVICE],
                capture_output=True, text=True, timeout=5,
            )
            running = r.stdout.strip() == "active"
        except Exception:
            running = False
    return {"installed": installed, "running": running}


def detect_server_manager():
    """Is the Anvil Server Manager companion app installed and running?"""
    if PREVIEW:
        installed = _preview_state["server_manager"]
        return {"installed": installed, "running": installed}
    installed = os.path.isdir(SERVER_MANAGER_DIR)
    running = False
    if installed:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", SERVER_MANAGER_SERVICE],
                capture_output=True, text=True, timeout=5,
            )
            running = r.stdout.strip() == "active"
        except Exception:
            running = False
    return {"installed": installed, "running": running}


def get_local_ip():
    """Returns this server's own LAN-facing IP (not the IP of whoever is SSH'd in)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def get_default_iface():
    """Finds the network interface used for the default route (e.g. 'eth0', 'enp3s0')."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.strip().split()
                if len(fields) >= 2 and fields[1] == "00000000":
                    return fields[0]
    except Exception:
        pass
    return None


def get_mac_address():
    """Returns this server's MAC address, for setting up a router DHCP reservation."""
    iface = get_default_iface()
    if not iface:
        return None
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            mac = f.read().strip()
            return mac.upper() if mac else None
    except Exception:
        return None



# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
@require_auth
def index():
    return render_template("index.html")


@app.route("/api/status")
@require_auth
def api_status():
    return jsonify(check_status())


@app.route("/api/creds")
@require_auth
def api_creds():
    creds, error = find_crafty_creds()
    return jsonify({"creds": creds, "error": error})


@app.route("/api/mode")
def api_mode():
    return jsonify({"preview": PREVIEW})


# ---------------------------------------------------------------------------
# Self-update — checks GitHub for a newer commit than what's on disk, and
# notifies on the dashboard (Crafty-style banner). Never touches anything
# outside the install directory itself: `git pull` only updates tracked
# files, and none of this app's own config/token/session files live inside
# INSTALL_DIR, so nothing about the running setup gets disturbed.
# ---------------------------------------------------------------------------

def _git_head(path):
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _github_latest_commit(repo, branch="main"):
    """Best-effort call to the public GitHub API — no auth required, and this
    is read-only (never sends anything back to GitHub)."""
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "anvil-server-installer",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return {
            "sha": data.get("sha"),
            "message": (data.get("commit", {}).get("message", "").splitlines() or [""])[0],
            "date": data.get("commit", {}).get("author", {}).get("date"),
            "html_url": data.get("html_url"),
        }
    except Exception:
        # 404 on 'main' → try 'master' once before giving up.
        if branch == "main":
            return _github_latest_commit(repo, "master")
        return None


@app.route("/api/self_update_check")
@require_auth
def self_update_check():
    if PREVIEW:
        return jsonify({"update_available": False, "preview": True,
                         "note": "Update checks are disabled in preview mode."})
    current = _git_head(INSTALL_DIR)
    latest = _github_latest_commit(GITHUB_REPO)
    if not current or not latest or not latest.get("sha"):
        return jsonify({"update_available": False, "checked": False,
                         "note": "Couldn't determine version info (not a git checkout, or GitHub unreachable)."})
    update_available = not latest["sha"].startswith(current) and current != latest["sha"]
    return jsonify({
        "checked": True,
        "update_available": update_available,
        "current": current[:7],
        "latest": latest["sha"][:7],
        "latest_message": latest.get("message"),
        "repo_url": f"https://github.com/{GITHUB_REPO}",
        "compare_url": f"https://github.com/{GITHUB_REPO}/compare/{current[:12]}...{latest['sha'][:12]}",
    })


@app.route("/api/self_update", methods=["POST"])
@require_auth
def self_update():
    """Pulls the whole AnvilMC monorepo (one clone at MONOREPO_DIR covers all
    three apps) and restarts whichever of the three services are actually
    installed — one update action for the whole ecosystem, since they all
    live in the same checkout now, instead of three separate self-updates
    each only touching their own subfolder. /etc/anvilmc (token, sessions,
    PAM service) lives outside MONOREPO_DIR and is never part of the pull."""
    if PREVIEW:
        return jsonify({"ok": False, "error": "Updating is disabled in preview mode."}), 400
    try:
        # See the note in the Mod Manager code for the full explanation —
        # bootstrap scripts used to instruct `chmod +x` on a file tracked
        # non-executable in git, creating a permanent mode-only diff that
        # blocks `--ff-only` forever with "local changes would be overwritten
        # by merge." Idempotent and safe to run before every pull.
        subprocess.run(["git", "-C", MONOREPO_DIR, "config", "core.fileMode", "false"],
                        capture_output=True, timeout=10)
        r = subprocess.run(["git", "-C", MONOREPO_DIR, "pull", "--ff-only"],
                            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.strip() or r.stdout.strip()}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Restart happens after the response is sent, so the browser gets a
    # clean 200 back before this process (and its own web server) goes down.
    # This whole process already runs as root, so restarting the other two
    # services needs no special sudoers exception (that only exists for Mod
    # Manager's OWN non-root process restarting itself after ITS update
    # button) — just skip either one gracefully if it isn't installed.
    def _restart_later():
        time.sleep(1.0)
        subprocess.run(["systemctl", "restart", "anvil-installer"])
        for service in ("anvil-server-manager", "anvil-mod-manager"):
            subprocess.run(["systemctl", "restart", service], capture_output=True)
    threading.Thread(target=_restart_later, daemon=True).start()
    return jsonify({"ok": True, "restarting": True})


# ---------------------------------------------------------------------------
# Anvil Mod Manager companion app — install/launch from the installer UI
# ---------------------------------------------------------------------------

@app.route("/api/mod_manager/status")
@require_auth
def mod_manager_status():
    info = detect_mod_manager()
    info["url"] = f"http://{get_local_ip()}:{MOD_MANAGER_PORT}/"
    return jsonify(info)


@app.route("/api/server_manager/status")
@require_auth
def server_manager_status():
    info = detect_server_manager()
    info["url"] = f"http://{get_local_ip()}:{SERVER_MANAGER_PORT}/"
    return jsonify(info)


@app.route("/api/run/<step>")
@require_auth
def api_run(step):
    if step not in STEPS:
        return Response("Unknown step", status=404)

    if PREVIEW:
        def fake_stream():
            for line in PREVIEW_LINES.get(step, [">> [PREVIEW] nothing to show"]):
                time.sleep(0.35)
                yield f"data: {json.dumps({'line': line})}\n\n"
            _preview_state[step] = True
            yield f"data: {json.dumps({'done': True, 'exit_code': 0})}\n\n"
        return Response(fake_stream(), mimetype="text/event-stream")

    def stream():
        script = STEPS[step]
        proc = subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in iter(proc.stdout.readline, ""):
            yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
        proc.wait()

        yield f"data: {json.dumps({'done': True, 'exit_code': proc.returncode})}\n\n"

    return Response(stream(), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Danger Zone — uninstall routes
# ---------------------------------------------------------------------------

@app.route("/api/uninstall/<name>")
@require_auth
def api_uninstall(name):
    if name == "installer":
        return _uninstall_self()

    if name not in UNINSTALL_SCRIPTS:
        return Response("Unknown component", status=404)

    purge = request.args.get("purge") == "1"

    if PREVIEW:
        def fake_stream():
            for line in UNINSTALL_PREVIEW_LINES.get(name, [">> [PREVIEW] nothing to show"]):
                time.sleep(0.35)
                yield f"data: {json.dumps({'line': line})}\n\n"
            if name == "docker":
                _preview_state["docker"] = False
                _preview_state["crafty"] = False
            elif name == "crafty":
                _preview_state["crafty"] = False
            elif name == "cockpit":
                _preview_state["cockpit"] = False
            elif name == "mod_manager":
                _preview_state["mod_manager"] = False
            elif name == "server_manager":
                _preview_state["server_manager"] = False
            yield f"data: {json.dumps({'done': True, 'exit_code': 0})}\n\n"
        return Response(fake_stream(), mimetype="text/event-stream")

    def stream():
        env = os.environ.copy()
        if purge:
            env["ANVIL_PURGE_DATA"] = "1"
        proc = subprocess.Popen(
            ["bash", "-c", UNINSTALL_SCRIPTS[name]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        for line in iter(proc.stdout.readline, ""):
            yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
        proc.wait()
        yield f"data: {json.dumps({'done': True, 'exit_code': proc.returncode})}\n\n"

    return Response(stream(), mimetype="text/event-stream")


def _uninstall_self():
    """Removes the Anvil Server Installer dashboard itself. Deliberately does
    NOT touch Docker, Crafty, Cockpit, Mod Manager, or the firewall's other
    rules — only this app's own service, install directory, and config."""
    if PREVIEW:
        def fake_stream():
            yield f"data: {json.dumps({'line': '>> [PREVIEW] would remove the Anvil Server Installer service and files.'})}\n\n"
            time.sleep(0.4)
            yield f"data: {json.dumps({'done': True, 'exit_code': 0})}\n\n"
        return Response(fake_stream(), mimetype="text/event-stream")

    def stream():
        yield f"data: {json.dumps({'line': '>> Removing this dashboard\u2019s own firewall rule (8090/tcp)...'})}\n\n"
        subprocess.run(["ufw", "delete", "allow", "8090/tcp"], capture_output=True)
        yield f"data: {json.dumps({'line': '>> Docker, Crafty, Cockpit, Anvil Mod Manager, and your Minecraft servers are NOT affected — only this setup dashboard is being removed.'})}\n\n"
        yield f"data: {json.dumps({'line': '>> This dashboard is about to remove itself. This page will stop responding right after.'})}\n\n"
        yield f"data: {json.dumps({'done': True, 'exit_code': 0, 'self_destructing': True})}\n\n"

        def _teardown_later():
            time.sleep(1.5)
            # Delete files BEFORE stopping the service: stopping the service
            # kills this very process, so anything after that line never runs.
            subprocess.run(["rm", "-rf", INSTALL_DIR])
            # /etc/anvilmc is SHARED with Mod Manager and Server Manager now
            # (the token, get-link.sh, PAM service) — only remove what's
            # actually specific to this dashboard, not the shared login the
            # other two still depend on.
            subprocess.run(["rm", "-f", "/etc/anvilmc/session_secret_installer",
                             "/etc/anvilmc/firewall_confirmed", "/etc/anvilmc/firewall_autorevert.log",
                             "/etc/anvilmc/sudo_session", "/etc/anvilmc/enc.key"])
            subprocess.run(["systemctl", "disable", "--now", "anvil-installer"])
        threading.Thread(target=_teardown_later, daemon=True).start()

    return Response(stream(), mimetype="text/event-stream")
# memory. Only an encrypted blob is written to disk (root-owned, 0600), and
# only after a successful PAM check. The terminal itself runs as root
# because the whole Flask process already does; the password isn't replayed
# into any command.
# ---------------------------------------------------------------------------

@app.route("/api/terminal/status")
@require_auth
def terminal_status():
    if PREVIEW:
        return jsonify({"unlocked": False, "username": None})
    creds = load_session_creds()
    return jsonify({"unlocked": bool(creds), "username": creds["username"] if creds else None})


@app.route("/api/terminal/auth", methods=["POST"])
@require_auth
def terminal_auth():
    data = request.get_json(force=True) or {}
    username, password = data.get("username", ""), data.get("password", "")

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password required."}), 400

    if PREVIEW:
        return jsonify({"ok": False, "error": "Terminal auth is disabled in preview mode."}), 400

    if not _PAM:
        return jsonify({"ok": False, "error": "PAM isn't available on this server (couldn't load libpam)."}), 500

    if verify_credentials(username, password):
        save_session_creds(username, password)
        session["terminal_unlocked"] = True
        session["terminal_user"] = username
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "Invalid username or password."}), 401


@app.route("/api/terminal/logout", methods=["POST"])
@require_auth
def terminal_logout():
    clear_session_creds()
    session.pop("terminal_unlocked", None)
    session.pop("terminal_user", None)
    return jsonify({"ok": True})


if sock is not None and _PTY_AVAILABLE:
    @sock.route("/ws/terminal")
    def ws_terminal(ws):
        if PREVIEW:
            ws.close(reason="unavailable in preview mode")
            return
        if not (session.get("terminal_unlocked") or load_session_creds()):
            ws.close(reason="unauthorized")
            return

        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("bash", ["bash", "--login"])
            return

        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                try:
                    ws.send(data.decode(errors="ignore"))
                except Exception:
                    break

        threading.Thread(target=reader, daemon=True).start()

        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                try:
                    m = json.loads(msg)
                except (TypeError, ValueError):
                    continue
                if "input" in m:
                    os.write(fd, m["input"].encode())
                elif "resize" in m:
                    r, c = m["resize"].get("rows", 24), m["resize"].get("cols", 80)
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", r, c, 0, 0))
        finally:
            stop.set()
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    port = 8090
    if PREVIEW:
        print("=" * 50)
        print(" PREVIEW MODE — no real system commands will run.")
        print(f" Open http://localhost:{port}/ in your browser.")
        print("=" * 50)
    app.run(host="0.0.0.0", port=port, threaded=True)
