"""
Anvil Mod Manager
A companion dashboard for Crafty Controller servers that lets you search,
install, and update mods (Modrinth / CurseForge) and datapacks (Modrinth),
scoped to whatever Minecraft version + loader your server is currently on.

GitHub: https://github.com/TomCodes-sys/Anvil-Mod-Manager
"""
import hashlib
import io
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
import zipfile
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, send_from_directory, send_file, session

app = Flask(__name__, static_folder="static", template_folder="templates")

# ---------------------------------------------------------------------------
# Auth — shared with Anvil Server Installer and Anvil Server Manager. Mod
# Manager never had a token gate at all before (it only ever touched mod
# files, not system-level commands); this brings it in line with "one login
# for all three AnvilMC dashboards" rather than leaving it open on the LAN
# while its siblings are gated.
# ---------------------------------------------------------------------------

TOKEN_PATH = "/etc/anvilmc/token"

# Mod Manager runs as a normal (non-root) user, unlike its siblings — so its
# session secret and externally-persisted Crafty API token live in its own
# writable subdirectory rather than the shared root, which stays root-owned.
OWN_ETC_DIR = Path("/etc/anvilmc/mod-manager")
SESSION_SECRET_PATH = OWN_ETC_DIR / "session_secret"


def get_or_create_secret_key(path):
    """Same reasoning as the other two apps: a fresh secret_key on every
    restart would silently log everyone out even though nothing about the
    token changed. Persisted to disk so sessions survive restarts."""
    try:
        with open(path) as f:
            existing = f.read().strip()
            if existing:
                return existing
    except FileNotFoundError:
        pass
    new_secret = secrets.token_hex(32)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(new_secret)
        os.chmod(path, 0o600)
    except OSError:
        pass  # e.g. preview mode with no write access — falls back to in-memory only
    return new_secret


app.secret_key = get_or_create_secret_key(SESSION_SECRET_PATH)
from datetime import timedelta as _timedelta
app.config["PERMANENT_SESSION_LIFETIME"] = _timedelta(minutes=30)

# PAM login fallback — identical ctypes-based approach as the other two apps
# (see Anvil Server Installer's app.py for the full explanation of why this
# replaced the python-pam package). The "anvilmc-auth" PAM service file is
# shared across all three and created by install.sh (or lazily here too, if
# it's somehow missing).
import ctypes
import ctypes.util

_PAM_SERVICE_NAME = "anvilmc-auth"
_PAM_SERVICE_PATH = f"/etc/pam.d/{_PAM_SERVICE_NAME}"
_PAM_SERVICE_CONTENTS = """# Managed by AnvilMC — minimal PAM service for the dashboards' username/
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
        pass  # Mod Manager runs non-root, so this only succeeds if install.sh hasn't already created it as root


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


def verify_credentials(username, password):
    return _pam_authenticate(username, password)


def get_token():
    try:
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


UNAUTHORIZED_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anvil Mod Manager &mdash; sign in</title>
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
    <p class="sub">This dashboard needs the same token as the other AnvilMC apps, or a server login.</p>
    <div class="tabs">
      <button id="tab-token" class="active" onclick="showPane('token')">I have my token</button>
      <button id="tab-pam" onclick="showPane('pam')">Use my server login</button>
    </div>
    <div id="pane-token" class="pane active">
      <label>Token</label>
      <input id="in-token" type="text" placeholder="the same token as the other AnvilMC dashboards" autofocus>
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
      to print the links for all three AnvilMC dashboards again.
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
    """Kept as a decorator for symmetry with the other two apps, but the
    actual gate is applied via before_request below — with 34 routes here,
    decorating each one individually is one missed route away from leaving
    something unintentionally open."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapped


@app.before_request
def _enforce_auth():
    if PREVIEW:
        return None
    if request.path == "/api/auth/login" or request.path.startswith("/static/"):
        return None
    expected = get_token()
    if not expected:
        return None  # no token configured yet — don't lock the operator out
    if session.get("authed"):
        return None
    supplied = request.args.get("token") or request.headers.get("X-Anvil-Token")
    if supplied and secrets.compare_digest(supplied, expected):
        session["authed"] = True
        session.permanent = True
        return None
    pam_hint = ("No token handy at all? Use the \"Use my server login\" tab to sign in with your "
                "Linux username/password instead." if _PAM else
                "This server's PAM library couldn't be loaded, so username/password sign-in isn't "
                "available here — you'll need the token.")
    return Response(UNAUTHORIZED_PAGE.format(pam_hint=pam_hint), status=401, mimetype="text/html")


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if PREVIEW:
        return jsonify({"ok": True})
    body = request.get_json(force=True) or {}
    mode = body.get("mode")
    if mode == "token":
        expected = get_token()
        supplied = (body.get("token") or "").strip()
        if expected and supplied and secrets.compare_digest(supplied, expected):
            session["authed"] = True
            session.permanent = True
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
            session.permanent = True
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Invalid username or password."}), 401
    return jsonify({"ok": False, "error": "Unknown login mode."}), 400


DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"

# Local Windows demo runs only ("Start Preview Demo.bat") — distinct from the
# in-app preview_mode settings toggle below, which is for a REAL deployed
# instance simulating installs/updates without touching disk. This one just
# skips the auth gate entirely for someone demoing on their own machine.
PREVIEW = os.environ.get("ANVIL_MOD_MANAGER_PREVIEW", "0") == "1"

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"
CF_MINECRAFT_GAME_ID = 432
CF_MODS_CLASS_ID = 6

# Self-update (GitHub) — see check_for_updates() below.
GITHUB_REPO = "TomCodes-sys/Anvil-MC"
INSTALL_DIR = Path(__file__).parent

LOADER_MARKERS = {
    "fabric": ["fabric-server-launch.jar", "libraries/net/fabricmc", ".fabric-installer"],
    "quilt": ["quilt-server-launch.jar", "libraries/org/quiltmc"],
    # neoforge is checked before forge: NeoForge servers ship the exact same
    # run.sh launch script Forge does (it's a fork of Forge's tooling), so if
    # forge's markers included anything that ambiguous, a NeoForge server
    # would get misidentified as Forge before this line is ever reached.
    # Forge and NeoForge builds are NOT interchangeable on Modrinth/CurseForge,
    # so getting this wrong means silently offering mods that won't load.
    "neoforge": ["libraries/net/neoforged"],
    "forge": ["libraries/net/minecraftforge", "forge-installer"],
    "paper": ["libraries/io/papermc/paper"],
    "purpur": ["purpur.jar", "libraries/org/purpurmc"],
    "spigot": ["spigot.jar", "libraries/org/spigotmc"],
    "folia": ["libraries/io/papermc/folia"],
    "bukkit": ["craftbukkit.jar"],
}

# Loaders that install .jar mods into mods/ (Fabric-family + Forge-family).
MOD_LOADERS = {"fabric", "quilt", "forge", "neoforge"}
# Platforms that install .jar plugins into plugins/ (Bukkit-family).
PLUGIN_PLATFORMS = {"paper", "purpur", "spigot", "bukkit", "folia"}

HEADERS_UA = {"User-Agent": "anvil-mod-manager/1.0 (+https://github.com/TomCodes-sys/Anvil-Mod-Manager)"}


class LookupUnavailable(Exception):
    """Raised when a Modrinth/CurseForge version lookup couldn't be completed
    at all (network error, timeout, rate limit) — distinct from the lookup
    succeeding and simply finding no compatible build. Check for Updates uses
    this to mark an item 'check_failed' (retryable) instead of lumping it in
    with 'no_compatible_version' (which means "nothing exists for this MC
    version," a permanent state that a retry won't fix)."""
    pass

# Where the Anvil Server Installer mounts Crafty's own "servers" volume on the
# host (see Anvil Server Installer's CRAFTY_SCRIPT: -v /opt/crafty/servers:/crafty/servers).
# Each Crafty server's folder lives at <this>/<crafty_server_id>. Used to turn a
# Crafty server picked from a dropdown straight into a local path, with zero
# manual browsing — overridable in Settings for non-default installs.
CRAFTY_SERVERS_ROOT_DEFAULT = "/opt/crafty/servers"


# --------------------------------------------------------------------------
# Settings (CurseForge API key, saved server profiles)
# --------------------------------------------------------------------------
# The Crafty URL/token are the one thing in settings.json genuinely painful
# to lose: the Installer's "Danger Zone" uninstall for this app wipes
# data/settings.json along with everything else — meaning a reinstall used
# to mean re-typing the Crafty API token from scratch. This mirrors it into
# a file OUTSIDE the install directory that survives an uninstall.
#
# It's also the SAME file Anvil Server Manager uses — install.sh sets up
# /etc/anvilmc with a setgid bit and both apps' users in the "anvilmc" group,
# so entering the Crafty URL/token in either dashboard fills in the other
# too, instead of typing it in twice for what's really one connection.
EXTERNAL_CRAFTY_API_PATH = Path("/etc/anvilmc/crafty_api.json")


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
        os.chmod(EXTERNAL_CRAFTY_API_PATH, 0o660)  # group-writable — shared with Server Manager's different user, not owner-only
    except OSError:
        pass  # e.g. /etc/anvilmc doesn't exist yet on an install from before this change


def load_settings():
    if SETTINGS_FILE.exists():
        data = json.loads(SETTINGS_FILE.read_text())
        data.setdefault("preview_mode", False)
        data.setdefault("crafty_url", "")
        data.setdefault("crafty_token", "")
        data.setdefault("crafty_server_id", "")
        data.setdefault("crafty_servers_root", CRAFTY_SERVERS_ROOT_DEFAULT)
    else:
        data = {"curseforge_api_key": "", "servers": {}, "preview_mode": False,
                "crafty_url": "", "crafty_token": "", "crafty_server_id": "",
                "crafty_servers_root": CRAFTY_SERVERS_ROOT_DEFAULT}
    # If this is a fresh/reinstalled settings.json (no Crafty URL/token yet)
    # but the externally-persisted copy has one, restore it automatically —
    # this is exactly the "survived an uninstall" case.
    if not data.get("crafty_url") and not data.get("crafty_token"):
        ext_url, ext_token = _load_external_crafty_api()
        if ext_url or ext_token:
            data["crafty_url"], data["crafty_token"] = ext_url, ext_token
    return data


DEMO_SERVER_PATH = "demo://preview-server"


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    if settings.get("crafty_url") or settings.get("crafty_token"):
        _save_external_crafty_api(settings.get("crafty_url", ""), settings.get("crafty_token", ""))


def server_key(path):
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def server_data_file(path):
    return DATA_DIR / f"server_{server_key(path)}.json"


def load_server_data(path):
    f = server_data_file(path)
    if f.exists():
        data = json.loads(f.read_text())
        data.setdefault("plugins", [])
        data.setdefault("crafty_server_id", "")
        data.setdefault("crafty_server_name", "")
        data.setdefault("crafty_link_manual", False)
        data.setdefault("mc_version_source", "")
        return data
    return {"server_path": path, "mc_version": "", "mc_version_source": "", "loader": "vanilla",
            "world_name": "world", "mods": [], "datapacks": [], "plugins": [],
            "crafty_server_id": "", "crafty_server_name": "", "crafty_link_manual": False}


def save_server_data(path, data):
    server_data_file(path).write_text(json.dumps(data, indent=2))


# --------------------------------------------------------------------------
# Filesystem helpers (browsing the Crafty install on this machine)
# --------------------------------------------------------------------------
@app.route("/api/browse")
def browse():
    path = request.args.get("path") or str(Path.home())
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return jsonify({"error": "Path not found or not a directory"}), 400
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                entries.append({"name": child.name, "path": str(child)})
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    return jsonify({"current": str(p), "parent": str(p.parent), "entries": entries})


def detect_loader(server_path: Path):
    for loader, markers in LOADER_MARKERS.items():
        for marker in markers:
            if (server_path / marker).exists():
                return loader
    # fallback: scan root for jar names
    for jar in server_path.glob("*.jar"):
        name = jar.name.lower()
        if "fabric" in name:
            return "fabric"
        if "quilt" in name:
            return "quilt"
        if "forge" in name and "neoforge" not in name:
            return "forge"
        if "neoforge" in name:
            return "neoforge"
        if "purpur" in name:
            return "purpur"
        if "paper" in name:
            return "paper"
        if "spigot" in name:
            return "spigot"
        if "craftbukkit" in name or "bukkit" in name:
            return "bukkit"
        if "folia" in name:
            return "folia"
    return "vanilla"


_VERSION_RE = re.compile(r"\b(1\.\d{1,2}(?:\.\d{1,2})?)\b")


def detect_mc_version(server_path: Path):
    """Returns (version, source). source is one of:
      "jar"            — read straight out of an embedded version.json (highest
                         confidence; covers vanilla, Paper, Purpur, Spigot, Bukkit,
                         Folia, and Fabric/Quilt setups that keep the plain vanilla
                         server.jar alongside the loader jar)
      "forge"          — read from Forge/NeoForge's own libraries/net/minecraft/server
                         folder, which their installers always create — this is what
                         makes Forge/NeoForge detection work at all, since their
                         launch jar doesn't embed version.json itself
      "versions_folder" — read from a versions/<mc_version>/ folder, which Crafty
                         itself creates (one per version it's ever downloaded for
                         this server) — same confidence tier as "forge" above, since
                         it's a folder name Crafty controls rather than a filename
      "guess"          — a version-shaped number pulled out of a jar filename (e.g.
                         forge-1.20.1-47.2.20-installer.jar); lowest confidence of the
                         four, flagged as such so the UI can tell the person to
                         double-check it
      ""               — nothing found; falls back to whatever's already saved, or blank
    """
    # 1) Embedded version.json — the reliable case. Sorted newest-first by
    #    mtime: if more than one .jar sits in the server folder (a leftover
    #    from a previous version, a backup Crafty kept, an installer jar
    #    alongside the real server jar), unsorted glob() iteration is
    #    filesystem-order-dependent — effectively random — and could return
    #    a stale jar's version instead of the one that's actually current.
    #    The most recently modified jar is the best available signal for
    #    "what Crafty most recently installed here."
    jars_by_mtime = sorted(server_path.glob("*.jar"), key=lambda p: p.stat().st_mtime, reverse=True)
    for jar in jars_by_mtime:
        try:
            with zipfile.ZipFile(jar) as z:
                if "version.json" in z.namelist():
                    info = json.loads(z.read("version.json"))
                    if info.get("id"):
                        return info["id"], "jar"
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
            continue

    # 2) Forge/NeoForge: their installer lays down
    #    libraries/net/minecraft/server/<mc_version>/... every time, regardless of
    #    Forge/NeoForge version, since it's just the vanilla server jar they patch
    #    against. This is far more reliable than trying to parse it out of the
    #    Forge jar's own filename (whose version numbering doesn't match MC's).
    #    If multiple version folders exist (old ones aren't always cleaned up
    #    on update), the most recently modified one wins — picking the
    #    numerically highest would get a downgrade wrong, and doesn't reflect
    #    "what's actually running" as directly as "what was touched last."
    mc_server_libs = server_path / "libraries" / "net" / "minecraft" / "server"
    if mc_server_libs.is_dir():
        candidates = [d for d in mc_server_libs.iterdir() if d.is_dir() and _VERSION_RE.fullmatch(d.name)]
        if candidates:
            candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
            return candidates[0].name, "forge"

    # 3) Crafty's own "versions" folder: Crafty caches the server jar it
    #    downloaded under versions/<mc_version>/ (one subfolder per version
    #    it's ever installed for this server), named exactly after the MC
    #    version — same confidence tier as the Forge/NeoForge check above,
    #    since it's a folder name Crafty itself controls rather than
    #    anything parsed out of a jar's filename. Newest-mtime wins here too,
    #    for the same reason as above.
    versions_dir = server_path / "versions"
    if versions_dir.is_dir():
        candidates = [d for d in versions_dir.iterdir() if d.is_dir() and _VERSION_RE.fullmatch(d.name)]
        if candidates:
            candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
            return candidates[0].name, "versions_folder"

    # 4) Last resort: a version-shaped number in a jar filename in the root
    #    (e.g. "forge-1.20.1-47.2.20-installer.jar", "paper-1.20.1-196.jar"). Loosely
    #    confident at best — flagged to the caller as "guess" so the UI can show it
    #    as unverified rather than presenting it with the same confidence as the two
    #    checks above. Reuses the same newest-first jar list from tier 1.
    for jar in jars_by_mtime:
        m = _VERSION_RE.search(jar.name)
        if m:
            return m.group(1), "guess"

    return "", ""


# Bedrock Edition servers Crafty manages always ship this file (it's part of
# the official bedrock_server download) — Java-only tooling like this app has
# no business showing them in server pickers, since there's no such thing as
# a Modrinth/CurseForge mod for a Bedrock world.
BEDROCK_MARKER_FILE = "bedrock_server_how_to.html"


def is_bedrock_world(server_path) -> bool:
    return (Path(server_path) / BEDROCK_MARKER_FILE).exists()


def detect_world_name(server_path: Path):
    props = server_path / "server.properties"
    if props.exists():
        for line in props.read_text(errors="ignore").splitlines():
            if line.startswith("level-name="):
                val = line.split("=", 1)[1].strip()
                return val or "world"
    return "world"


@app.route("/api/server/version_drift")
def api_version_drift():
    """Cheap, read-only check for the periodic poll on the frontend: has the
    on-disk MC version moved since we last saved it, for the currently active
    server? Unlike /api/server/detect, this is meant to be called every so
    often in the background without the person doing anything — so it skips
    loader/world detection and only does the one filesystem scan that's
    actually needed to answer "did the version change." Previously,
    version-change detection only ever ran when someone re-clicked the
    server picker — if the jar got swapped out in the background and nobody
    touched the picker again, it went unnoticed indefinitely."""
    path = request.args.get("path", "")
    p = Path(path)
    if not path or not p.exists() or is_bedrock_world(p):
        return jsonify({"version_changed": False})
    mc_version, source = detect_mc_version(p)
    data = load_server_data(path)
    previous_version = data.get("mc_version")
    version_changed = bool(previous_version and mc_version and previous_version != mc_version)
    if version_changed:
        # Detection is authoritative (it reflects the actual jar/versions
        # folder on disk), so correct the stored value now rather than just
        # reporting the drift and leaving the old one in place — otherwise a
        # "check updates" click straight after this toast would still
        # compare mods against the stale version.
        data["mc_version"] = mc_version
        data["mc_version_source"] = source
        save_server_data(path, data)
    return jsonify({
        "version_changed": version_changed,
        "previous_mc_version": previous_version if version_changed else None,
        "current_mc_version": mc_version if version_changed else None,
    })


@app.route("/api/server/detect")
def api_detect():
    path = request.args.get("path", "")
    p = Path(path)
    if not p.exists():
        return jsonify({"error": "Path not found"}), 400
    if is_bedrock_world(p):
        return jsonify({"error": "This looks like a Bedrock Edition world (found "
                                  f"{BEDROCK_MARKER_FILE}) — Bedrock doesn't support Modrinth/CurseForge "
                                  "mods, so there's nothing for this app to manage here."}), 400
    mc_version, mc_version_source = detect_mc_version(p)
    previous = load_server_data(path)
    version_changed = bool(
        previous.get("mc_version") and mc_version and previous["mc_version"] != mc_version
    )
    return jsonify({
        "mc_version": mc_version,
        "mc_version_source": mc_version_source,
        "loader": detect_loader(p),
        "world_name": detect_world_name(p),
        "has_mods_folder": (p / "mods").exists(),
        "version_changed": version_changed,
        "previous_mc_version": previous.get("mc_version") if version_changed else None,
    })


@app.route("/api/server/config", methods=["GET", "POST"])
def api_server_config():
    if request.method == "POST":
        body = request.json
        path = body["server_path"]
        data = load_server_data(path)
        data.update({
            "server_path": path,
            "mc_version": body.get("mc_version", data["mc_version"]),
            "mc_version_source": "confirmed" if body.get("mc_version") else data.get("mc_version_source", ""),
            "loader": body.get("loader", data["loader"]),
            "world_name": body.get("world_name", data.get("world_name", "world")),
            "name": body.get("name", data.get("name", Path(path).name)),
        })
        save_server_data(path, data)
        settings = load_settings()
        settings["servers"][server_key(path)] = {"path": path, "name": data["name"]}
        save_settings(settings)
        return jsonify(data)
    else:
        path = request.args.get("path", "")
        return jsonify(load_server_data(path))


@app.route("/api/servers")
def api_servers():
    settings = load_settings()
    out = []
    for key, meta in settings.get("servers", {}).items():
        d = load_server_data(meta["path"])
        out.append(d)
    return jsonify(out)


@app.route("/api/servers/status")
def api_servers_status():
    """Live running-status for every locally-tracked server that's linked to
    Crafty — used to badge the quick-switcher and gate downloads. Servers
    that aren't linked come back with running: null (unknown, not blocked)."""
    settings = load_settings()
    out = {}
    for key, meta in settings.get("servers", {}).items():
        data = load_server_data(meta["path"])
        out[meta["path"]] = crafty_running_status(data, settings)
    return jsonify(out)


@app.route("/api/settings/active_server", methods=["GET", "POST"])
def active_server():
    """Remembers which server was last selected so it's auto-selected again
    next time the dashboard is opened — handy when you manage more than one
    Crafty server and don't want to re-pick every visit."""
    settings = load_settings()
    if request.method == "POST":
        settings["active_server"] = request.json.get("server_path", "")
        save_settings(settings)
        return jsonify({"ok": True})
    path = settings.get("active_server", "")
    return jsonify({"server_path": path, "server": load_server_data(path) if path else None})


# --------------------------------------------------------------------------
# Settings: CurseForge key
# --------------------------------------------------------------------------
@app.route("/api/settings/curseforge_key", methods=["GET", "POST"])
def cf_key():
    settings = load_settings()
    if request.method == "POST":
        settings["curseforge_api_key"] = request.json.get("key", "")
        save_settings(settings)
        return jsonify({"ok": True})
    return jsonify({"key": settings.get("curseforge_api_key", "")})


@app.route("/api/settings/preview_mode", methods=["GET", "POST"])
def preview_mode_setting():
    settings = load_settings()
    if request.method == "POST":
        settings["preview_mode"] = bool(request.json.get("enabled", False))
        save_settings(settings)
        return jsonify({"ok": True, "preview_mode": settings["preview_mode"]})
    return jsonify({"preview_mode": settings.get("preview_mode", False)})


# --------------------------------------------------------------------------
# Settings: Crafty Controller API (for the "restart server" hook)
# --------------------------------------------------------------------------
@app.route("/api/settings/crafty", methods=["GET", "POST"])
def crafty_settings():
    settings = load_settings()
    if request.method == "POST":
        body = request.json or {}
        settings["crafty_url"] = body.get("url", settings.get("crafty_url", "")).rstrip("/")
        settings["crafty_token"] = body.get("token", settings.get("crafty_token", ""))
        settings["crafty_server_id"] = body.get("server_id", settings.get("crafty_server_id", ""))
        settings["crafty_servers_root"] = (body.get("servers_root", settings.get("crafty_servers_root", CRAFTY_SERVERS_ROOT_DEFAULT)) or CRAFTY_SERVERS_ROOT_DEFAULT).rstrip("/")
        save_settings(settings)
        return jsonify({"ok": True})
    return jsonify({
        "url": settings.get("crafty_url", ""),
        "token_set": bool(settings.get("crafty_token")),
        "server_id": settings.get("crafty_server_id", ""),
        "servers_root": settings.get("crafty_servers_root", CRAFTY_SERVERS_ROOT_DEFAULT),
    })


@app.route("/api/network/local_ip")
def network_local_ip():
    """Best-effort local LAN IP of this machine, for auto-filling the Crafty
    URL field. Opens a UDP socket toward a public address without sending
    any traffic (UDP connect is just a routing-table lookup) purely to see
    which local interface/IP the OS would use."""
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
    return jsonify({"ok": True, "ip": ip, "suggested_url": f"https://{ip}:8443"})



def crafty_restart():
    """Optional hook: triggers a restart through Crafty's own REST API right
    after an install/update, so the change actually takes effect. Requires
    the Crafty URL/API token to be set in Settings, and the target server to
    be linked to a Crafty server (Saved servers card)."""
    body = request.json or {}
    settings = load_settings()
    url, token = settings.get("crafty_url"), settings.get("crafty_token")
    server_path = body.get("server_path") or settings.get("active_server", "")
    server_id = None
    if server_path:
        server_id = load_server_data(server_path).get("crafty_server_id")
    server_id = server_id or settings.get("crafty_server_id")  # legacy fallback
    if not (url and token and server_id):
        return jsonify({"ok": False, "error": "Set your Crafty URL + API token in Settings, and link this server to a Crafty server first."}), 400
    if settings.get("preview_mode"):
        return jsonify({"ok": True, "preview": True, "note": "Preview mode — restart simulated, Crafty was not contacted."})
    try:
        r = requests.post(
            f"{url}/api/v2/servers/{server_id}/action/restart_server",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15, verify=False,
        )
        if r.status_code >= 400:
            return jsonify({"ok": False, "error": f"Crafty returned HTTP {r.status_code}: {r.text[:200]}"}), 502
        return jsonify({"ok": True})
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Couldn't reach Crafty: {e}"}), 502


# --------------------------------------------------------------------------
# Crafty server discovery — lists Crafty's *own* servers (by name, not raw
# UUID) so each locally-tracked Anvil server can be linked to the right one.
# That link is what lets us check "is this actually running right now?"
# before letting mods/plugins/datapacks be downloaded to it.
# --------------------------------------------------------------------------

def _crafty_configured(settings):
    return bool(settings.get("crafty_url") and settings.get("crafty_token"))


@app.route("/api/crafty/health")
def crafty_health():
    """Lightweight reachability check for the topbar health dot — separate
    from /api/crafty/servers (below) so polling it every 20s doesn't also
    pull the full server list and stats for every server each time."""
    settings = load_settings()
    if not _crafty_configured(settings):
        return jsonify({"configured": False, "reachable": False})
    if settings.get("preview_mode"):
        return jsonify({"configured": True, "reachable": True, "preview": True})
    url, token = settings["crafty_url"], settings["crafty_token"]
    try:
        r = requests.get(f"{url}/api/v2/servers", headers={"Authorization": f"Bearer {token}"},
                          timeout=6, verify=False)
        return jsonify({"configured": True, "reachable": r.status_code < 400})
    except requests.RequestException:
        return jsonify({"configured": True, "reachable": False})


@app.route("/api/crafty/servers")
def crafty_servers():
    """Live list of every server Crafty knows about: {id, name, running}.
    Used to populate the link/override picker and to auto-match local
    server profiles to their Crafty counterpart by name. Also used directly
    by the "Pick your server" dropdown on Server Setup — each entry says
    whether it's already been imported locally and whether its guessed
    folder actually exists, so the dropdown can be the *entire* setup flow."""
    settings = load_settings()
    if not _crafty_configured(settings):
        return jsonify({"ok": False, "error": "Set your Crafty URL and API token in Settings first.", "servers": []}), 400

    linked_paths = {}
    for meta in settings.get("servers", {}).values():
        d = load_server_data(meta["path"])
        if d.get("crafty_server_id"):
            linked_paths[d["crafty_server_id"]] = meta["path"]

    if settings.get("preview_mode"):
        demo = [
            {"id": "demo-1111", "name": "Survival (preview)", "running": False},
            {"id": "demo-2222", "name": "Creative Build (preview)", "running": True},
        ]
        for s in demo:
            s["already_added"] = s["id"] in linked_paths
        return jsonify({"ok": True, "preview": True, "servers": demo})

    url, token = settings["crafty_url"], settings["crafty_token"]
    try:
        r = requests.get(f"{url}/api/v2/servers", headers={"Authorization": f"Bearer {token}"},
                         timeout=15, verify=False)
        if r.status_code >= 400:
            return jsonify({"ok": False, "error": f"Crafty returned HTTP {r.status_code}: {r.text[:200]}", "servers": []}), 502
        body = r.json()
        servers = body.get("data", []) if isinstance(body, dict) else []
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Couldn't reach Crafty: {e}", "servers": []}), 502
    except ValueError:
        return jsonify({"ok": False, "error": "Crafty returned something unexpected.", "servers": []}), 502

    root = settings.get("crafty_servers_root", CRAFTY_SERVERS_ROOT_DEFAULT)
    out = []
    for s in servers:
        sid = s.get("server_id") or s.get("server_uuid")
        if not sid:
            continue
        running = None
        try:
            sr = requests.get(f"{url}/api/v2/servers/{sid}/stats",
                               headers={"Authorization": f"Bearer {token}"}, timeout=10, verify=False)
            if sr.status_code < 400:
                running = _extract_running(sr.json())
        except (requests.RequestException, ValueError):
            pass
        guessed_path = str(Path(root) / sid)
        if Path(guessed_path).is_dir() and is_bedrock_world(guessed_path):
            # Bedrock worlds have no mods/plugins/datapacks to manage here —
            # leaving them out of the picker avoids someone importing one and
            # hitting "no compatible version" for everything they search for.
            continue
        out.append({
            "id": sid,
            "name": s.get("server_name") or sid,
            "running": running,
            "already_added": sid in linked_paths,
            "existing_path": linked_paths.get(sid),
            "guessed_path": guessed_path,
            "path_exists": Path(guessed_path).is_dir(),
        })
    return jsonify({"ok": True, "servers": out})


@app.route("/api/crafty/select_server", methods=["POST"])
def crafty_select_server():
    """The whole "point this at your server" flow, collapsed into one click:
    given a Crafty server's id + name (from the dropdown on Server Setup),
    guess its folder from crafty_servers_root, auto-detect version/loader/
    world the same way manual browsing does, save the profile already
    linked to that Crafty server, and make it the active one. No folder
    picking, no separate confirm step — picking it from the dropdown *is*
    the confirmation. Manual browsing (Advanced) remains for setups that
    don't follow the default Anvil Server Installer layout."""
    body = request.json or {}
    settings = load_settings()
    if not _crafty_configured(settings):
        return jsonify({"ok": False, "error": "Set your Crafty URL and API token in Settings first."}), 400

    crafty_id = (body.get("crafty_server_id") or "").strip()
    crafty_name = (body.get("crafty_server_name") or "").strip()
    if not crafty_id:
        return jsonify({"ok": False, "error": "crafty_server_id required"}), 400

    if settings.get("preview_mode"):
        path = f"demo://crafty-{crafty_id}"
        data = load_server_data(path)
        data.update({"server_path": path, "name": crafty_name or crafty_id,
                      "mc_version": data.get("mc_version") or "1.20.1", "loader": data.get("loader") or "fabric",
                      "world_name": data.get("world_name") or "world",
                      "crafty_server_id": crafty_id, "crafty_server_name": crafty_name, "crafty_link_manual": True})
        save_server_data(path, data)
        settings["servers"][server_key(path)] = {"path": path, "name": data["name"]}
        settings["active_server"] = path
        save_settings(settings)
        return jsonify({"ok": True, "preview": True, "server": data})

    root = settings.get("crafty_servers_root", CRAFTY_SERVERS_ROOT_DEFAULT)
    guessed_path = str(Path(root) / crafty_id)
    p = Path(guessed_path)
    if not p.is_dir():
        return jsonify({
            "ok": False,
            "error": (f"Expected this server's files at {guessed_path}, but that folder doesn't exist. "
                      f"If Crafty isn't installed via the Anvil Server Installer's default layout, set the "
                      f"correct \"Crafty servers folder\" in Settings, or use \"Point at a folder manually\" below."),
            "guessed_path": guessed_path,
        }), 400

    if is_bedrock_world(p):
        return jsonify({"ok": False, "error": "This is a Bedrock Edition server — there's nothing for this app "
                                               "to manage (no Modrinth/CurseForge mods exist for Bedrock)."}), 400

    data = load_server_data(guessed_path)
    previous_version = data.get("mc_version")
    detected_version, detected_source = detect_mc_version(p)
    version_changed = bool(previous_version and detected_version and previous_version != detected_version)
    data.update({
        "server_path": guessed_path,
        "name": crafty_name or data.get("name") or p.name,
        "mc_version": detected_version or data.get("mc_version", ""),
        "mc_version_source": detected_source if detected_version else "",
        "loader": detect_loader(p),
        "world_name": detect_world_name(p),
        "crafty_server_id": crafty_id,
        "crafty_server_name": crafty_name,
        "crafty_link_manual": True,
    })
    save_server_data(guessed_path, data)
    settings["servers"][server_key(guessed_path)] = {"path": guessed_path, "name": data["name"]}
    settings["active_server"] = guessed_path
    save_settings(settings)
    return jsonify({
        "ok": True,
        "server": data,
        "version_changed": version_changed,
        "previous_mc_version": previous_version if version_changed else None,
    })





@app.route("/api/crafty/auto_match", methods=["POST"])
def crafty_auto_match():
    """Best-effort: for every locally-tracked server that isn't already
    manually linked, try to match it to a Crafty server by exact
    (case-insensitive) name. Ambiguous or no match = left alone for the
    person to pick manually via /api/server/link_crafty."""
    settings = load_settings()
    resp = crafty_servers()
    body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    if not body.get("ok"):
        return jsonify({"ok": False, "error": body.get("error", "Couldn't reach Crafty."), "matched": []}), 400

    crafty_list = body.get("servers", [])
    by_name = {}
    for cs in crafty_list:
        by_name.setdefault(cs["name"].strip().lower(), []).append(cs)

    matched = []
    for key, meta in settings.get("servers", {}).items():
        data = load_server_data(meta["path"])
        if data.get("crafty_link_manual"):
            continue  # don't clobber an explicit manual choice
        candidates = by_name.get((data.get("name") or "").strip().lower(), [])
        if len(candidates) == 1:
            data["crafty_server_id"] = candidates[0]["id"]
            data["crafty_server_name"] = candidates[0]["name"]
            save_server_data(meta["path"], data)
            matched.append({"server_path": meta["path"], "name": data.get("name"),
                             "crafty_server_id": candidates[0]["id"], "crafty_server_name": candidates[0]["name"]})
    return jsonify({"ok": True, "matched": matched, "crafty_servers": crafty_list})


@app.route("/api/server/link_crafty", methods=["POST"])
def link_crafty_server():
    """Sets (or clears, with an empty crafty_server_id) which real Crafty
    server a locally-tracked Anvil server profile corresponds to. This is
    the manual-override path — auto-match (by exact name) happens in
    api_servers() below and only fills this in when it isn't set yet."""
    body = request.json or {}
    path = body.get("server_path", "")
    if not path:
        return jsonify({"error": "server_path required"}), 400
    data = load_server_data(path)
    data["crafty_server_id"] = body.get("crafty_server_id", "") or ""
    data["crafty_server_name"] = body.get("crafty_server_name", "") or ""
    data["crafty_link_manual"] = bool(data["crafty_server_id"])
    save_server_data(path, data)
    return jsonify({"ok": True, "server": data})


def _extract_running(stats_json):
    """Crafty's /stats endpoint wraps its fields under a "data" key
    (e.g. {"status": "ok", "data": {"running": true, ...}}). Falls back to
    reading the top level too, in case a future/older Crafty version
    flattens the response."""
    payload = stats_json.get("data") if isinstance(stats_json.get("data"), dict) else stats_json
    return bool(payload.get("running"))


def crafty_running_status(data, settings=None):
    """Given a loaded server_data dict, returns:
      {"linked": bool, "crafty_name": str|None, "running": bool|None}
    running is None when we can't tell (not linked, or Crafty unreachable) —
    callers should NOT block downloads on an unknown status, only on a
    confirmed True."""
    settings = settings or load_settings()
    crafty_id = data.get("crafty_server_id")
    if not crafty_id or not _crafty_configured(settings):
        return {"linked": False, "crafty_name": None, "running": None}
    if settings.get("preview_mode"):
        return {"linked": True, "crafty_name": data.get("crafty_server_name") or crafty_id, "running": False}
    url, token = settings["crafty_url"], settings["crafty_token"]
    try:
        r = requests.get(f"{url}/api/v2/servers/{crafty_id}/stats",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10, verify=False)
        if r.status_code >= 400:
            return {"linked": True, "crafty_name": data.get("crafty_server_name") or crafty_id, "running": None}
        body = r.json()
        running = _extract_running(body)
        return {"linked": True, "crafty_name": data.get("crafty_server_name") or crafty_id, "running": running}
    except (requests.RequestException, ValueError):
        return {"linked": True, "crafty_name": data.get("crafty_server_name") or crafty_id, "running": None}


def require_server_stopped(server_path, settings=None):
    """Returns None if it's safe to write mods/plugins/datapacks to this
    server, or a (message, http_status) tuple to return early if it's
    confirmed running. Unknown status (not linked, or Crafty unreachable)
    is allowed through — we only ever block on a *confirmed* running state,
    never on uncertainty, since plenty of setups don't link Crafty at all."""
    data = load_server_data(server_path)
    status = crafty_running_status(data, settings)
    if status["running"] is True:
        name = status["crafty_name"] or "this server"
        return (f"'{name}' is currently running in Crafty. Stop it first, then install/update mods — "
                f"installing into a live server's mods folder can crash it or corrupt the install."), 409
    return None


@app.route("/api/demo/seed", methods=["POST"])
def demo_seed():
    """Create a fake server profile so search/install can be tried with no real
    Crafty folder. Always installs in preview (simulated) mode regardless of
    the global toggle, since there is no real folder to write into."""
    settings = load_settings()
    settings["preview_mode"] = True
    data = load_server_data(DEMO_SERVER_PATH)
    data.update({
        "server_path": DEMO_SERVER_PATH,
        "name": "Preview Server",
        "mc_version": "1.20.1",
        "loader": "fabric",
        "world_name": "world",
    })
    save_server_data(DEMO_SERVER_PATH, data)
    settings["servers"][server_key(DEMO_SERVER_PATH)] = {"path": DEMO_SERVER_PATH, "name": data["name"]}
    save_settings(settings)
    return jsonify(data)


# --------------------------------------------------------------------------
# Self-update — checks GitHub for a newer commit than what's on disk and
# lets the dashboard notify you (Crafty-style banner), same idea as the
# Anvil Server Installer. `git pull` only touches files tracked in this
# repo — data/ (your server profiles + settings) is git-ignored, and any
# mods/plugins/datapacks you've installed live on the Crafty server itself,
# not in this install directory — so nothing about your mod setup is
# touched by updating the dashboard app.
# --------------------------------------------------------------------------

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
            "User-Agent": "anvil-mod-manager",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        return {
            "sha": data.get("sha"),
            "message": (data.get("commit", {}).get("message", "").splitlines() or [""])[0],
            "html_url": data.get("html_url"),
        }
    except Exception:
        if branch == "main":
            return _github_latest_commit(repo, "master")
        return None


@app.route("/api/self_update_check")
def self_update_check():
    current = _git_head(INSTALL_DIR)
    latest = _github_latest_commit(GITHUB_REPO)
    if not current or not latest or not latest.get("sha"):
        return jsonify({"update_available": False, "checked": False,
                         "note": "Couldn't determine version info (not a git checkout, or GitHub unreachable)."})
    update_available = current != latest["sha"]
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
def self_update():
    """AnvilMC's update action now lives in one place — the Installer
    dashboard, which pulls the whole shared monorepo and restarts all three
    services in one go, instead of three separate self-updates each only
    touching their own subfolder (and each needing you to remember which
    dashboard's "Update" button to click)."""
    return jsonify({"ok": False, "redirect_to_installer": True,
                     "error": "Run updates from the Installer dashboard's Home tab — it updates all three AnvilMC apps together in one action."}), 400


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
@app.route("/api/search")
def api_search():
    source = request.args.get("source", "modrinth")
    query = request.args.get("query", "")
    content_type = request.args.get("type", "mod")  # mod | datapack | plugin
    mc_version = request.args.get("mc_version", "")
    # Comma-separated so the sidebar can multi-select (e.g. "fabric,quilt").
    loaders = [l for l in request.args.get("loaders", "").split(",") if l]
    offset = int(request.args.get("offset", 0))

    try:
        if source == "modrinth":
            return jsonify(search_modrinth(query, content_type, mc_version, loaders, offset))
        elif source == "curseforge":
            if content_type != "mod":
                return jsonify({"results": [], "total": 0,
                                 "note": f"CurseForge search here only covers mods; use Modrinth for {content_type}s."})
            return jsonify(search_curseforge(query, mc_version, loaders[0] if loaders else "", offset))
        return jsonify({"error": "unknown source"}), 400
    except requests.RequestException as e:
        return jsonify({"results": [], "total": 0,
                         "error": f"Couldn't reach {source.title()} right now: {e}"}), 502


@app.route("/api/game_versions")
def api_game_versions():
    """Minecraft versions for the sidebar filter, straight from Modrinth's
    tag list. Releases only unless ?all=1 is passed (snapshots/betas)."""
    show_all = request.args.get("all") == "1"
    try:
        r = requests.get(f"{MODRINTH_API}/tag/game_version", headers=HEADERS_UA, timeout=20)
        r.raise_for_status()
        versions = r.json()
    except (requests.RequestException, ValueError) as e:
        return jsonify({"error": f"Couldn't reach Modrinth for the version list: {e}"}), 502
    if not show_all:
        versions = [v for v in versions if v.get("version_type") == "release"]
    return jsonify([v["version"] for v in versions])


def search_modrinth(query, content_type, mc_version, loaders, offset):
    project_type = "plugin" if content_type == "plugin" else content_type
    facets = [[f"project_type:{project_type}"]]
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    if content_type in ("mod", "plugin") and loaders:
        # OR within the group: any one of the selected loaders/platforms matches.
        facets.append([f"categories:{l}" for l in loaders])
    params = {
        "query": query,
        "limit": 20,
        "offset": offset,
        "facets": json.dumps(facets),
    }
    r = requests.get(f"{MODRINTH_API}/search", params=params, headers=HEADERS_UA, timeout=20)
    r.raise_for_status()
    data = r.json()
    results = [{
        "source": "modrinth",
        "project_id": h["project_id"],
        "slug": h["slug"],
        "title": h["title"],
        "description": h["description"],
        "icon_url": h.get("icon_url"),
        "downloads": h.get("downloads"),
        "author": h.get("author"),
        "categories": [c for c in h.get("categories", []) if c not in PLUGIN_PLATFORMS | MOD_LOADERS],
        "loaders": [c for c in h.get("categories", []) if c in PLUGIN_PLATFORMS | MOD_LOADERS] or h.get("display_categories", []),
        "client_side": h.get("client_side"),   # required | optional | unsupported
        "server_side": h.get("server_side"),
        "versions": h.get("versions", []),
    } for h in data.get("hits", [])]
    return {"results": results, "total": data.get("total_hits", 0)}


def search_curseforge(query, mc_version, loader, offset):
    settings = load_settings()
    key = settings.get("curseforge_api_key")
    if not key:
        return {"results": [], "total": 0, "error": "No CurseForge API key set. Add one in Settings."}
    headers = {"x-api-key": key, **HEADERS_UA}
    params = {
        "gameId": CF_MINECRAFT_GAME_ID,
        "classId": CF_MODS_CLASS_ID,
        "searchFilter": query,
        "index": offset,
        "pageSize": 20,
        "sortField": 2,
        "sortOrder": "desc",
    }
    if mc_version:
        params["gameVersion"] = mc_version
    loader_map = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
    if loader in loader_map:
        params["modLoaderType"] = loader_map[loader]
    r = requests.get(f"{CURSEFORGE_API}/mods/search", params=params, headers=headers, timeout=20)
    if r.status_code == 401:
        return {"results": [], "total": 0, "error": "CurseForge rejected the API key."}
    r.raise_for_status()
    data = r.json()
    results = [{
        "source": "curseforge",
        "project_id": str(m["id"]),
        "slug": m.get("slug"),
        "title": m.get("name"),
        "description": m.get("summary"),
        "icon_url": (m.get("logo") or {}).get("thumbnailUrl"),
        "downloads": m.get("downloadCount"),
        "author": (m.get("authors") or [{}])[0].get("name"),
        "categories": [],
    } for m in data.get("data", [])]
    return {"results": results, "total": data.get("pagination", {}).get("totalCount", 0)}


# --------------------------------------------------------------------------
# Version lookup + install + update-check
# --------------------------------------------------------------------------
def modrinth_best_version(project_id, mc_version, loader, content_type):
    params = {"game_versions": json.dumps([mc_version])}
    if content_type in ("mod", "plugin") and loader and loader != "vanilla":
        params["loaders"] = json.dumps([loader])
    try:
        r = requests.get(f"{MODRINTH_API}/project/{project_id}/version", params=params,
                          headers=HEADERS_UA, timeout=20)
        r.raise_for_status()
        versions = r.json()
    except (requests.RequestException, ValueError) as e:
        raise LookupUnavailable(str(e))
    if not versions:
        return None
    versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
    v = versions[0]
    primary_file = next((f for f in v["files"] if f.get("primary")), v["files"][0])
    return {
        "version_id": v["id"],
        "version_number": v.get("version_number"),
        "file_name": primary_file["filename"],
        "download_url": primary_file["url"],
        "raw_dependencies": v.get("dependencies", []),
    }


def curseforge_best_version(project_id, mc_version, loader):
    settings = load_settings()
    key = settings.get("curseforge_api_key")
    if not key:
        return None
    headers = {"x-api-key": key, **HEADERS_UA}
    params = {"gameVersion": mc_version}
    loader_map = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
    if loader in loader_map:
        params["modLoaderType"] = loader_map[loader]
    try:
        r = requests.get(f"{CURSEFORGE_API}/mods/{project_id}/files", params=params,
                          headers=headers, timeout=20)
        if r.status_code != 200:
            return None
        files = r.json().get("data", [])
    except (requests.RequestException, ValueError) as e:
        raise LookupUnavailable(str(e))
    if not files:
        return None
    files.sort(key=lambda f: f.get("fileDate", ""), reverse=True)
    f = files[0]
    return {
        "version_id": str(f["id"]),
        "version_number": f.get("displayName"),
        "file_name": f["fileName"],
        "download_url": f.get("downloadUrl"),
    }


def resolve_version(source, project_id, mc_version, loader, content_type):
    if source == "modrinth":
        return modrinth_best_version(project_id, mc_version, loader, content_type)
    elif source == "curseforge":
        return curseforge_best_version(project_id, mc_version, loader)
    else:
        # e.g. "modpack" — imported as a raw file with no queryable project,
        # so there's nothing to check for updates against.
        return None


def is_preview(server_path, settings=None):
    settings = settings or load_settings()
    return bool(settings.get("preview_mode")) or server_path == DEMO_SERVER_PATH


def bucket_for(content_type):
    """Which list in the server's data file a piece of content lives in."""
    return {"mod": "mods", "plugin": "plugins", "datapack": "datapacks"}[content_type]


def dest_dir_for(server_path, data, content_type):
    """Where a downloaded file actually gets written on disk, matching what
    Crafty/the server itself expects to find."""
    if content_type == "mod":
        return Path(server_path) / "mods"
    elif content_type == "plugin":
        return Path(server_path) / "plugins"
    else:  # datapack
        return Path(server_path) / data.get("world_name", "world") / "datapacks"


def backups_dir_for(server_path, data, content_type):
    """A .backups/ subfolder next to the real mods/plugins/datapacks folder,
    so the old jar is never lost when an update is applied."""
    d = dest_dir_for(server_path, data, content_type) / ".backups"
    return d


# --------------------------------------------------------------------------
# Changelogs (shown before you click Update) + CurseForge incompatibility data
# --------------------------------------------------------------------------
def modrinth_changelog(version_id):
    try:
        r = requests.get(f"{MODRINTH_API}/version/{version_id}", headers=HEADERS_UA, timeout=15)
        if r.status_code != 200:
            return None
        return r.json().get("changelog") or None
    except requests.RequestException:
        return None


def curseforge_changelog(project_id, file_id):
    settings = load_settings()
    key = settings.get("curseforge_api_key")
    if not key:
        return None
    try:
        r = requests.get(f"{CURSEFORGE_API}/mods/{project_id}/files/{file_id}/changelog",
                          headers={"x-api-key": key, **HEADERS_UA}, timeout=15)
        if r.status_code != 200:
            return None
        return r.json().get("data") or None
    except requests.RequestException:
        return None


def curseforge_file_dependencies(project_id, file_id):
    """Returns the list of {modId, relationType} for a CurseForge file.
    relationType 5 == Incompatible, 3 == RequiredDependency. Modrinth has no
    equivalent public field for incompatibility, so incompatibility warnings
    only ever apply to CurseForge-sourced mods."""
    settings = load_settings()
    key = settings.get("curseforge_api_key")
    if not key:
        return []
    try:
        r = requests.get(f"{CURSEFORGE_API}/mods/{project_id}/files/{file_id}",
                          headers={"x-api-key": key, **HEADERS_UA}, timeout=15)
        if r.status_code != 200:
            return []
        return r.json().get("data", {}).get("dependencies", []) or []
    except requests.RequestException:
        return []


def required_dependency_project_ids(source, project_id, version):
    """Given the version dict resolve_version() returned (plus the project_id
    of the mod being installed, needed for the CurseForge lookup), pull out
    the project ids of REQUIRED dependencies only (never optional/embedded/
    incompatible ones) — this is what /api/install offers to install
    alongside the mod itself, since a mod that needs a library mod to
    function is useless without it."""
    if source == "modrinth":
        deps = version.get("raw_dependencies") or []
        return list({d["project_id"] for d in deps
                     if d.get("dependency_type") == "required" and d.get("project_id")})
    elif source == "curseforge":
        deps = curseforge_file_dependencies(project_id, version["version_id"])
        return list({str(d["modId"]) for d in deps if d.get("relationType") == 3 and d.get("modId")})
    return []


def project_title(source, project_id):
    """Best-effort human-readable name for a dependency project — falls back
    to the raw id if the lookup fails, since this is just for a confirmation
    prompt, not anything load-bearing."""
    try:
        if source == "modrinth":
            r = requests.get(f"{MODRINTH_API}/project/{project_id}", headers=HEADERS_UA, timeout=10)
            if r.status_code == 200:
                return r.json().get("title", project_id)
        elif source == "curseforge":
            settings = load_settings()
            key = settings.get("curseforge_api_key")
            if key:
                r = requests.get(f"{CURSEFORGE_API}/mods/{project_id}", headers={"x-api-key": key, **HEADERS_UA}, timeout=10)
                if r.status_code == 200:
                    return r.json().get("data", {}).get("name", project_id)
    except requests.RequestException:
        pass
    return project_id


@app.route("/api/changelog")
def api_changelog():
    source = request.args.get("source")
    project_id = request.args.get("project_id")
    version_id = request.args.get("version_id")
    if source == "modrinth":
        changelog = modrinth_changelog(version_id)
    elif source == "curseforge":
        changelog = curseforge_changelog(project_id, version_id)
    else:
        return jsonify({"error": "unknown source"}), 400
    return jsonify({"changelog": changelog or "No changelog was provided for this version."})


@app.route("/api/install", methods=["POST"])
def api_install():
    body = request.json
    server_path = body["server_path"]
    source = body["source"]
    project_id = body["project_id"]
    title = body.get("title", project_id)
    content_type = body.get("type", "mod")  # mod | datapack | plugin
    icon_url = body.get("icon_url")

    data = load_server_data(server_path)
    mc_version, loader = data["mc_version"], data["loader"]
    if not mc_version:
        return jsonify({"error": "Set the server's Minecraft version first (Server Setup tab)."}), 400

    block = require_server_stopped(server_path)
    if block:
        return jsonify({"error": block[0]}), block[1]

    # Real lookup against Modrinth/CurseForge either way, so results are accurate
    # even in preview mode.
    try:
        version = resolve_version(source, project_id, mc_version, loader, content_type)
    except LookupUnavailable as e:
        return jsonify({"error": f"Couldn't reach {source.title()} to look up {title}: {e}"}), 502
    if not version or not version.get("download_url"):
        return jsonify({"error": f"No {source} file found for {title} on Minecraft {mc_version}"
                                  f"{' / ' + loader if content_type in ('mod', 'plugin') else ''}."}), 404

    preview = is_preview(server_path)
    if not preview:
        dest_dir = dest_dir_for(server_path, data, content_type)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / version["file_name"]
        try:
            r = requests.get(version["download_url"], headers=HEADERS_UA, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            return jsonify({"error": f"Couldn't download {title}: {e}"}), 502
        dest_file.write_bytes(r.content)

    entry = {
        "project_id": project_id,
        "source": source,
        "title": title,
        "icon_url": icon_url,
        "version_id": version["version_id"],
        "version_number": version.get("version_number"),
        "file_name": version["file_name"],
        "mc_version": mc_version,
        "loader": loader if content_type in ("mod", "plugin") else "datapack",
        "type": content_type,
        "preview": preview,
    }
    bucket = bucket_for(content_type)
    data[bucket] = [m for m in data[bucket] if m["project_id"] != project_id or m["source"] != source]
    data[bucket].append(entry)
    save_server_data(server_path, data)

    # Dependencies: only offered for mods/plugins (datapacks don't have them),
    # only the REQUIRED ones, and only ones not already installed — never
    # installed automatically, just handed back so the person can say yes.
    suggested_dependencies = []
    if content_type in ("mod", "plugin"):
        already_installed = {(m["source"], m["project_id"]) for m in data["mods"] + data.get("plugins", [])}
        dep_ids = required_dependency_project_ids(source, project_id, version)
        for dep_id in dep_ids:
            if (source, dep_id) in already_installed:
                continue
            suggested_dependencies.append({
                "source": source, "project_id": dep_id, "title": project_title(source, dep_id),
            })

    return jsonify({"ok": True, "installed": entry, "preview": preview,
                     "suggested_dependencies": suggested_dependencies})


def _sha1_of_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _modrinth_identify_by_hash(sha1_hex: str):
    """Looks up a jar by its exact file hash via Modrinth's version-file
    endpoint — this is how a mod that was dropped into mods/ by hand (or
    installed before this tool existed) can still be identified and
    reconciled back to a real Modrinth project/version, rather than either
    staying invisible or needing to be manually re-searched and reinstalled."""
    try:
        r = requests.get(f"{MODRINTH_API}/version_file/{sha1_hex}", params={"algorithm": "sha1"},
                          headers=HEADERS_UA, timeout=10)
        if r.status_code != 200:
            return None
        version = r.json()
        project_id = version.get("project_id")
        if not project_id:
            return None
        title = project_id
        try:
            pr = requests.get(f"{MODRINTH_API}/project/{project_id}", headers=HEADERS_UA, timeout=10)
            if pr.status_code == 200:
                title = pr.json().get("title", project_id)
        except requests.RequestException:
            pass
        primary_file = next((f for f in version.get("files", []) if f.get("primary")), (version.get("files") or [{}])[0])
        return {
            "source": "modrinth", "project_id": project_id, "title": title,
            "version_id": version.get("id"), "version_number": version.get("version_number"),
            "file_name": primary_file.get("filename"),
        }
    except requests.RequestException:
        return None


def scan_untracked_files(server_path, data, content_type):
    """Files sitting in mods/ or plugins/ that this tool didn't put there
    itself (dropped in by hand, or installed before this tool was set up)
    used to be entirely invisible — the Installed tab only ever showed what
    was in the tracked list, never what was actually on disk. This scans the
    real folder, skips anything already tracked (matched by filename) and
    the .backups subfolder, and tries to identify each leftover via Modrinth
    hash lookup so it can be "adopted" with real metadata instead of staying
    a mystery file forever.

    Results are cached per-file (keyed by size+mtime, so a real file change
    invalidates it) in the server's own data — this function runs on every
    single Installed-tab refresh (every install/update/revert/tab switch),
    and without caching that meant hashing the file AND hitting Modrinth's
    API again for the same untouched file every single time. A hash miss
    (identified: None) is cached too, so an unrecognized file doesn't get
    re-queried forever, only re-tried if the file itself actually changes."""
    if content_type not in ("mod", "plugin"):
        return []
    folder = dest_dir_for(server_path, data, content_type)
    if not folder.is_dir():
        return []
    tracked_filenames = {item.get("file_name") for item in data[bucket_for(content_type)]}
    cache = data.setdefault("_untracked_cache", {})
    cache_dirty = False
    out = []
    for jar in sorted(folder.glob("*.jar")):
        if jar.name in tracked_filenames:
            continue
        stat = jar.stat()
        cache_key = f"{content_type}:{jar.name}"
        cached = cache.get(cache_key)
        if cached and cached.get("size") == stat.st_size and cached.get("mtime") == stat.st_mtime:
            identified = cached.get("identified")
        else:
            identified = None
            try:
                identified = _modrinth_identify_by_hash(_sha1_of_file(jar))
            except OSError:
                pass
            cache[cache_key] = {"size": stat.st_size, "mtime": stat.st_mtime, "identified": identified}
            cache_dirty = True
        out.append({
            "file_name": jar.name,
            "size_bytes": stat.st_size,
            "identified": identified,  # None if Modrinth doesn't recognize this exact file
        })
    # Drop stale cache entries for files that no longer exist, so this
    # doesn't grow forever across renames/deletes.
    current_keys = {f"{content_type}:{item['file_name']}" for item in out}
    for key in [k for k in cache if k.startswith(f"{content_type}:") and k not in current_keys]:
        del cache[key]
        cache_dirty = True
    if cache_dirty:
        save_server_data(server_path, data)
    return out


@app.route("/api/installed")
def api_installed():
    path = request.args.get("server_path", "")
    data = load_server_data(path)
    return jsonify({
        "mods": data["mods"], "datapacks": data["datapacks"],
        "plugins": data.get("plugins", []),
        "mc_version": data["mc_version"], "loader": data["loader"],
        "untracked_mods": scan_untracked_files(path, data, "mod"),
        "untracked_plugins": scan_untracked_files(path, data, "plugin"),
    })


@app.route("/api/adopt", methods=["POST"])
def api_adopt():
    """Brings an untracked file (found by scan_untracked_files) into this
    tool's tracking. If Modrinth identified it, the real project/version
    metadata is used, so update-checking works on it going forward. If not
    (unknown origin — CurseForge, a private build, or hand-compiled), it's
    still adopted with source="unknown" so it at least shows up and isn't
    silently deleted/ignored by anything else this tool does — it just won't
    ever show an update, since there's nothing to check it against."""
    body = request.json
    server_path = body["server_path"]
    content_type = body.get("type", "mod")
    file_name = body["file_name"]
    identified = body.get("identified")
    data = load_server_data(server_path)
    bucket = bucket_for(content_type)

    if identified:
        entry = {
            "project_id": identified["project_id"], "source": identified["source"],
            "title": identified["title"], "icon_url": None,
            "version_id": identified.get("version_id"), "version_number": identified.get("version_number"),
            "file_name": file_name, "mc_version": data.get("mc_version"),
            "loader": data.get("loader") if content_type in ("mod", "plugin") else "datapack",
            "type": content_type, "preview": False,
        }
    else:
        entry = {
            "project_id": f"unknown:{file_name}", "source": "unknown",
            "title": file_name, "icon_url": None,
            "version_id": None, "version_number": None,
            "file_name": file_name, "mc_version": data.get("mc_version"),
            "loader": data.get("loader") if content_type in ("mod", "plugin") else "datapack",
            "type": content_type, "preview": False,
        }
    data[bucket] = [m for m in data[bucket] if m.get("file_name") != file_name]
    data[bucket].append(entry)
    save_server_data(server_path, data)
    return jsonify({"ok": True, "adopted": entry})


@app.route("/api/check_updates", methods=["POST"])
def api_check_updates():
    body = request.json
    server_path = body["server_path"]
    # Optional: [{"source":..., "project_id":...}, ...] to re-check only these
    # items (used by the "Retry failed items" button) instead of the whole
    # installed list — avoids re-hitting Modrinth/CurseForge for everything
    # that already checked out fine.
    only = body.get("only")
    only_keys = {(o["source"], o["project_id"]) for o in only} if only else None

    data = load_server_data(server_path)
    mc_version, loader = data["mc_version"], data["loader"]
    results = []
    all_items = [(bucket, item) for bucket in ("mods", "plugins", "datapacks") for item in data[bucket]]

    # CurseForge-only incompatibility check: Modrinth doesn't expose a
    # dependency-relation field publicly, but CurseForge does (relationType
    # 5 == "Incompatible"). We only look this up for CurseForge-sourced
    # items, and only flag a conflict when the OTHER mod is also installed
    # through this tool.
    installed_cf_ids = {item["project_id"] for _, item in all_items if item["source"] == "curseforge"}
    conflicts_by_project = {}
    for _, item in all_items:
        if item["source"] != "curseforge":
            continue
        if only_keys is not None and (item["source"], item["project_id"]) not in only_keys:
            continue
        deps = curseforge_file_dependencies(item["project_id"], item["version_id"])
        incompatible_ids = {str(d.get("modId")) for d in deps if d.get("relationType") == 5}
        hit = incompatible_ids & installed_cf_ids
        if hit:
            conflicting_titles = [i["title"] for _, i in all_items if i["project_id"] in hit and i["source"] == "curseforge"]
            conflicts_by_project[item["project_id"]] = conflicting_titles

    for bucket in ("mods", "plugins", "datapacks"):
        for item in data[bucket]:
            if only_keys is not None and (item["source"], item["project_id"]) not in only_keys:
                continue
            try:
                latest = resolve_version(item["source"], item["project_id"], mc_version,
                                          loader if item["type"] in ("mod", "plugin") else "", item["type"])
            except LookupUnavailable as e:
                # The lookup itself failed (network/rate-limit/timeout) — this
                # is retryable, unlike "no_compatible_version" below, which
                # means the lookup succeeded and there's genuinely nothing to
                # install yet.
                results.append({**item, "status": "check_failed",
                                 "message": f"Couldn't check {item['source'].title()} just now: {e}",
                                 "latest": None, "conflicts": [], "retryable": True})
                continue
            if latest is None:
                if item["source"] not in ("modrinth", "curseforge"):
                    status = "no_compatible_version"
                    message = "Imported directly from a modpack — no source project to check for updates against."
                else:
                    status = "no_compatible_version"
                    message = (f"No {item['source']} build of {item['title']} is available yet "
                                f"for Minecraft {mc_version}. Keeping the currently installed file.")
            elif latest["version_id"] != item["version_id"]:
                status = "update_available"
                message = f"Update available: {item.get('version_number', item['file_name'])} -> " \
                           f"{latest.get('version_number', latest['file_name'])}"
            else:
                status = "up_to_date"
                message = "Up to date."
            conflicts = conflicts_by_project.get(item["project_id"], [])
            results.append({**item, "status": status, "message": message,
                             "latest": latest, "conflicts": conflicts})
    return jsonify({"results": results, "mc_version": mc_version, "loader": loader, "partial": only_keys is not None})


@app.route("/api/update_mod", methods=["POST"])
def api_update_mod():
    body = request.json
    server_path = body["server_path"]
    source = body["source"]
    project_id = body["project_id"]
    content_type = body.get("type", "mod")

    data = load_server_data(server_path)
    mc_version, loader = data["mc_version"], data["loader"]
    bucket = bucket_for(content_type)
    item = next((m for m in data[bucket] if m["project_id"] == project_id and m["source"] == source), None)
    if not item:
        return jsonify({"error": "Not installed"}), 404

    block = require_server_stopped(server_path)
    if block:
        return jsonify({"error": block[0]}), block[1]

    try:
        latest = resolve_version(source, project_id, mc_version, loader if content_type in ("mod", "plugin") else "", content_type)
    except LookupUnavailable as e:
        return jsonify({"error": f"Couldn't reach {source.title()} to check for an update: {e}"}), 502
    if not latest:
        return jsonify({"error": "No compatible version found for the current Minecraft version."}), 404

    backup_info = None
    if not item.get("preview"):
        dest_dir = dest_dir_for(server_path, data, content_type)
        old_file = dest_dir / item["file_name"]

        # Download the new file FIRST and only touch the old one once we know
        # the new one is safely on disk — otherwise a network blip here would
        # leave the mod deleted with nothing to replace it (see api_install
        # for the equivalent, already-safe ordering used on first install).
        try:
            r = requests.get(latest["download_url"], headers=HEADERS_UA, timeout=60)
            r.raise_for_status()
            new_bytes = r.content
        except requests.RequestException as e:
            return jsonify({"error": f"Couldn't download the update: {e}. "
                                      f"Nothing was changed — the current file is untouched."}), 502

        if old_file.exists():
            # Backup-before-update: the old jar is copied into .backups/ before
            # it's ever removed, so a bad update is always one click to undo.
            backup_dir = backups_dir_for(server_path, data, content_type)
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_name = f"{stamp}__{old_file.name}"
            shutil.copy2(old_file, backup_dir / backup_name)
            backup_info = {"file_name": backup_name, "original_name": old_file.name,
                            "version_id": item["version_id"], "version_number": item.get("version_number"),
                            "created": stamp}
            old_file.unlink()
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / latest["file_name"]).write_bytes(new_bytes)

    if backup_info:
        item["last_backup"] = backup_info

    item.update({
        "version_id": latest["version_id"],
        "version_number": latest.get("version_number"),
        "file_name": latest["file_name"],
        "mc_version": mc_version,
    })
    save_server_data(server_path, data)
    return jsonify({"ok": True, "updated": item})


@app.route("/api/update_all", methods=["POST"])
def api_update_all():
    """Bulk action: updates every installed mod/plugin/datapack that
    currently has an update available. Reuses the same per-item logic
    (and backup-before-update) as a single Update click."""
    body = request.json
    server_path = body["server_path"]
    data = load_server_data(server_path)
    mc_version, loader = data["mc_version"], data["loader"]

    block = require_server_stopped(server_path)
    if block:
        return jsonify({"error": block[0]}), block[1]

    updated, failed = [], []
    for bucket in ("mods", "plugins", "datapacks"):
        for item in list(data[bucket]):
            content_type = item["type"]
            try:
                latest = resolve_version(item["source"], item["project_id"], mc_version,
                                          loader if content_type in ("mod", "plugin") else "", content_type)
            except LookupUnavailable as e:
                failed.append({"title": item["title"], "error": f"Couldn't check for an update: {e}"})
                continue
            if not latest or latest["version_id"] == item["version_id"]:
                continue
            try:
                if not item.get("preview"):
                    dest_dir = dest_dir_for(server_path, data, content_type)
                    old_file = dest_dir / item["file_name"]
                    # Download the replacement FIRST — only touch the old file
                    # once the new one is confirmed on disk (see api_update_mod
                    # for the same fix, applied for the same reason).
                    r = requests.get(latest["download_url"], headers=HEADERS_UA, timeout=60)
                    r.raise_for_status()
                    new_bytes = r.content
                    if old_file.exists():
                        backup_dir = backups_dir_for(server_path, data, content_type)
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                        backup_name = f"{stamp}__{old_file.name}"
                        shutil.copy2(old_file, backup_dir / backup_name)
                        item["last_backup"] = {"file_name": backup_name, "original_name": old_file.name,
                                                "version_id": item["version_id"],
                                                "version_number": item.get("version_number"), "created": stamp}
                        old_file.unlink()
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    (dest_dir / latest["file_name"]).write_bytes(new_bytes)
                item.update({
                    "version_id": latest["version_id"],
                    "version_number": latest.get("version_number"),
                    "file_name": latest["file_name"],
                    "mc_version": mc_version,
                })
                updated.append({"title": item["title"], "version_number": item.get("version_number")})
            except Exception as e:
                failed.append({"title": item["title"], "error": str(e)})
    save_server_data(server_path, data)
    return jsonify({"ok": True, "updated": updated, "failed": failed})


@app.route("/api/revert", methods=["POST"])
def api_revert():
    """One-click revert: restores the most recent .backups/ copy for an
    item, swapping the current (post-update) file back out."""
    body = request.json
    server_path = body["server_path"]
    source = body["source"]
    project_id = body["project_id"]
    content_type = body.get("type", "mod")

    data = load_server_data(server_path)
    bucket = bucket_for(content_type)
    item = next((m for m in data[bucket] if m["project_id"] == project_id and m["source"] == source), None)
    if not item:
        return jsonify({"error": "Not installed"}), 404
    backup = item.get("last_backup")
    if not backup:
        return jsonify({"error": "No backup available for this item yet — backups are only made when Update replaces an existing file."}), 404

    block = require_server_stopped(server_path)
    if block:
        return jsonify({"error": block[0]}), block[1]

    if not item.get("preview"):
        dest_dir = dest_dir_for(server_path, data, content_type)
        backup_dir = backups_dir_for(server_path, data, content_type)
        backup_file = backup_dir / backup["file_name"]
        if not backup_file.exists():
            return jsonify({"error": "Backup file is missing on disk."}), 404
        current_file = dest_dir / item["file_name"]
        if current_file.exists():
            current_file.unlink()
        restored_path = dest_dir / backup["original_name"]
        shutil.copy2(backup_file, restored_path)

    item.update({
        "version_id": backup["version_id"],
        "version_number": backup.get("version_number"),
        "file_name": backup["original_name"],
    })
    item.pop("last_backup", None)
    save_server_data(server_path, data)
    return jsonify({"ok": True, "reverted": item})



# --------------------------------------------------------------------------
# Modpack import / export (Modrinth .mrpack format)
# --------------------------------------------------------------------------
def _resolve_mrpack_bytes(url=None, upload=None):
    """Accepts either an uploaded .mrpack file, a direct .mrpack download
    URL, or a Modrinth project/version page URL (which gets resolved to its
    primary .mrpack file via the API first)."""
    if upload is not None:
        return upload.read()
    if not url:
        raise ValueError("Provide either a .mrpack file or a URL.")
    if url.endswith(".mrpack") or "cdn.modrinth.com" in url:
        r = requests.get(url, headers=HEADERS_UA, timeout=60)
        r.raise_for_status()
        return r.content
    # Otherwise treat it as a Modrinth project/version page URL and resolve
    # to that project's newest modpack-type version with a .mrpack file.
    m = re.search(r"modrinth\.com/(?:modpack|project)/([^/?#]+)", url)
    if not m:
        raise ValueError("Couldn't recognize that as a Modrinth modpack URL or a direct .mrpack link.")
    slug = m.group(1)
    r = requests.get(f"{MODRINTH_API}/project/{slug}/version", headers=HEADERS_UA, timeout=20)
    r.raise_for_status()
    versions = r.json()
    if not versions:
        raise ValueError("No versions found for that modpack.")
    versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
    for v in versions:
        for f in v.get("files", []):
            if f["filename"].endswith(".mrpack"):
                dl = requests.get(f["url"], headers=HEADERS_UA, timeout=60)
                dl.raise_for_status()
                return dl.content
    raise ValueError("That modpack doesn't have a .mrpack file to import.")


@app.route("/api/modpack/import", methods=["POST"])
def modpack_import():
    """Imports every server-compatible mod from a Modrinth .mrpack into the
    currently selected server. Overrides/resource-only files are skipped —
    this only installs the mod jars themselves into mods/."""
    server_path = request.form.get("server_path") or (request.json or {}).get("server_path")
    url = request.form.get("url") or (request.json or {}).get("url")
    upload = request.files.get("file")

    if not server_path:
        return jsonify({"error": "No server selected."}), 400
    data = load_server_data(server_path)
    if not data["mc_version"]:
        return jsonify({"error": "Set the server's Minecraft version first (Server Setup tab)."}), 400

    block = require_server_stopped(server_path)
    if block:
        return jsonify({"error": block[0]}), block[1]

    try:
        raw = _resolve_mrpack_bytes(url=url, upload=upload)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        manifest = json.loads(zf.read("modrinth.index.json"))
    except Exception as e:
        return jsonify({"error": f"Couldn't read that as a .mrpack file: {e}"}), 400

    preview = is_preview(server_path)
    installed, skipped = [], []
    dest_dir = dest_dir_for(server_path, data, "mod")
    if not preview:
        dest_dir.mkdir(parents=True, exist_ok=True)

    for file_entry in manifest.get("files", []):
        env = file_entry.get("env", {})
        if env.get("server") == "unsupported":
            skipped.append({"file": file_entry.get("path"), "reason": "not needed server-side"})
            continue
        downloads = file_entry.get("downloads") or []
        if not downloads:
            skipped.append({"file": file_entry.get("path"), "reason": "no download URL in manifest"})
            continue
        file_name = os.path.basename(file_entry.get("path", downloads[0]))
        project_id = (file_entry.get("path", "") or file_name)
        title = file_name.rsplit(".", 1)[0]
        if not preview:
            try:
                r = requests.get(downloads[0], headers=HEADERS_UA, timeout=60)
                r.raise_for_status()
                (dest_dir / file_name).write_bytes(r.content)
            except requests.RequestException as e:
                skipped.append({"file": file_name, "reason": str(e)})
                continue
        entry = {
            "project_id": f"modpack:{project_id}",
            "source": "modpack",
            "title": title,
            "icon_url": None,
            "version_id": (file_entry.get("hashes") or {}).get("sha1", file_name),
            "version_number": manifest.get("versionId", ""),
            "file_name": file_name,
            "mc_version": data["mc_version"],
            "loader": data["loader"],
            "type": "mod",
            "preview": preview,
            "modpack": manifest.get("name", "imported modpack"),
        }
        data["mods"] = [m for m in data["mods"] if m["file_name"] != file_name]
        data["mods"].append(entry)
        installed.append(title)

    save_server_data(server_path, data)
    return jsonify({
        "ok": True, "modpack_name": manifest.get("name"), "modpack_version": manifest.get("versionId"),
        "installed": installed, "skipped": skipped, "preview": preview,
    })


@app.route("/api/modpack/export")
def modpack_export():
    """Exports everything currently installed on a server as a minimal
    Modrinth-style .mrpack (best-effort — items installed via CurseForge or
    hand-copied jars are included as generic file entries pointing at a
    Modrinth CDN URL only when we can resolve one; otherwise they're listed
    in a companion note so nothing is silently dropped)."""
    server_path = request.args.get("server_path", "")
    data = load_server_data(server_path)
    manifest = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "name": f"{data.get('name', 'server')} export",
        "summary": "Exported from Anvil Mod Manager.",
        "files": [],
        "dependencies": {"minecraft": data.get("mc_version", "")},
    }
    if data.get("loader") in ("fabric", "quilt", "forge", "neoforge"):
        manifest["dependencies"][f"{data['loader']}-loader"] = "*"

    unresolved = []
    for item in data.get("mods", []):
        if item["source"] == "modrinth":
            v = modrinth_best_version(item["project_id"], data["mc_version"], data["loader"], "mod")
            if v:
                manifest["files"].append({
                    "path": f"mods/{v['file_name']}",
                    "hashes": {}, "env": {"client": "optional", "server": "required"},
                    "downloads": [v["download_url"]], "fileSize": 0,
                })
                continue
        unresolved.append(item["title"])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(manifest, indent=2))
        if unresolved:
            zf.writestr("UNRESOLVED_MODS.txt",
                        "These installed items couldn't be re-resolved to a Modrinth download URL "
                        "(CurseForge-only, or no longer available) and are NOT included in this pack:\n\n"
                        + "\n".join(f"- {t}" for t in unresolved))
    buf.seek(0)
    fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", data.get("name", "server")) + ".mrpack"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype="application/zip")


@app.route("/api/uninstall", methods=["POST"])
def api_uninstall():
    body = request.json
    server_path = body["server_path"]
    source = body["source"]
    project_id = body["project_id"]
    content_type = body.get("type", "mod")

    data = load_server_data(server_path)
    bucket = bucket_for(content_type)
    item = next((m for m in data[bucket] if m["project_id"] == project_id and m["source"] == source), None)
    if item:
        if not item.get("preview"):
            dest_dir = dest_dir_for(server_path, data, content_type)
            f = dest_dir / item["file_name"]
            if f.exists():
                f.unlink()
        data[bucket] = [m for m in data[bucket] if not (m["project_id"] == project_id and m["source"] == source)]
        save_server_data(server_path, data)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5151))
    # ANVIL_MOD_MANAGER_DEBUG=1 for local dev (auto-reload). Off by default so it's
    # safe to run under systemd (install.sh's systemd service sets nothing, so
    # this is off on a real server install).
    debug = os.environ.get("ANVIL_MOD_MANAGER_DEBUG", "0") == "1"
    if not debug:
        print("=" * 50)
        print("  Anvil Mod Manager is running.")
        print(f"  Open this in a browser: http://<this-machine-ip>:{port}/")
        print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=debug)
