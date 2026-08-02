#!/usr/bin/env bash
# AnvilMC — single install script for the whole ecosystem.
#
# Run this ONCE over SSH on the Ubuntu Server box that will run Crafty
# Controller. Sets up all three dashboards — Anvil Server Installer, Anvil
# Mod Manager, and Anvil Server Manager — sharing one access token, so
# there's nothing else to configure before you're looking at a working
# dashboard. Safe to re-run any time (idempotent): stops and replaces
# anything left over from a previous attempt, including the old separate-repo
# installs this project used before AnvilMC became one monorepo.
#
# Docker, Crafty Controller, and Cockpit are NOT installed by this script —
# those stay as deliberate, one-click steps inside the Installer's own
# dashboard, since they involve bigger downloads and real choices (Crafty
# server folder location, etc.) that are worth seeing happen rather than
# running unattended.

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/anvilmc"
ETC_DIR="/etc/anvilmc"

echo "=================================================="
echo "  AnvilMC — install"
echo "=================================================="

if [ "$EUID" -eq 0 ]; then
  SUDO=""
  INSTALL_USER="root"
else
  if ! command -v sudo &> /dev/null; then
    echo "This user isn't root and sudo isn't installed, so this script can't get the"
    echo "admin privileges it needs. Either:"
    echo "  1) Log in as root (or 'su -') and re-run this script, or"
    echo "  2) As root, run: apt install sudo   then add yourself: usermod -aG sudo $(whoami)"
    echo "     log out and back in, and re-run this script."
    exit 1
  fi
  if ! sudo -v; then
    echo ""
    echo "This user ($(whoami)) doesn't have sudo privileges, so the install can't continue."
    echo "Add it with: usermod -aG sudo $(whoami)   (as root), then log out/in and retry."
    exit 1
  fi
  SUDO="sudo"
  INSTALL_USER="$(whoami)"
fi

echo ""
echo "[1/7] Clearing out anything left over from a previous install attempt..."
# Covers three real cases: (1) re-running this script after a previous run
# got partway through, (2) the old separate-repo era of this project (before
# AnvilMC became one monorepo), where each app had its own bootstrap.sh and
# its own /opt/anvil-<name> folder, and (3) a genuinely ancient install under
# this project's original name. Any of these could still be holding onto
# ports 8090/5151/6161, which is the single most common reason a fresh
# install's dashboards silently "don't load" — the OLD process is still the
# one answering on that port, not the new one this script just set up.
for svc in anvil-installer anvil-mod-manager anvil-server-manager mc-dashboard; do
  $SUDO systemctl disable --now "$svc" > /dev/null 2>&1 || true
  $SUDO rm -f "/etc/systemd/system/${svc}.service"
done
$SUDO systemctl daemon-reload
for old_dir in /opt/anvil-installer /opt/anvil-mod-manager /opt/anvil-server-manager /opt/mc-dashboard; do
  if [ -d "$old_dir" ]; then
    echo "    Removing old install at $old_dir ..."
    $SUDO rm -rf "$old_dir"
  fi
done
# Anything still bound to these ports at this point isn't one of our own
# services (those were just stopped above) — flag it rather than silently
# fighting over the port later.
for port in 8090 5151 6161; do
  if $SUDO ss -ltnp 2>/dev/null | grep -q ":$port "; then
    echo "    WARNING: something else is already listening on port $port:"
    $SUDO ss -ltnp 2>/dev/null | grep ":$port " | sed 's/^/      /'
    echo "    This install will likely fail to bind that port until it's stopped."
  fi
done

echo ""
echo "[2/7] Installing shared prerequisites (python3, pip, venv, git, curl,"
echo "      restic, smartmontools, lm-sensors)..."
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq python3 python3-venv python3-pip git curl restic smartmontools lm-sensors > /dev/null

echo "[3/7] Copying AnvilMC to $INSTALL_DIR ..."
$SUDO mkdir -p "$INSTALL_DIR"

# Preserve real settings across a re-run (updating, or fixing something) —
# settings.json for both these apps got committed to git early on, before
# .gitignore excluded it, so it's still sitting in the repo's history as
# stale demo content. A plain overwrite would silently replace a REAL Crafty
# connection, tracked servers, and backup config with that leftover demo
# data every single time this script runs again. Back up whatever's already
# there, copy the fresh checkout over, then restore it — so first install
# gets the repo's (harmless, demo) defaults, and every install after that
# keeps whatever you actually configured.
DATA_BACKUP="$(mktemp -d)"
for app_dir in mod-manager server-manager; do
  if [ -d "$INSTALL_DIR/$app_dir/data" ]; then
    cp -r "$INSTALL_DIR/$app_dir/data" "$DATA_BACKUP/$app_dir-data" 2>/dev/null || true
  fi
done

# Copies the whole checkout (including .git, if present) so the Installer's
# "Check for updates" can later `git pull` this one clone and restart all
# three services — updating the whole ecosystem in one action instead of
# three separate self-updates.
$SUDO cp -r "$REPO_DIR"/. "$INSTALL_DIR"/
$SUDO rm -rf "$INSTALL_DIR/installer/venv" "$INSTALL_DIR/installer/.venv" \
             "$INSTALL_DIR/mod-manager/venv" "$INSTALL_DIR/mod-manager/.venv" \
             "$INSTALL_DIR/server-manager/venv" "$INSTALL_DIR/server-manager/.venv"

for app_dir in mod-manager server-manager; do
  if [ -d "$DATA_BACKUP/$app_dir-data" ]; then
    echo "    Restoring your existing $app_dir settings (not overwriting with the repo's demo data)..."
    $SUDO rm -rf "$INSTALL_DIR/$app_dir/data"
    $SUDO cp -r "$DATA_BACKUP/$app_dir-data" "$INSTALL_DIR/$app_dir/data"
  fi
done
rm -rf "$DATA_BACKUP"

echo "[4/7] Setting ownership (Installer + Server Manager as root, since they"
echo "      genuinely need it for apt/docker/systemctl; Mod Manager as '$INSTALL_USER',"
echo "      since it only ever touches mod/plugin files and shouldn't need more)..."
$SUDO chown -R root:root "$INSTALL_DIR/installer" "$INSTALL_DIR/server-manager"
$SUDO mkdir -p "$INSTALL_DIR/mod-manager/data"
$SUDO chown -R "$INSTALL_USER":"$INSTALL_USER" "$INSTALL_DIR/mod-manager"

echo "[5/7] Shared token, sessions, and PAM login (one login for all three)..."
$SUDO mkdir -p "$ETC_DIR"
# 2775 + setgid: root keeps full control, "anvilmc" group members (which
# includes $INSTALL_USER, added below) can create/update files here too —
# this is what lets Mod Manager (running as $INSTALL_USER, not root) write
# its own session secret AND share the same crafty_api.json Server Manager
# uses, so entering the Crafty URL/token in either app fills in the other.
$SUDO chmod 2775 "$ETC_DIR"
$SUDO groupadd -f anvilmc
$SUDO chgrp anvilmc "$ETC_DIR"
$SUDO usermod -aG anvilmc "$INSTALL_USER" 2>/dev/null || true

# Mod Manager still gets its own private subdirectory for its session secret
# (a per-app signing key — no reason to share that one).
$SUDO mkdir -p "$ETC_DIR/mod-manager"
$SUDO chown "$INSTALL_USER":"$INSTALL_USER" "$ETC_DIR/mod-manager"

if [ -s "$ETC_DIR/token" ]; then
  TOKEN=$($SUDO cat "$ETC_DIR/token")
  echo "    Reusing the existing shared token (re-running install.sh doesn't invalidate it)."
else
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  echo "$TOKEN" | $SUDO tee "$ETC_DIR/token" > /dev/null
fi
# Group-readable (not world-readable) rather than root-only: Mod Manager runs
# as "$INSTALL_USER", not root, and needs to read this same token to gate its
# own dashboard. It's an app-level access token, not a system credential, so
# this is a reasonable trade for "one login for all three" instead of a
# separate token per app.
$SUDO chgrp anvilmc "$ETC_DIR/token"
$SUDO chmod 640 "$ETC_DIR/token"

cat <<'EOS' | $SUDO tee "$ETC_DIR/get-link.sh" > /dev/null
#!/usr/bin/env bash
IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
[ -z "$IP" ] && IP=$(hostname -I | awk '{print $1}')
TOKEN=$(cat /etc/anvilmc/token)
echo "Installer:       http://$IP:8090/?token=$TOKEN"
echo "Mod Manager:     http://$IP:5151/?token=$TOKEN"
echo "Server Manager:  http://$IP:6161/?token=$TOKEN"
EOS
$SUDO chmod 750 "$ETC_DIR/get-link.sh"
$SUDO chgrp anvilmc "$ETC_DIR/get-link.sh"

# PAM login fallback for all three dashboards' sign-in page (see each app's
# _ensure_pam_service_file() — this is created lazily by the apps themselves
# too, so this is just belt-and-braces to have it ready from first boot).
cat <<'EOS' | $SUDO tee /etc/pam.d/anvilmc-auth > /dev/null
# Managed by AnvilMC — minimal PAM service for the dashboards' username/
# password sign-in fallback. Deliberately skips login-specific checks
# (pam_nologin, pam_securetty, pam_lastlog) that assume a real interactive
# terminal session and otherwise cause a correct password to still fail.
auth    include common-auth
account include common-account
EOS

install_component() {
  local label="$1" dir="$2" user="$3" service_file="$4" service_name
  service_name="$(basename "$service_file" .service)"
  echo "    Setting up $label..."
  python3 -m venv "$INSTALL_DIR/$dir/venv"
  if [ -f "$INSTALL_DIR/$dir/requirements.txt" ]; then
    "$INSTALL_DIR/$dir/venv/bin/pip" install -q -r "$INSTALL_DIR/$dir/requirements.txt"
  else
    "$INSTALL_DIR/$dir/venv/bin/pip" install -q flask flask-sock cryptography
  fi
  sed "s/__INSTALL_USER__/$user/" "$INSTALL_DIR/$dir/$service_file" | $SUDO tee "/etc/systemd/system/$service_file" > /dev/null
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$service_name" > /dev/null 2>&1
  $SUDO systemctl restart "$service_name"
}

echo "[6/7] Installer dashboard..."
install_component "Anvil Server Installer" "installer" "root" "anvil-installer.service"

echo "[7/7] Anvil Mod Manager + Anvil Server Manager..."
install_component "Anvil Mod Manager" "mod-manager" "$INSTALL_USER" "anvil-mod-manager.service"
install_component "Anvil Server Manager" "server-manager" "root" "anvil-server-manager.service"

# The app itself runs as "$INSTALL_USER" (not root) so it only has the same
# filesystem access as Crafty — but the "Update now" button needs to
# restart its own systemd unit afterward. A narrow, single-command sudo
# exception, same reasoning as before.
if [ "$INSTALL_USER" != "root" ]; then
  SYSTEMCTL_PATH="$(command -v systemctl)"
  echo "$INSTALL_USER ALL=(root) NOPASSWD: $SYSTEMCTL_PATH restart anvil-mod-manager" | \
    $SUDO tee /etc/sudoers.d/anvil-mod-manager-restart > /dev/null
  $SUDO chmod 440 /etc/sudoers.d/anvil-mod-manager-restart
  $SUDO visudo -c -f /etc/sudoers.d/anvil-mod-manager-restart > /dev/null || \
    echo "  WARNING: the generated sudoers rule failed validation — Mod Manager's restart-on-update may not work. Check /etc/sudoers.d/anvil-mod-manager-restart"
fi

IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
[ -z "$IP" ] && IP=$(hostname -I | awk '{print $1}')

echo ""
echo "=================================================="
echo "  AnvilMC is running (and will auto-start on every reboot)."
echo ""
echo "  Open this on a browser on the SAME network:"
echo ""
echo "    http://$IP:8090/?token=$TOKEN"
echo ""
echo "  >>> NOTE THIS LINK DOWN NOW — it's only printed here once. Recover it"
echo "      any time with:"
echo ""
echo "        sudo /etc/anvilmc/get-link.sh"
echo ""
echo "      That works even after a reboot changes the server's IP. If you"
echo "      don't have terminal access, the sign-in page also accepts your"
echo "      Linux username/password as a fallback (via PAM)."
echo ""
echo "  Mod Manager and Server Manager are already running too, using the same"
echo "  token — open the Installer link above and use its Home tab to reach them."
echo ""
echo "  Do NOT port-forward any of these ports to the internet — Installer"
echo "  and Server Manager can run root commands on this machine. Keep"
echo "  everything LAN-only."
echo "=================================================="
