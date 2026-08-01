"""
Encrypted, persistent storage for the terminal's sudo session credentials.

The password is only ever written here AFTER a successful PAM check
(see app.py: verify_credentials). It is never used to build shell commands —
it exists solely so the terminal tab can skip re-prompting on future visits,
until the user explicitly logs out.

Both the key and the encrypted blob are root-owned, mode 0600. Since the
Flask app itself already runs as root (systemd), this protects against
accidental exposure (backups, screenshots, stray `cat`s) — it is not a
substitute for keeping the dashboard off the public internet and treating
its access token like a root password.
"""

import json
import os

try:
    from cryptography.fernet import Fernet
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

SESSION_CREDS_PATH = "/etc/anvilmc/sudo_session"
ENC_KEY_PATH = "/etc/anvilmc/enc.key"


def _get_or_create_key():
    if os.path.exists(ENC_KEY_PATH):
        return open(ENC_KEY_PATH, "rb").read()
    key = Fernet.generate_key()
    fd = os.open(ENC_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def save_session_creds(username, password):
    if not _CRYPTO_AVAILABLE:
        return  # Terminal tab is unavailable here anyway (see app.py _PTY_AVAILABLE) — nothing to persist
    f = Fernet(_get_or_create_key())
    token = f.encrypt(json.dumps({"username": username, "password": password}).encode())
    fd = os.open(SESSION_CREDS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(token)


def load_session_creds():
    if not _CRYPTO_AVAILABLE or not os.path.exists(SESSION_CREDS_PATH):
        return None
    try:
        f = Fernet(_get_or_create_key())
        with open(SESSION_CREDS_PATH, "rb") as fh:
            return json.loads(f.decrypt(fh.read()))
    except Exception:
        return None


def clear_session_creds():
    if _CRYPTO_AVAILABLE and os.path.exists(SESSION_CREDS_PATH):
        os.remove(SESSION_CREDS_PATH)
