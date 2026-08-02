#!/usr/bin/env python3
"""
Anvil Server Manager — the central hub for the Anvil ecosystem.

Ties Anvil Server Installer and Anvil Mod Manager together: one home page
with links to both, one place to check for Crafty/Docker/Cockpit/companion
updates, a fleet overview of every server Anvil Mod Manager tracks, scheduled
world backups (restic), and a Discord/webhook notifier.

Runs as root (same trust model as Anvil Server Installer) because checking
for and applying Docker/Cockpit updates and managing the Crafty container
needs real system access. Gated by an access token generated at install time
(see ../install.sh) — keep this LAN-only, never port-forwarded.

GitHub: https://github.com/TomCodes-sys/Anvil-Server-Manager
Companion apps:
  https://github.com/TomCodes-sys/Anvil-Server-Installer
  https://github.com/TomCodes-sys/Anvil-Mod-Manager
"""
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import timedelta
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, session

# PAM login fallback for the sign-in page. Binds directly to libpam via
# ctypes instead of the `python-pam` pip package — that package needs to
# compile a C extension against libpam headers (gcc + python3-dev +
# libpam0g-dev), which kept failing/half-installing in practice. libpam.so.0
# itself is a base-system library already on every real Ubuntu install (PAM
# is how login/su/sudo authenticate), so this needs nothing extra at all —
# no install step, no restart to pick it up.
#
# Authenticates against a dedicated "anvil-auth" PAM service (written to
# /etc/pam.d/anvil-auth on first use) rather than the generic "login"
# service — "login" pulls in pam_securetty/pam_nologin/pam_lastlog, which
# assume a real interactive TTY and reliably fail even a correct password
# from a non-interactive process like this one. A minimal service that just
# includes common-auth/common-account (what Debian/Ubuntu's own "passwd" and
# "sudo" services build on) is the same idea as Cockpit's own dedicated PAM
# service file.
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
        pass


_libpam = None
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
except OSError:
    _libpam = None

_PAM = _libpam is not None


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

app = Flask(__name__, static_folder="static", template_folder="templates")


def get_or_create_secret_key(path):
    """Same reasoning as Anvil Server Installer: a fresh secret_key on every
    restart silently logs everyone out even though nothing about the token
    changed. Persisted to disk, root-only, same trust tier as the token."""
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
        pass
    return new_secret


app.secret_key = get_or_create_secret_key("/etc/anvilmc/session_secret_server_manager")

# 30-minute idle timeout: session.permanent + PERMANENT_SESSION_LIFETIME give
# the cookie a real expiry instead of the previous behavior (an
# indefinitely-lived session cookie, or one that got invalidated unpredictably
# whenever the secret key changed — now fixed separately by persisting that
# key to disk). SESSION_REFRESH_EACH_REQUEST (Flask's default: on) means the
# 30 minutes is idle time, not a hard cutoff — it resets on every request, so
# someone actively using the dashboard is never logged out mid-session.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)

PREVIEW = os.environ.get("ANVIL_SERVER_MANAGER_PREVIEW", "0") == "1"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
BACKUP_HISTORY_FILE = DATA_DIR / "backup_history.json"

# Shares its token with Anvil Server Installer (installed first, on the same
# box) instead of generating its own — one token, one link to note down,
# instead of two dashboards each needing their own. Falls back to this app's
# own legacy token path for installs from before the AnvilMC monorepo migration
# and haven't re-run it since.
TOKEN_PATH = "/etc/anvilmc/token"
LEGACY_TOKEN_PATH = "/etc/anvil-installer/token"  # pre-monorepo installs (shared Installer+Server Manager token)
OLDEST_LEGACY_TOKEN_PATH = "/etc/anvil-server-manager/token"  # original separate-repo installs, before token sharing existed at all
EXTERNAL_CRAFTY_API_PATH = Path("/etc/anvilmc/crafty_api.json")  # shared with Mod Manager now — one Crafty connection for both


def _load_external_crafty_api():
    try:
        data = json.loads(EXTERNAL_CRAFTY_API_PATH.read_text())
        return data.get("crafty_url", ""), data.get("crafty_token", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "", ""


def _save_external_crafty_api(url, token):
    try:
        EXTERNAL_CRAFTY_API_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXTERNAL_CRAFTY_API_PATH.write_text(json.dumps({"crafty_url": url, "crafty_token": token}, indent=2))
        os.chmod(EXTERNAL_CRAFTY_API_PATH, 0o660)  # group-writable — shared with Mod Manager's different user, not owner-only
    except OSError:
        pass

GITHUB_REPO = "TomCodes-sys/Anvil-MC"
INSTALL_DIR = Path(__file__).parent
MONOREPO_DIR = Path(__file__).parent.parent  # /opt/anvilmc — the shared clone all three apps live inside
SERVICE_NAME = "anvil-server-manager"

INSTALLER_URL = "http://127.0.0.1:8090/"
MOD_MANAGER_URL = "http://127.0.0.1:5151/"
MOD_MANAGER_DATA_DIR = Path("/opt/anvilmc/mod-manager/data")

CRAFTY_CONTAINER = "crafty"
CRAFTY_IMAGE_REPO = "registry.gitlab.com/crafty-controller/crafty-4"
CRAFTY_GITLAB_PROJECT = "crafty-controller%2Fcrafty-4"  # URL-encoded path for the GitLab API

DEFAULT_SETTINGS = {
    "discord": {"webhook_url": "", "notify_backup_complete": True,
                "notify_backup_failed": True, "notify_update_available": True,
                "notify_health_alert": True, "notify_crash_detected": True},
    "backup": {
        "repo_type": "local",          # local | s3 | b2
        "repo_path": "/opt/anvil-backups/restic-repo",
        "restic_password": "",
        "s3_bucket": "", "s3_endpoint": "", "s3_access_key": "", "s3_secret_key": "",
        "b2_bucket": "", "b2_account_id": "", "b2_account_key": "",
        "source_paths": ["/opt/crafty/servers", "/opt/crafty/config"],
        "keep_last": 14,
        "schedule_enabled": False,
        "schedule_interval_hours": 24,
        "last_run_at": None,
    },
    # Crafty's own image tag is pinned here rather than tracking Docker's mutable
    # :latest — see the Updates panel note. "latest" is the starting value so
    # existing installs (whose container was created against :latest by Anvil
    # Server Installer) keep working until the first "Update" pins a real version.
    "crafty_image_tag": "latest",
    "rcon_targets": {},   # server_path -> {host, port, password, label}
    "_notified_updates": {},  # tracks which updates we've already pinged Discord about
    "crafty_api": {"url": "", "token": ""},
    "crash_recovery": {
        "enabled": False,
        "auto_restart": True,
        "check_interval_s": 60,
    },
    "health_monitor": {
        "disk_warn_pct": 85,
        "disk_crit_pct": 95,
        "cpu_temp_warn_c": 75,
        "cpu_temp_crit_c": 90,
        "ram_warn_pct": 85,
        "ram_crit_pct": 95,
        "notify_on_warn": True,
    },
}


def load_settings():
    if SETTINGS_FILE.exists():
        data = json.loads(SETTINGS_FILE.read_text())
    else:
        data = {}
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    for k, v in data.items():
        if isinstance(v, dict) and k in merged:
            merged[k].update(v)
        else:
            merged[k] = v
    # Same reasoning as Anvil Mod Manager: the Danger Zone uninstall for this
    # app clears data/ (wiping data/settings.json and the Crafty API token in
    # it) even though its source code stays put as part of the shared
    # AnvilMC checkout. If this is a fresh/reinstalled settings.json with no
    # token yet, but the externally persisted copy (outside /opt, in
    # /etc/anvilmc, survives that) has one, restore it automatically.
    api = merged.get("crafty_api", {})
    if not api.get("url") and not api.get("token"):
        ext_url, ext_token = _load_external_crafty_api()
        if ext_url or ext_token:
            merged["crafty_api"] = {"url": ext_url, "token": ext_token}
    return merged


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    api = settings.get("crafty_api", {})
    if api.get("url") or api.get("token"):
        _save_external_crafty_api(api.get("url", ""), api.get("token", ""))


def load_backup_history():
    if BACKUP_HISTORY_FILE.exists():
        return json.loads(BACKUP_HISTORY_FILE.read_text())
    return []


def save_backup_history(history):
    BACKUP_HISTORY_FILE.write_text(json.dumps(history[-100:], indent=2))


# ---------------------------------------------------------------------------
# Auth — same token-gate model as Anvil Server Installer, since this app can
# also run real apt/docker commands on the host.
# ---------------------------------------------------------------------------

def get_token():
    for path in (TOKEN_PATH, LEGACY_TOKEN_PATH, OLDEST_LEGACY_TOKEN_PATH):
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return None


def verify_credentials(username, password):
    return _pam_authenticate(username, password)


UNAUTHORIZED_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anvil Server Manager &mdash; sign in</title>
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
    <p class="sub">This dashboard needs the same token as Anvil Server Installer, or a server login.</p>
    <div class="tabs">
      <button id="tab-token" class="active" onclick="showPane('token')">I have my token</button>
      <button id="tab-pam" onclick="showPane('pam')">Use my server login</button>
    </div>
    <div id="pane-token" class="pane active">
      <label>Token</label>
      <input id="in-token" type="text" placeholder="the same token as the Installer" autofocus>
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
    <p class="hint">Lost the token? SSH into the server and run <code>sudo /etc/anvil-installer/get-link.sh</code>
      to print both this dashboard's link and the Installer's — they share one token now.
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


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if PREVIEW:
            return f(*args, **kwargs)
        expected = get_token()
        if not expected:
            return f(*args, **kwargs)  # no token configured yet — don't lock the operator out
        if session.get("authed"):
            return f(*args, **kwargs)
        supplied = request.args.get("token") or request.headers.get("X-Anvil-Token")
        if supplied and secrets.compare_digest(supplied, expected):
            session["authed"] = True
            session.permanent = True  # start the 30-minute idle timeout
            return f(*args, **kwargs)
        pam_hint = ("No token handy at all? Use the \"Use my server login\" tab to sign in with your "
                    "Linux username/password instead." if _PAM else
                    "This server's PAM library couldn't be loaded, so username/password sign-in isn't "
                    "available here — you'll need the token.")
        return Response(UNAUTHORIZED_PAGE.format(pam_hint=pam_hint), status=401, mimetype="text/html")
    return wrapped


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Same second-door-in pattern as Anvil Server Installer: paste the
    (shared) token, or fall back to a PAM check of the box's own Linux
    login when nobody can find the token at all."""
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
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Invalid username or password."}), 401
    return jsonify({"ok": False, "error": "Unknown login mode."}), 400


@app.route("/")
@require_auth
def index():
    return render_template("index.html")


@app.route("/api/mode")
def api_mode():
    return jsonify({"preview": PREVIEW})


# ---------------------------------------------------------------------------
# Discord / webhook notifier
# ---------------------------------------------------------------------------

def notify_discord(event_key, message, force=False):
    """Posts a plain message to the configured Discord webhook if that event
    type is enabled. `force=True` bypasses the enabled-check (used by the
    'send test message' button)."""
    settings = load_settings()
    discord = settings.get("discord", {})
    webhook = discord.get("webhook_url")
    if not webhook:
        return False, "No webhook URL configured."
    if not force and not discord.get(f"notify_{event_key}", True):
        return False, "That notification type is disabled."
    if PREVIEW:
        return True, "preview"
    try:
        r = requests.post(webhook, json={"content": message[:1900]}, timeout=10)
        if r.status_code >= 300:
            return False, f"Discord returned HTTP {r.status_code}"
        return True, None
    except requests.RequestException as e:
        return False, str(e)


def notify_update_once(component, message):
    """Only fires the Discord ping the first time a given component's update
    becomes available, not on every 60s poll — otherwise a person leaving the
    dashboard open would get spammed."""
    settings = load_settings()
    notified = settings.setdefault("_notified_updates", {})
    if notified.get(component):
        return
    notified[component] = True
    save_settings(settings)
    notify_discord("update_available", message)


def clear_notified(component):
    settings = load_settings()
    notified = settings.setdefault("_notified_updates", {})
    if notified.pop(component, None) is not None:
        save_settings(settings)


@app.route("/api/notifications/discord", methods=["GET", "POST"])
@require_auth
def notifications_discord():
    settings = load_settings()
    if request.method == "POST":
        body = request.json or {}
        d = settings["discord"]
        d["webhook_url"] = body.get("webhook_url", d["webhook_url"])
        for key in ("notify_backup_complete", "notify_backup_failed", "notify_update_available",
                    "notify_health_alert", "notify_crash_detected"):
            if key in body:
                d[key] = bool(body[key])
        save_settings(settings)
        return jsonify({"ok": True})
    d = dict(settings["discord"])
    d["webhook_set"] = bool(d.pop("webhook_url", ""))
    return jsonify(d)


@app.route("/api/notifications/test", methods=["POST"])
@require_auth
def notifications_test():
    ok, err = notify_discord("test", "🔨 Anvil Server Manager — this is a test notification.", force=True)
    return jsonify({"ok": ok, "error": err})


# ---------------------------------------------------------------------------
# Companion app status (Installer + Mod Manager) — for the Home page
# ---------------------------------------------------------------------------

def _probe(url):
    if PREVIEW:
        return True
    try:
        r = requests.get(url, timeout=3)
        return r.status_code < 500
    except requests.RequestException:
        return False


@app.route("/api/companions")
@require_auth
def api_companions():
    return jsonify({
        "installer": {"url": "http://" + request.host.split(":")[0] + ":8090/", "reachable": _probe(INSTALLER_URL)},
        "mod_manager": {"url": "http://" + request.host.split(":")[0] + ":5151/", "reachable": _probe(MOD_MANAGER_URL)},
    })


# ---------------------------------------------------------------------------
# Self-update (GitHub) — identical pattern to the Installer and Mod Manager.
# ---------------------------------------------------------------------------

def _git_head(path):
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _github_latest_commit(repo, branch="main"):
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "anvil-server-manager",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return {"sha": data.get("sha"),
                "message": (data.get("commit", {}).get("message", "").splitlines() or [""])[0],
                "html_url": data.get("html_url")}
    except Exception:
        if branch == "main":
            return _github_latest_commit(repo, "master")
        return None


@app.route("/api/updates/self/check")
@require_auth
def self_update_check():
    if PREVIEW:
        return jsonify({"update_available": False, "preview": True})
    current = _git_head(INSTALL_DIR)
    latest = _github_latest_commit(GITHUB_REPO)
    if not current or not latest or not latest.get("sha"):
        return jsonify({"update_available": False, "checked": False,
                         "note": "Couldn't determine version info (not a git checkout, or GitHub unreachable)."})
    update_available = current != latest["sha"]
    if update_available:
        notify_update_once("self", f"🔔 Anvil Server Manager update available: {latest.get('message', '')}")
    else:
        clear_notified("self")
    return jsonify({
        "checked": True, "update_available": update_available,
        "current": current[:7], "latest": latest["sha"][:7],
        "latest_message": latest.get("message"),
        "repo_url": f"https://github.com/{GITHUB_REPO}",
        "compare_url": f"https://github.com/{GITHUB_REPO}/compare/{current[:12]}...{latest['sha'][:12]}",
    })


@app.route("/api/updates/self/apply", methods=["POST"])
@require_auth
def self_update_apply():
    """AnvilMC's update action now lives in one place — the Installer
    dashboard, which pulls the whole shared monorepo and restarts all three
    services in one go, instead of three separate self-updates each only
    touching their own subfolder (and each needing you to remember which
    dashboard's "Update" button to click)."""
    return jsonify({"ok": False, "redirect_to_installer": True,
                     "error": "Run updates from the Installer dashboard's Home tab — it updates all three AnvilMC apps together in one action."}), 400


# ---------------------------------------------------------------------------
# Central updater: Crafty, Docker, Cockpit — same "check, then apply" shape
# as the self-updater, so the frontend can reuse one banner component for
# all four. Each one's check/apply is independent so a failure in one never
# blocks the others (no overlapping state).
# ---------------------------------------------------------------------------

def _apt_versions(package):
    """Returns (installed, candidate) version strings for a package via
    `apt-cache policy`, or (None, None) if it isn't installed/found."""
    try:
        subprocess.run(["apt-get", "update", "-qq"], capture_output=True, timeout=60)
        r = subprocess.run(["apt-cache", "policy", package], capture_output=True, text=True, timeout=20)
        installed = candidate = None
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("Installed:"):
                installed = line.split(":", 1)[1].strip()
            elif line.startswith("Candidate:"):
                candidate = line.split(":", 1)[1].strip()
        if installed in (None, "(none)"):
            installed = None
        return installed, candidate
    except Exception:
        return None, None


@app.route("/api/updates/docker/check")
@require_auth
def docker_update_check():
    if PREVIEW:
        return jsonify({"checked": True, "update_available": False, "preview": True})
    installed, candidate = _apt_versions("docker-ce")
    if not installed:
        return jsonify({"checked": False, "update_available": False, "note": "Docker isn't installed via apt (docker-ce not found)."})
    available = bool(candidate and candidate != installed)
    if available:
        notify_update_once("docker", f"🔔 Docker engine update available: {installed} → {candidate}")
    else:
        clear_notified("docker")
    return jsonify({"checked": True, "update_available": available, "current": installed, "latest": candidate})


@app.route("/api/updates/docker/apply", methods=["POST"])
@require_auth
def docker_update_apply():
    if PREVIEW:
        return jsonify({"ok": False, "error": "Updating is disabled in preview mode."}), 400
    try:
        r = subprocess.run(
            ["apt-get", "install", "-y", "-qq", "--only-upgrade",
             "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.strip() or r.stdout.strip()}), 500
        clear_notified("docker")
        return jsonify({"ok": True, "output": r.stdout.strip()[-2000:]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/updates/cockpit/check")
@require_auth
def cockpit_update_check():
    if PREVIEW:
        return jsonify({"checked": True, "update_available": False, "preview": True})
    installed, candidate = _apt_versions("cockpit")
    if not installed:
        return jsonify({"checked": False, "update_available": False, "note": "Cockpit isn't installed."})
    available = bool(candidate and candidate != installed)
    if available:
        notify_update_once("cockpit", f"🔔 Cockpit update available: {installed} → {candidate}")
    else:
        clear_notified("cockpit")
    return jsonify({"checked": True, "update_available": available, "current": installed, "latest": candidate})


@app.route("/api/updates/cockpit/apply", methods=["POST"])
@require_auth
def cockpit_update_apply():
    if PREVIEW:
        return jsonify({"ok": False, "error": "Updating is disabled in preview mode."}), 400
    try:
        r = subprocess.run(
            ["apt-get", "install", "-y", "-qq", "--only-upgrade",
             "cockpit", "cockpit-storaged", "cockpit-navigator", "cockpit-bridge", "cockpit-ws", "cockpit-system"],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.strip() or r.stdout.strip()}), 500
        clear_notified("cockpit")
        return jsonify({"ok": True, "output": r.stdout.strip()[-2000:]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _gitlab_latest_crafty_tag():
    """Queries GitLab's public API for the most recent Crafty release tag —
    no auth needed, crafty-4 is a public project. Returns None on any
    failure (network, unexpected shape, etc.)."""
    url = f"https://gitlab.com/api/v4/projects/{CRAFTY_GITLAB_PROJECT}/releases"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "anvil-server-manager"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            releases = json.loads(resp.read().decode())
        if releases:
            return releases[0].get("tag_name")
    except Exception:
        pass
    return None


@app.route("/api/updates/crafty/check")
@require_auth
def crafty_update_check():
    """Compares the currently-pinned Crafty image tag (see settings) against
    the latest tag GitLab has released — NOT against Docker's mutable
    :latest. Pinning a specific version means updates are a deliberate,
    reviewable action instead of ':latest' silently drifting underneath the
    running container between reboots."""
    if PREVIEW:
        return jsonify({"checked": True, "update_available": False, "preview": True})
    settings = load_settings()
    current_tag = settings.get("crafty_image_tag", "latest")
    latest_tag = _gitlab_latest_crafty_tag()
    if not latest_tag:
        return jsonify({"checked": False, "update_available": False,
                         "note": "Couldn't reach GitLab to check the latest Crafty release."})
    available = current_tag != latest_tag
    if available:
        notify_update_once("crafty", f"🔔 Crafty Controller update available: {current_tag} → {latest_tag}")
    else:
        clear_notified("crafty")
    return jsonify({"checked": True, "update_available": available,
                     "current": current_tag, "latest": latest_tag})


@app.route("/api/updates/crafty/apply", methods=["POST"])
@require_auth
def crafty_update_apply():
    """Pulls the specific latest-released tag (not :latest), recreates the
    Crafty container from it using the exact same run parameters Anvil
    Server Installer used originally, and pins that tag in settings so the
    next check compares against a known, deliberate baseline."""
    if PREVIEW:
        return jsonify({"ok": False, "error": "Updating is disabled in preview mode."}), 400
    settings = load_settings()
    latest_tag = _gitlab_latest_crafty_tag() or settings.get("crafty_image_tag", "latest")
    image = f"{CRAFTY_IMAGE_REPO}:{latest_tag}"
    try:
        pull = subprocess.run(["docker", "pull", image], capture_output=True, text=True, timeout=300)
        if pull.returncode != 0:
            return jsonify({"ok": False, "error": pull.stderr.strip() or pull.stdout.strip()}), 500
        subprocess.run(["docker", "rm", "-f", CRAFTY_CONTAINER], capture_output=True, timeout=30)
        # IMPORTANT: --network host, matching Anvil Server Installer's original
        # `docker run` exactly. This used to individually map -p 8443/8123/
        # 25565-25570 instead, which is the precise bug the Installer's own
        # comments describe: Crafty lets a server run on ANY port via
        # server.properties, and anything outside a narrow mapped range (and
        # Bedrock's UDP ports, which weren't mapped here at all) becomes
        # unreachable even though Crafty shows it as running fine. Recreating
        # the container this way on every "Update Crafty" click silently
        # undid that fix each time.
        r = subprocess.run([
            "docker", "run", "-d", "--name", CRAFTY_CONTAINER,
            "--network", "host",
            "-v", "/opt/crafty/backups:/crafty/backups",
            "-v", "/opt/crafty/logs:/crafty/logs",
            "-v", "/opt/crafty/servers:/crafty/servers",
            "-v", "/opt/crafty/config:/crafty/app/config",
            "-v", "/opt/crafty/import:/crafty/import",
            "--restart", "unless-stopped",
            image,
        ], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.strip() or r.stdout.strip()}), 500
        settings["crafty_image_tag"] = latest_tag
        save_settings(settings)
        clear_notified("crafty")
        return jsonify({"ok": True, "pinned_tag": latest_tag})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Fleet overview / server discovery — this used to source its ENTIRE server
# list from Anvil Mod Manager's data/server_<hash>.json files. Two real
# problems with that: (1) Mod Manager deliberately excludes Bedrock servers
# (it only manages Java mods/plugins), so Bedrock servers could never appear
# in Fleet, RCON, Players, or Logs at all; and (2) if Mod Manager's data
# folder didn't exist at the expected path (different install location,
# nothing imported there yet, etc.), every one of those tabs looked
# completely empty with no indication why.
#
# Now Crafty's own API — which this app already talks to for stats/actions —
# is the PRIMARY source of "what servers exist," covering Java and Bedrock
# alike. Mod Manager's data is used only to enrich a Java server's entry with
# mc_version/loader/mod counts when it's available; a server Mod Manager
# doesn't know about (including every Bedrock one) still shows up, just
# without those extra fields.
# ---------------------------------------------------------------------------

BEDROCK_MARKER_FILE = "bedrock_server_how_to.html"
CRAFTY_SERVERS_ROOT_DEFAULT = "/opt/crafty/servers"


def _is_bedrock_world(server_path) -> bool:
    try:
        return (Path(server_path) / BEDROCK_MARKER_FILE).exists()
    except OSError:
        return False


def _mod_manager_data_by_path():
    """Anvil Mod Manager's tracked servers, keyed by server_path, for
    enrichment only — never the sole source of what servers exist."""
    out = {}
    if not MOD_MANAGER_DATA_DIR.is_dir():
        return out
    for f in MOD_MANAGER_DATA_DIR.glob("server_*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if data.get("server_path"):
            out[data["server_path"]] = data
    return out


def _tracked_servers():
    """Every server that exists, Java or Bedrock, sourced from Crafty
    directly — with Mod Manager's extra fields (crafty_server_id, world_name,
    mc_version, loader, mod/plugin/datapack counts) merged in wherever it
    knows about that server too. Returns [] if Crafty's API isn't configured
    or unreachable, same as before."""
    settings = load_settings()
    if not _crafty_api_configured(settings):
        return []
    api = settings["crafty_api"]
    try:
        r = requests.get(f"{api['url']}/api/v2/servers", headers={"Authorization": f"Bearer {api['token']}"},
                          timeout=15, verify=False)
        if r.status_code >= 400:
            return []
        body = r.json()
        crafty_servers = body.get("data", []) if isinstance(body, dict) else []
    except (requests.RequestException, ValueError):
        return []

    root = settings.get("crafty_servers_root", CRAFTY_SERVERS_ROOT_DEFAULT)
    by_path = _mod_manager_data_by_path()
    out = []
    for s in crafty_servers:
        sid = s.get("server_id") or s.get("server_uuid")
        if not sid:
            continue
        guessed_path = str(Path(root) / sid)
        platform = "bedrock" if _is_bedrock_world(guessed_path) else "java"
        enrichment = by_path.get(guessed_path, {})
        merged = {
            "server_path": guessed_path,
            "name": enrichment.get("name") or s.get("server_name") or sid,
            "crafty_server_id": sid,
            "world_name": enrichment.get("world_name", "world"),
            "platform": platform,
            "mc_version": enrichment.get("mc_version", ""),
            "loader": enrichment.get("loader", ""),
            "mods": enrichment.get("mods", []),
            "plugins": enrichment.get("plugins", []),
            "datapacks": enrichment.get("datapacks", []),
            "_tracked_by_mod_manager": bool(enrichment),
        }
        out.append(merged)
    return out


@app.route("/api/fleet")
@require_auth
def api_fleet():
    if PREVIEW:
        return jsonify({"available": True, "servers": [
            {"name": "Survival (preview)", "server_path": "/opt/crafty/servers/preview-survival", "platform": "java",
             "mc_version": "1.21.3", "loader": "paper", "mods": 0, "plugins": 12, "datapacks": 2, "updates_pending": 3},
            {"name": "Modded SMP (preview)", "server_path": "/opt/crafty/servers/preview-smp", "platform": "java",
             "mc_version": "1.20.1", "loader": "fabric", "mods": 84, "plugins": 0, "datapacks": 1, "updates_pending": 0},
            {"name": "Bedrock Survival (preview)", "server_path": "/opt/crafty/servers/preview-bedrock", "platform": "bedrock",
             "mc_version": "", "loader": "", "mods": 0, "plugins": 0, "datapacks": 0, "updates_pending": 0},
        ]})
    settings = load_settings()
    if not _crafty_api_configured(settings):
        return jsonify({"available": False, "note": "Set your Crafty URL and API token in Settings first.", "servers": []})

    servers = []
    for data in _tracked_servers():
        mods, plugins, datapacks = data["mods"], data["plugins"], data["datapacks"]
        pending = sum(1 for m in mods + plugins + datapacks if m.get("status") == "update_available")
        servers.append({
            "name": data["name"],
            "server_path": data["server_path"],
            "platform": data["platform"],
            "mc_version": data.get("mc_version") or ("n/a (Bedrock)" if data["platform"] == "bedrock" else "unset"),
            "loader": data.get("loader") or ("bedrock" if data["platform"] == "bedrock" else "vanilla"),
            "mods": len(mods), "plugins": len(plugins), "datapacks": len(datapacks),
            "updates_pending": pending,
        })
    note = None
    if not MOD_MANAGER_DATA_DIR.is_dir():
        note = ("Anvil Mod Manager isn't installed (or its data folder wasn't found) — server names/status still "
                 "come from Crafty directly, but mod/plugin/version details won't show until it's set up.")
    return jsonify({"available": True, "servers": servers, "note": note})


# ---------------------------------------------------------------------------
# Ubuntu Server Health Monitor — disk health/SMART, CPU temperature, RAM
# usage, network throughput, with a Discord alert before things actually
# fail (crossing a "warn" threshold, not just the "critical" one).
# ---------------------------------------------------------------------------

def _disk_usage():
    """Usage % for every real mounted filesystem, via /proc/mounts + statvfs
    (no extra dependency like psutil needed)."""
    out = []
    seen = set()
    try:
        with open("/proc/mounts") as f:
            lines = f.readlines()
    except Exception:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fstype = parts[0], parts[1], parts[2]
        if not device.startswith("/dev/") or mount in seen:
            continue
        if fstype in ("tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2"):
            continue
        try:
            st = os.statvfs(mount)
            total = st.f_blocks * st.f_frsize
            free = st.f_bfree * st.f_frsize
            if total == 0:
                continue
            used_pct = round((1 - free / total) * 100, 1)
            out.append({
                "device": device, "mount": mount,
                "total_gb": round(total / (1024**3), 1),
                "used_pct": used_pct,
            })
            seen.add(mount)
        except OSError:
            continue
    return out


def _smart_status():
    """Best-effort SMART health per physical disk via smartctl. Returns an
    empty list (not an error) if smartmontools isn't installed — SMART is a
    nice-to-have, its absence shouldn't make the whole health check fail."""
    if shutil.which("smartctl") is None:
        return {"available": False, "disks": []}
    disks = []
    # Whole-disk devices only (no partitions like sda1, nvme0n1p2, etc).
    try:
        candidates = sorted(
            d.name for d in Path("/sys/block").iterdir()
            if re.fullmatch(r"(sd[a-z]+|vd[a-z]+|nvme\d+n\d+)", d.name)
        )
    except Exception:
        candidates = []
    for name in candidates:
        dev = f"/dev/{name}"
        try:
            r = subprocess.run(["smartctl", "-H", dev], capture_output=True, text=True, timeout=15)
            out = (r.stdout or "") + (r.stderr or "")
            if "PASSED" in out:
                health = "passed"
            elif "FAILED" in out:
                health = "failed"
            else:
                health = "unknown"
            disks.append({"device": dev, "health": health})
        except Exception:
            disks.append({"device": dev, "health": "unknown"})
    return {"available": True, "disks": disks}


def _cpu_temp():
    """Highest reported CPU-ish temperature in °C, checking two sources
    since neither alone covers all hardware:
      1. /sys/class/thermal/thermal_zone* — ACPI thermal zones. Reliable on
         most server/laptop boards, but plenty of desktop boards (a common
         choice for a home Minecraft box) don't register one at all.
      2. /sys/class/hwmon/hwmon*/temp*_input — what lm-sensors itself reads
         (coretemp, k10temp, etc.), and catches hardware thermal_zone misses.
    Returns None only if NEITHER source has anything, rather than pretending
    to know on hardware that genuinely doesn't expose it (common on cloud VPS)."""
    best = None

    try:
        base = Path("/sys/class/thermal")
        if base.is_dir():
            for zone in base.glob("thermal_zone*"):
                try:
                    raw = int((zone / "temp").read_text().strip())
                    c = raw / 1000.0 if raw > 1000 else float(raw)
                    if best is None or c > best:
                        best = c
                except Exception:
                    continue
    except Exception:
        pass

    try:
        hwmon_base = Path("/sys/class/hwmon")
        if hwmon_base.is_dir():
            for chip in hwmon_base.glob("hwmon*"):
                try:
                    name = (chip / "name").read_text().strip().lower()
                except Exception:
                    name = ""
                # Only trust chips that are plausibly a CPU sensor — hwmon
                # also exposes things like battery/fan/voltage chips whose
                # "temp1_input" (if any) isn't a CPU temperature at all.
                if name not in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
                    continue
                for temp_file in chip.glob("temp*_input"):
                    try:
                        raw = int(temp_file.read_text().strip())
                        c = raw / 1000.0 if raw > 1000 else float(raw)
                        if best is None or c > best:
                            best = c
                    except Exception:
                        continue
    except Exception:
        pass

    return round(best, 1) if best is not None else None


def _ram_usage():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.strip().split()[0])  # kB
    except Exception:
        return None
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    if not total:
        return None
    used_pct = round((1 - avail / total) * 100, 1)
    return {"total_mb": round(total / 1024), "used_pct": used_pct}


_net_prev = {"t": None, "counters": {}}


def _network_speed():
    """Instantaneous throughput per interface, computed from the delta
    between two /proc/net/dev reads a moment apart (no extra dependency)."""
    def _read():
        counters = {}
        try:
            with open("/proc/net/dev") as f:
                lines = f.readlines()[2:]
            for line in lines:
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo":
                    continue
                fields = rest.split()
                counters[iface] = {"rx": int(fields[0]), "tx": int(fields[8])}
        except Exception:
            pass
        return counters

    now = time.time()
    first = _read()
    time.sleep(1.0)
    second = _read()
    elapsed = max(0.1, time.time() - now)

    out = []
    for iface, c2 in second.items():
        c1 = first.get(iface)
        if not c1:
            continue
        rx_bps = max(0, (c2["rx"] - c1["rx"]) / elapsed)
        tx_bps = max(0, (c2["tx"] - c1["tx"]) / elapsed)
        out.append({
            "iface": iface,
            "rx_kbps": round(rx_bps / 1024, 1),
            "tx_kbps": round(tx_bps / 1024, 1),
        })
    return out


def _health_snapshot():
    return {
        "disks": _disk_usage(),
        "smart": _smart_status(),
        "cpu_temp_c": _cpu_temp(),
        "ram": _ram_usage(),
        "network": _network_speed(),
    }


@app.route("/api/health/system")
@require_auth
def api_health_system():
    if PREVIEW:
        return jsonify({
            "disks": [{"device": "/dev/sda1", "mount": "/", "total_gb": 233.0, "used_pct": 61.4}],
            "smart": {"available": True, "disks": [{"device": "/dev/sda", "health": "passed"}]},
            "cpu_temp_c": 48.2,
            "ram": {"total_mb": 15872, "used_pct": 42.0},
            "network": [{"iface": "eth0", "rx_kbps": 128.4, "tx_kbps": 32.1}],
        })
    return jsonify(_health_snapshot())


@app.route("/api/health/settings", methods=["GET", "POST"])
@require_auth
def health_settings():
    settings = load_settings()
    if request.method == "POST":
        body = request.json or {}
        h = settings["health_monitor"]
        for key in ("disk_warn_pct", "disk_crit_pct", "cpu_temp_warn_c", "cpu_temp_crit_c",
                    "ram_warn_pct", "ram_crit_pct"):
            if key in body:
                h[key] = float(body[key])
        if "notify_on_warn" in body:
            h["notify_on_warn"] = bool(body["notify_on_warn"])
        save_settings(settings)
        return jsonify({"ok": True})
    return jsonify(settings["health_monitor"])


def _health_monitor_loop():
    """Every 5 minutes, checks disk/CPU-temp/RAM against configured
    thresholds and pings Discord once per breach (not on every single poll)
    so a slow climb toward "full disk" gets flagged well before Crafty
    itself starts failing writes."""
    while True:
        try:
            settings = load_settings()
            h = settings["health_monitor"]
            snap = _health_snapshot()

            alerts = []
            for d in snap["disks"]:
                if d["used_pct"] >= h["disk_crit_pct"]:
                    alerts.append(("disk_" + d["mount"], f"🔴 Disk critical: {d['mount']} is {d['used_pct']}% full."))
                elif d["used_pct"] >= h["disk_warn_pct"]:
                    alerts.append(("disk_" + d["mount"], f"🟠 Disk warning: {d['mount']} is {d['used_pct']}% full."))

            for disk in snap["smart"].get("disks", []):
                if disk["health"] == "failed":
                    alerts.append(("smart_" + disk["device"], f"🔴 SMART FAILURE reported on {disk['device']} — back up and replace this disk soon."))

            temp = snap["cpu_temp_c"]
            if temp is not None:
                if temp >= h["cpu_temp_crit_c"]:
                    alerts.append(("cpu_temp", f"🔴 CPU temperature critical: {temp}°C."))
                elif temp >= h["cpu_temp_warn_c"]:
                    alerts.append(("cpu_temp", f"🟠 CPU temperature warning: {temp}°C."))

            ram = snap["ram"]
            if ram:
                if ram["used_pct"] >= h["ram_crit_pct"]:
                    alerts.append(("ram", f"🔴 RAM critical: {ram['used_pct']}% used."))
                elif ram["used_pct"] >= h["ram_warn_pct"]:
                    alerts.append(("ram", f"🟠 RAM warning: {ram['used_pct']}% used."))

            notified = settings.setdefault("_notified_health", {})
            changed = False
            active_keys = set()
            for key, msg in alerts:
                active_keys.add(key)
                if h.get("notify_on_warn", True) and not notified.get(key):
                    notify_discord("health_alert", msg)
                    notified[key] = True
                    changed = True
            # Clear notified-state for anything that's back to normal, so a
            # future breach re-alerts instead of staying silenced forever.
            for key in list(notified.keys()):
                if key not in active_keys:
                    notified.pop(key, None)
                    changed = True
            if changed:
                save_settings(settings)
        except Exception:
            pass
        time.sleep(300)


if not PREVIEW:
    threading.Thread(target=_health_monitor_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Crash Recovery Tool — watches every Crafty-linked server (via Anvil Mod
# Manager's tracked profiles), detects an unexpected stop (not one you
# triggered yourself through Crafty), restarts it automatically, saves a
# copy of its recent logs somewhere durable, and flags a likely-corrupt
# world so you don't lose time troubleshooting the wrong thing.
# ---------------------------------------------------------------------------

CRASH_LOG_DIR = Path("/opt/anvil-backups/crash-logs")


def _crafty_api_configured(settings):
    api = settings.get("crafty_api", {})
    return bool(api.get("url") and api.get("token"))


def _crafty_stats(crafty_id, settings):
    api = settings["crafty_api"]
    try:
        r = requests.get(f"{api['url']}/api/v2/servers/{crafty_id}/stats",
                          headers={"Authorization": f"Bearer {api['token']}"}, timeout=10, verify=False)
        if r.status_code >= 400:
            return None
        body = r.json()
        # Same "data" wrapping as Anvil Mod Manager talks to — see its
        # crafty_running_status() for the same fix applied there.
        return body.get("data") if isinstance(body.get("data"), dict) else body
    except (requests.RequestException, ValueError):
        return None


def _crafty_action(crafty_id, action, settings):
    api = settings["crafty_api"]
    try:
        r = requests.post(f"{api['url']}/api/v2/servers/{crafty_id}/action/{action}",
                           headers={"Authorization": f"Bearer {api['token']}"}, timeout=15, verify=False)
        return r.status_code < 400
    except requests.RequestException:
        return False


def _world_looks_corrupt(server_path, world_name):
    """Cheap heuristics only — this is a warning to go look, not a proof.
    Flags: missing level.dat, a zero-byte level.dat, or any zero-byte
    region file (a very common signature of a server killed mid-write)."""
    reasons = []
    world_dir = Path(server_path) / (world_name or "world")
    level_dat = world_dir / "level.dat"
    if not level_dat.exists():
        reasons.append("level.dat is missing")
    elif level_dat.stat().st_size == 0:
        reasons.append("level.dat is 0 bytes")
    region_dir = world_dir / "region"
    if region_dir.is_dir():
        try:
            zero_byte = [p.name for p in region_dir.glob("*.mca") if p.stat().st_size == 0]
            if zero_byte:
                reasons.append(f"{len(zero_byte)} zero-byte region file(s), e.g. {zero_byte[0]}")
        except Exception:
            pass
    return reasons


def _capture_crash_logs(server_path, name):
    """Copies whatever log/crash-report files exist into a timestamped
    folder outside the server's own directory, so they survive even if the
    world folder itself gets wiped/reinstalled later."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "server")
    dest = CRASH_LOG_DIR / safe_name / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    src = Path(server_path)
    for candidate in [src / "logs" / "latest.log", src / "latest.log"]:
        if candidate.exists():
            try:
                shutil.copy2(candidate, dest / "latest.log")
                copied.append("latest.log")
            except Exception:
                pass
            break
    crash_dir = src / "crash-reports"
    if crash_dir.is_dir():
        try:
            reports = sorted(crash_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
            for r in reports:
                shutil.copy2(r, dest / r.name)
                copied.append(r.name)
        except Exception:
            pass
    return str(dest), copied


_crash_last_running = {}  # crafty_server_id -> bool
_crash_pending_stop = {}  # crafty_server_id -> consecutive-stopped tick count


@app.route("/api/crash_recovery/settings", methods=["GET", "POST"])
@require_auth
def crash_recovery_settings():
    settings = load_settings()
    if request.method == "POST":
        body = request.json or {}
        cr = settings["crash_recovery"]
        if "enabled" in body:
            cr["enabled"] = bool(body["enabled"])
        if "auto_restart" in body:
            cr["auto_restart"] = bool(body["auto_restart"])
        if "check_interval_s" in body:
            cr["check_interval_s"] = max(15, int(body["check_interval_s"]))
        save_settings(settings)
        return jsonify({"ok": True})
    return jsonify(settings["crash_recovery"])


@app.route("/api/crash_recovery/events")
@require_auth
def crash_recovery_events():
    settings = load_settings()
    return jsonify(settings.get("_crash_events", [])[-30:])


def _crash_watch_loop():
    while True:
        try:
            settings = load_settings()
            cr = settings["crash_recovery"]
            interval = max(15, int(cr.get("check_interval_s", 60)))
            if not cr.get("enabled") or not _crafty_api_configured(settings):
                time.sleep(interval)
                continue

            for data in _tracked_servers():
                crafty_id = data.get("crafty_server_id")
                if not crafty_id:
                    continue
                stats = _crafty_stats(crafty_id, settings)
                if stats is None:
                    continue  # Crafty unreachable this tick — don't guess, just wait for the next one
                running = bool(stats.get("running"))
                crashed_flag = bool(stats.get("crashed"))
                name = data.get("name") or crafty_id

                was_running = _crash_last_running.get(crafty_id)
                if was_running is True and not running:
                    _crash_pending_stop[crafty_id] = _crash_pending_stop.get(crafty_id, 0) + 1
                else:
                    _crash_pending_stop[crafty_id] = 0
                _crash_last_running[crafty_id] = running

                # Require either Crafty's own "crashed" flag, or the server
                # staying stopped for 2 consecutive ticks after having been
                # running — a single tick isn't enough to rule out someone
                # just clicking "stop" in Crafty at that exact moment.
                confirmed_crash = crashed_flag or _crash_pending_stop.get(crafty_id, 0) >= 2
                if not confirmed_crash:
                    continue

                _crash_pending_stop[crafty_id] = 0  # only fire once per crash
                server_path = data.get("server_path", "")
                world_name = data.get("world_name", "world")
                log_dir, copied = _capture_crash_logs(server_path, name)
                corrupt_reasons = _world_looks_corrupt(server_path, world_name)

                restarted = False
                if cr.get("auto_restart", True):
                    restarted = _crafty_action(crafty_id, "start_server", settings)

                event = {
                    "time": time.time(), "server": name, "server_path": server_path,
                    "log_dir": log_dir, "logs_captured": copied,
                    "corrupt_suspected": bool(corrupt_reasons), "corrupt_reasons": corrupt_reasons,
                    "auto_restarted": restarted,
                }
                events = settings.setdefault("_crash_events", [])
                events.append(event)
                settings["_crash_events"] = events[-50:]
                save_settings(settings)

                msg = f"💥 **{name}** crashed. Logs saved to `{log_dir}`."
                if restarted:
                    msg += " Restart triggered automatically."
                if cr.get("auto_restart", True) and not restarted:
                    msg += " ⚠️ Automatic restart failed — restart it manually."
                if corrupt_reasons:
                    msg += f"\n⚠️ Possible world corruption: {'; '.join(corrupt_reasons)} — check before rejoining."
                notify_discord("crash_detected", msg)

            time.sleep(interval)
        except Exception:
            time.sleep(60)


if not PREVIEW:
    threading.Thread(target=_crash_watch_loop, daemon=True).start()


@app.route("/api/network/local_ip")
@require_auth
def network_local_ip():
    """Best-effort local LAN IP of this machine — Crafty always runs
    co-located with this app (same box, same install flow), so this is
    enough to auto-suggest the Crafty API URL instead of making the person
    type it in by hand."""
    ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        pass
    if not ip or ip.startswith("127."):
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = None
    if not ip or ip.startswith("127."):
        return jsonify({"ok": False, "error": "Couldn't detect a LAN IP automatically — type it in manually."})
    return jsonify({"ok": True, "ip": ip, "suggested_crafty_url": f"https://{ip}:8443"})


@app.route("/api/settings/crafty_api", methods=["GET", "POST"])
@require_auth
def crafty_api_settings():
    """URL + API token for Crafty's own REST API — separate from the Docker
    container management this app already does, and needed specifically so
    Crash Recovery can check real per-server running state and trigger a
    restart. Anvil Mod Manager has the same pair of settings; they're kept
    independent since the two apps don't share a settings file."""
    settings = load_settings()
    if request.method == "POST":
        body = request.json or {}
        api = settings["crafty_api"]
        api["url"] = (body.get("url", api.get("url", "")) or "").rstrip("/")
        if body.get("token"):
            api["token"] = body["token"]
        save_settings(settings)
        return jsonify({"ok": True})
    api = settings["crafty_api"]
    return jsonify({"url": api.get("url", ""), "token_set": bool(api.get("token"))})


# ---------------------------------------------------------------------------
# RCON web console — a small native Source RCON client (the same protocol
# Minecraft servers speak), so commands can be sent without going through
# Crafty's own console or SSH. Independent of Crafty entirely: works against
# any server with rcon.port/rcon.password set in server.properties.
# ---------------------------------------------------------------------------

class RconError(Exception):
    pass


def _rcon_command(host, port, password, command, timeout=6):
    """One-shot Source RCON call: connect, auth, send command, read the
    response, disconnect. Simpler and more robust than holding a persistent
    connection open across requests from a stateless HTTP API."""
    import socket as _socket
    import struct as _struct

    def _send_packet(sock, request_id, pkt_type, payload):
        body = _struct.pack("<ii", request_id, pkt_type) + payload.encode("utf-8") + b"\x00\x00"
        sock.sendall(_struct.pack("<i", len(body)) + body)

    def _read_packet(sock):
        raw_len = _recv_exact(sock, 4)
        length = _struct.unpack("<i", raw_len)[0]
        data = _recv_exact(sock, length)
        req_id, pkt_type = _struct.unpack("<ii", data[:8])
        payload = data[8:-2].decode("utf-8", errors="replace")
        return req_id, pkt_type, payload

    def _recv_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RconError("Connection closed unexpectedly.")
            buf += chunk
        return buf

    with _socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _send_packet(sock, 1, 3, password)  # SERVERDATA_AUTH
        req_id, _, _ = _read_packet(sock)
        if req_id == -1:
            raise RconError("Authentication failed — check the RCON password.")
        _send_packet(sock, 2, 2, command)  # SERVERDATA_EXECCOMMAND
        _, _, response = _read_packet(sock)
        return response


@app.route("/api/rcon/targets", methods=["GET", "POST"])
@require_auth
def rcon_targets():
    settings = load_settings()
    if request.method == "POST":
        body = request.json or {}
        server_path = body.get("server_path")
        if not server_path:
            return jsonify({"ok": False, "error": "server_path required"}), 400
        settings.setdefault("rcon_targets", {})[server_path] = {
            "host": body.get("host", "127.0.0.1"),
            "port": int(body.get("port", 25575)),
            "password": body.get("password", ""),
            "label": body.get("label", ""),
        }
        save_settings(settings)
        return jsonify({"ok": True})
    out = {}
    for path, t in settings.get("rcon_targets", {}).items():
        out[path] = {"host": t.get("host"), "port": t.get("port"), "label": t.get("label"), "password_set": bool(t.get("password"))}
    return jsonify(out)


@app.route("/api/rcon/targets/delete", methods=["POST"])
@require_auth
def rcon_targets_delete():
    body = request.json or {}
    server_path = body.get("server_path")
    settings = load_settings()
    settings.get("rcon_targets", {}).pop(server_path, None)
    save_settings(settings)
    return jsonify({"ok": True})


@app.route("/api/rcon/command", methods=["POST"])
@require_auth
def rcon_command():
    body = request.json or {}
    server_path = body.get("server_path")
    command = (body.get("command") or "").strip()
    if not command:
        return jsonify({"ok": False, "error": "No command given."}), 400
    if PREVIEW:
        return jsonify({"ok": True, "response": f"[preview] ran: {command}"})
    settings = load_settings()
    target = settings.get("rcon_targets", {}).get(server_path)
    if not target or not target.get("password"):
        return jsonify({"ok": False, "error": "No RCON connection configured for this server yet."}), 400
    try:
        response = _rcon_command(target.get("host", "127.0.0.1"), int(target.get("port", 25575)),
                                  target["password"], command)
        return jsonify({"ok": True, "response": response})
    except (RconError, OSError, Exception) as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ---------------------------------------------------------------------------
# Whitelist / ops / ban manager — a small UI over whitelist.json, ops.json,
# banned-players.json, and banned-ips.json, which all live directly in a
# server's own root directory (the same "server_path" Anvil Mod Manager
# tracks). Includes a Mojang username -> UUID lookup so people don't have to
# hand-craft UUIDs.
# ---------------------------------------------------------------------------

PLAYER_FILE_NAMES = {
    "whitelist": "whitelist.json",
    "ops": "ops.json",
    "banned-players": "banned-players.json",
    "banned-ips": "banned-ips.json",
}


def _player_file_path(server_path, kind):
    if kind not in PLAYER_FILE_NAMES:
        raise ValueError("unknown player file kind")
    return Path(server_path) / PLAYER_FILE_NAMES[kind]


def _mojang_uuid_lookup(username):
    """Resolves a Minecraft username to its UUID via Mojang's public API.
    Returns (uuid_with_dashes, canonical_name) or (None, None) if not found."""
    try:
        r = requests.get(f"https://api.mojang.com/users/profiles/minecraft/{username}", timeout=8)
        if r.status_code != 200:
            return None, None
        data = r.json()
        raw = data.get("id", "")
        if len(raw) != 32:
            return None, None
        dashed = f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
        return dashed, data.get("name", username)
    except requests.RequestException:
        return None, None


@app.route("/api/playerfiles/<kind>")
@require_auth
def playerfiles_get(kind):
    server_path = request.args.get("server_path", "")
    if PREVIEW:
        return jsonify({"ok": True, "entries": []})
    try:
        f = _player_file_path(server_path, kind)
    except ValueError:
        return jsonify({"ok": False, "error": "Unknown file type."}), 400
    if not f.exists():
        return jsonify({"ok": True, "entries": []})
    try:
        return jsonify({"ok": True, "entries": json.loads(f.read_text())})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't parse {f.name}: {e}"}), 500


@app.route("/api/playerfiles/<kind>/add", methods=["POST"])
@require_auth
def playerfiles_add(kind):
    body = request.json or {}
    server_path = body.get("server_path", "")
    if PREVIEW:
        return jsonify({"ok": True, "preview": True})
    try:
        f = _player_file_path(server_path, kind)
    except ValueError:
        return jsonify({"ok": False, "error": "Unknown file type."}), 400

    entries = json.loads(f.read_text()) if f.exists() else []

    if kind == "banned-ips":
        ip = body.get("ip", "").strip()
        if not ip:
            return jsonify({"ok": False, "error": "IP address required."}), 400
        entries = [e for e in entries if e.get("ip") != ip]
        entries.append({"ip": ip, "created": time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime()),
                         "source": "Anvil Server Manager", "expires": "forever",
                         "reason": body.get("reason", "Banned by an operator.")})
    else:
        username = (body.get("username") or "").strip()
        if not username:
            return jsonify({"ok": False, "error": "Username required."}), 400
        uuid, canonical_name = _mojang_uuid_lookup(username)
        if not uuid:
            return jsonify({"ok": False, "error": f"Couldn't find a Mojang account named '{username}'."}), 404
        entries = [e for e in entries if e.get("uuid") != uuid]
        entry = {"uuid": uuid, "name": canonical_name}
        if kind == "ops":
            entry.update({"level": int(body.get("level", 4)), "bypassesPlayerLimit": False})
        elif kind == "banned-players":
            entry.update({"created": time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime()),
                          "source": "Anvil Server Manager", "expires": "forever",
                          "reason": body.get("reason", "Banned by an operator.")})
        entries.append(entry)

    f.write_text(json.dumps(entries, indent=2))
    return jsonify({"ok": True, "entries": entries})


@app.route("/api/playerfiles/<kind>/remove", methods=["POST"])
@require_auth
def playerfiles_remove(kind):
    body = request.json or {}
    server_path = body.get("server_path", "")
    key = body.get("uuid") or body.get("ip")
    if PREVIEW:
        return jsonify({"ok": True, "preview": True})
    try:
        f = _player_file_path(server_path, kind)
    except ValueError:
        return jsonify({"ok": False, "error": "Unknown file type."}), 400
    if not f.exists():
        return jsonify({"ok": True, "entries": []})
    entries = json.loads(f.read_text())
    entries = [e for e in entries if e.get("uuid") != key and e.get("ip") != key]
    f.write_text(json.dumps(entries, indent=2))
    return jsonify({"ok": True, "entries": entries})


# ---------------------------------------------------------------------------
# Log / crash analyzer — tails a server's latest.log and pattern-matches
# common crash signatures into a plain-English note, so a bad boot doesn't
# require reading a full Java stack trace to diagnose.
# ---------------------------------------------------------------------------

LOG_SIGNATURES = [
    (r"OutOfMemoryError|Java heap space", "Out of memory — the server ran out of allocated RAM. Increase the -Xmx value in the server's launch settings, or reduce loaded chunks/plugins."),
    (r"duplicate mod id|Duplicate mod", "Two installed mods are registering the same mod ID — remove the duplicate/older copy."),
    (r"Address already in use", "Something else is already using this server's port — check for another running instance, or a leftover process from a crash."),
    (r"requires version .* of .* but (only|it was not found)", "A mod is missing a required dependency, or the installed dependency version doesn't match what this mod needs."),
    (r"Missing or unsupported mandatory dependenc", "A required mod dependency is missing entirely — check the mod's page for what else it needs installed."),
    (r"Exception in server tick loop", "The server crashed during normal operation (a 'tick') — usually a mod/plugin bug. Check the lines just above this for the actual exception."),
    (r"Corrupt(ed)? (NBT|region|chunk)", "Possible world/chunk corruption — restoring from a recent backup is usually safer than trying to repair this by hand."),
    (r"Watchdog", "The server was force-killed by its own watchdog for freezing (not responding) — often caused by a plugin/mod stuck in an infinite loop or heavy world generation."),
    (r"UnsupportedClassVersionError", "This server jar needs a newer Java version than what's currently running it."),
]


@app.route("/api/logs/tail")
@require_auth
def logs_tail():
    server_path = request.args.get("server_path", "")
    lines_n = min(int(request.args.get("lines", 200)), 2000)
    if PREVIEW:
        sample = ["[12:00:01] [Server thread/INFO]: Done (12.345s)! For help, type \"help\"",
                  "[12:04:22] [Server thread/INFO]: player123 joined the game"]
        return jsonify({"ok": True, "lines": sample, "findings": []})
    log_path = Path(server_path) / "logs" / "latest.log"
    if not log_path.exists():
        if _is_bedrock_world(server_path):
            return jsonify({"ok": False, "lines": [], "findings": [],
                             "error": ("Bedrock dedicated servers don't write logs/latest.log the way Java servers "
                                       "do — vanilla bedrock_server writes to stdout instead. Check Crafty's own "
                                       "console/log view for this server instead of this tab.")})
        return jsonify({"ok": False, "error": f"No log file found at {log_path}.", "lines": [], "findings": []})
    try:
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "lines": [], "findings": []}), 500

    tail = [l.rstrip("\n") for l in all_lines[-lines_n:]]
    findings = []
    for line in tail:
        for pattern, explanation in LOG_SIGNATURES:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append({"line": line.strip(), "explanation": explanation})
                break
    return jsonify({"ok": True, "lines": tail, "findings": findings})


# ---------------------------------------------------------------------------
# Backup Manager — restic-backed scheduled backups with restore-to-point.
# Supports a local second disk/path, or S3-compatible / Backblaze B2 remotes.
# ---------------------------------------------------------------------------

def _restic_env(settings):
    b = settings["backup"]
    env = dict(os.environ)
    env["RESTIC_PASSWORD"] = b.get("restic_password", "")
    if b["repo_type"] == "local":
        env["RESTIC_REPOSITORY"] = b["repo_path"]
    elif b["repo_type"] == "s3":
        env["RESTIC_REPOSITORY"] = f"s3:{b['s3_endpoint']}/{b['s3_bucket']}"
        env["AWS_ACCESS_KEY_ID"] = b.get("s3_access_key", "")
        env["AWS_SECRET_ACCESS_KEY"] = b.get("s3_secret_key", "")
    elif b["repo_type"] == "b2":
        env["RESTIC_REPOSITORY"] = f"b2:{b['b2_bucket']}"
        env["B2_ACCOUNT_ID"] = b.get("b2_account_id", "")
        env["B2_ACCOUNT_KEY"] = b.get("b2_account_key", "")
    return env


def _restic(args, settings, timeout=120):
    env = _restic_env(settings)
    return subprocess.run(["restic"] + args, capture_output=True, text=True, timeout=timeout, env=env)


@app.route("/api/backup/settings", methods=["GET", "POST"])
@require_auth
def backup_settings():
    settings = load_settings()
    if request.method == "POST":
        body = request.json or {}
        b = settings["backup"]
        for key in ("repo_type", "repo_path", "restic_password", "s3_bucket", "s3_endpoint",
                    "s3_access_key", "s3_secret_key", "b2_bucket", "b2_account_id", "b2_account_key"):
            if key in body:
                b[key] = body[key]
        if "source_paths" in body and isinstance(body["source_paths"], list):
            b["source_paths"] = body["source_paths"]
        if "keep_last" in body:
            b["keep_last"] = int(body["keep_last"])
        if "schedule_enabled" in body:
            b["schedule_enabled"] = bool(body["schedule_enabled"])
        if "schedule_interval_hours" in body:
            b["schedule_interval_hours"] = max(1, int(body["schedule_interval_hours"]))
        save_settings(settings)
        return jsonify({"ok": True})
    out = dict(settings["backup"])
    for secret in ("restic_password", "s3_secret_key", "b2_account_key"):
        out[secret + "_set"] = bool(out.pop(secret, ""))
    return jsonify(out)


@app.route("/api/backup/init", methods=["POST"])
@require_auth
def backup_init():
    """Initializes the restic repository. Safe to call again later —
    restic errors out harmlessly if it's already initialized."""
    settings = load_settings()
    if settings["backup"]["repo_type"] == "local":
        Path(settings["backup"]["repo_path"]).mkdir(parents=True, exist_ok=True)
    if PREVIEW:
        return jsonify({"ok": True, "preview": True})
    if shutil.which("restic") is None:
        return jsonify({"ok": False, "error": "restic isn't installed. Re-run install.sh, or: sudo apt-get install restic"}), 500
    r = _restic(["init"], settings, timeout=60)
    already = "already initialized" in (r.stderr or "").lower()
    if r.returncode != 0 and not already:
        return jsonify({"ok": False, "error": r.stderr.strip() or r.stdout.strip()}), 500
    return jsonify({"ok": True, "already_initialized": already})


def _run_backup(trigger="manual"):
    settings = load_settings()
    b = settings["backup"]
    history = load_backup_history()
    started = time.time()
    entry = {"trigger": trigger, "started_at": started, "ok": False, "error": None, "duration_s": None}

    if PREVIEW:
        entry.update({"ok": True, "duration_s": 1.2})
        history.append(entry)
        save_backup_history(history)
        ok, _ = notify_discord("backup_complete", f"✅ Backup completed ({trigger}, preview mode).")
        return entry

    try:
        existing_paths = [p for p in b["source_paths"] if Path(p).exists()]
        if not existing_paths:
            raise RuntimeError("None of the configured source paths exist on disk: " + ", ".join(b["source_paths"]))
        r = _restic(["backup"] + existing_paths + ["--tag", trigger], settings, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "restic backup failed")
        if b.get("keep_last"):
            _restic(["forget", "--keep-last", str(b["keep_last"]), "--prune"], settings, timeout=600)
        entry["ok"] = True
    except Exception as e:
        entry["error"] = str(e)
    entry["duration_s"] = round(time.time() - started, 1)

    history.append(entry)
    save_backup_history(history)
    settings["backup"]["last_run_at"] = started
    save_settings(settings)

    if entry["ok"]:
        notify_discord("backup_complete", f"✅ Backup completed ({trigger}) in {entry['duration_s']}s.")
    else:
        notify_discord("backup_failed", f"❌ Backup failed ({trigger}): {entry['error']}")
    return entry


@app.route("/api/backup/run", methods=["POST"])
@require_auth
def backup_run():
    entry = _run_backup(trigger="manual")
    return jsonify(entry)


@app.route("/api/backup/history")
@require_auth
def backup_history():
    return jsonify(list(reversed(load_backup_history()))[:30])


@app.route("/api/backup/snapshots")
@require_auth
def backup_snapshots():
    settings = load_settings()
    if PREVIEW:
        return jsonify({"ok": True, "snapshots": [
            {"id": "abc123de", "time": "2026-07-12T03:00:00Z", "paths": settings["backup"]["source_paths"]},
        ]})
    if shutil.which("restic") is None:
        return jsonify({"ok": False, "error": "restic isn't installed.", "snapshots": []}), 500
    r = _restic(["snapshots", "--json"], settings, timeout=30)
    if r.returncode != 0:
        return jsonify({"ok": False, "error": r.stderr.strip(), "snapshots": []}), 500
    try:
        raw = json.loads(r.stdout)
    except ValueError:
        raw = []
    snaps = [{"id": s.get("short_id"), "time": s.get("time"), "paths": s.get("paths", [])} for s in raw]
    return jsonify({"ok": True, "snapshots": list(reversed(snaps))})


@app.route("/api/backup/restore", methods=["POST"])
@require_auth
def backup_restore():
    """Restores a snapshot to a target directory — deliberately NEVER
    restores directly on top of /opt/crafty/servers, to avoid silently
    overwriting a live world. Restore to a staging folder, then move what
    you need into place by hand (or via Cockpit Navigator)."""
    body = request.json or {}
    snapshot_id = body.get("snapshot_id")
    if not snapshot_id:
        return jsonify({"ok": False, "error": "snapshot_id required"}), 400
    target = body.get("target_dir") or f"/opt/anvil-backups/restore-{snapshot_id}"
    if PREVIEW:
        return jsonify({"ok": True, "preview": True, "target_dir": target})
    settings = load_settings()
    Path(target).mkdir(parents=True, exist_ok=True)
    r = _restic(["restore", snapshot_id, "--target", target], settings, timeout=3600)
    if r.returncode != 0:
        return jsonify({"ok": False, "error": r.stderr.strip() or r.stdout.strip()}), 500
    return jsonify({"ok": True, "target_dir": target})


@app.route("/api/backup/chown", methods=["POST"])
@require_auth
def backup_chown():
    """Recursively fixes ownership of a directory — meant to be run right
    after a restore. Restic preserves the UID/GID that was on disk at
    backup time, which is frequently wrong on a *different* machine (e.g.
    restoring onto a fresh second server) or after a snapshot was made by a
    process running as a different user — either way Crafty (running as
    root in its container) then can't read/write the restored world,
    producing confusing permission errors that look unrelated to the
    restore itself. Defaults to root:root, which matches how this whole
    Anvil suite runs Crafty; override "owner" if your setup differs."""
    body = request.json or {}
    target = (body.get("target_dir") or "").strip()
    owner = (body.get("owner") or "root:root").strip()
    if not target:
        return jsonify({"ok": False, "error": "target_dir required"}), 400
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(:[A-Za-z0-9_.-]+)?", owner):
        return jsonify({"ok": False, "error": "owner must look like 'user' or 'user:group'"}), 400
    p = Path(target)
    # Guard rails: never let this run against the filesystem root or other
    # obviously-wrong paths, since this is a recursive chown running as root.
    if not p.is_absolute() or p == Path("/") or len(p.parts) < 2:
        return jsonify({"ok": False, "error": "Refusing to chown that path — it looks too broad."}), 400
    if PREVIEW:
        return jsonify({"ok": True, "preview": True, "target_dir": target, "owner": owner})
    if not p.exists():
        return jsonify({"ok": False, "error": f"{target} doesn't exist."}), 404
    r = subprocess.run(["chown", "-R", owner, str(p)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return jsonify({"ok": False, "error": r.stderr.strip() or r.stdout.strip()}), 500
    return jsonify({"ok": True, "target_dir": target, "owner": owner})


# ---------------------------------------------------------------------------
# Backup scheduler — a simple interval loop, not real cron: every 60s it
# checks whether schedule_enabled is on and enough time has passed since
# last_run_at. Good enough for "back this up every N hours" without pulling
# in a cron dependency; restarts of this service just push the next run back
# by up to a minute, which doesn't matter for a daily/weekly cadence.
# ---------------------------------------------------------------------------

def _scheduler_loop():
    while True:
        try:
            settings = load_settings()
            b = settings["backup"]
            if b.get("schedule_enabled"):
                last = b.get("last_run_at") or 0
                interval_s = max(1, int(b.get("schedule_interval_hours", 24))) * 3600
                if time.time() - last >= interval_s:
                    _run_backup(trigger="scheduled")
        except Exception:
            pass
        time.sleep(60)


if not PREVIEW:
    threading.Thread(target=_scheduler_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6161, threaded=True)
