# Anvil Server Installer

A click-through setup dashboard for running a Minecraft Java + Bedrock server on Ubuntu Server, alongside
**Crafty Controller** (server management), **Cockpit** (system management), and its companion apps
**Anvil Mod Manager** and **Anvil Server Manager**. Instead of typing every command by hand over SSH, you run one
install command, then do the rest from a browser.

This is part of the [AnvilMC](../README.md) monorepo — see that top-level README for the actual install command
(`../install.sh`), the shared token/login model, and how the three apps share updates. This file covers what this
specific dashboard's wizard actually does, once it's running: open the link `install.sh` printed (or
`sudo /etc/anvilmc/get-link.sh` to get it again) — everything from there is buttons, covered below.

---

## Trying it on Windows first (no Ubuntu server needed)

This dashboard is built to run on Ubuntu Server, but you can preview the whole wizard flow on Windows before you
touch a real server:

- **`Start Preview Demo.bat`** — double-click it. Runs the real Flask app in `ANVIL_INSTALLER_PREVIEW=1` mode:
  every step (firewall, Docker, Crafty, Cockpit, and the optional Mod Manager install) streams realistic fake
  output instead of running actual `apt`/`ufw`/`docker`/`systemctl` commands, so it's completely safe to click
  through. Requires [Python 3.9+](https://www.python.org/downloads/) with "Add python.exe to PATH" checked.
- **`preview/index.html`** — an even lighter option: a static, look-alike copy of the dashboard with no backend at
  all (just double-click the file to open it in your browser). Good for a quick look at the UI and animations
  without installing Python.

---

## Prerequisites — do these before you start

1. **Static local IP** for this server. Set a DHCP reservation in your router (preferred) using this machine's MAC
   address — the dashboard shows it to you on the Setup tab so you can copy/paste it straight in. If the IP changes
   later, your port forwards and the dashboard link both break.

2. **CGNAT must be disabled**, i.e. you need a real public IP from your ISP, so friends outside your house can
   actually reach your server. Compare your router's WAN IP to [whatismyipaddress.com](https://whatismyipaddress.com).
   If they don't match, you're behind Carrier-Grade NAT and port forwarding will not work — call your ISP (some will
   disable CGNAT on request) or use a tunnel service (e.g. playit.gg, ngrok) instead.

3. **Router access with port forwarding.** Once this server has a static IP, forward these to it:
   | Port | Protocol | Purpose |
   |---|---|---|
   | 25565 | TCP | Minecraft Java Edition |
   | 19132 | UDP | Minecraft Bedrock Edition |
   | 24454 | UDP | Simple Voice Chat |

   **Do not forward 8443 (Crafty), 9090 (Cockpit), or 5151 (Anvil Mod Manager)** to the internet — those should
   stay LAN-only. They give control over the whole machine or your mod files.

---

## Using it

1. **Configure firewall** — updates the system, disables sleep-on-lid-close, makes the physical power button trigger
   a clean shutdown (the one physical way to power the box off, besides running a shutdown command yourself), then
   opens SSH, Cockpit, Crafty, Java, Bedrock, and voice chat ports with `ufw`. SSH is explicitly re-enabled and
   allowed *before* the firewall is turned on, so you won't get locked out.
2. **Install Docker** — required by Crafty.
3. **Install Crafty Controller** — runs it in Docker on port 8443.
4. **Install Cockpit** — system dashboard on port 9090. Bundles in **Cockpit Explorer** (package `cockpit-files`,
   the actively-maintained cockpit-project file manager) automatically — there's no separate step or toggle for it.
   If an old, unmaintained 45Drives "Navigator" install is detected, it's removed first so Explorer replaces it
   cleanly.
5. **Install Anvil Mod Manager (optional)** — sets up the companion mod-management dashboard on port 5151. Skip
   this step if you'd rather manage mods by hand. Once it's installed, an **"Open Anvil Mod Manager"** button
   appears right in this step so you can jump straight to it; the dashboard also auto-detects whether it's
   installed and running every time you load the Setup tab.
6. Click **Get Crafty login** to see the auto-generated admin username and password, pulled straight from the
   container's first-boot logs.

The Crafty and Cockpit links in the top bar auto-fill with this server's local IP and the right port as soon as the
dashboard detects them.

**Advanced mode** (toggle in the top bar) removes the step locks and unlocks the Crafty/Cockpit top-bar links
immediately. The optional Mod Manager step is never locked, regardless of advanced mode, since skipping it doesn't
block anything else.

**Terminal tab** gives you a full root shell in the browser as an alternative to PuTTY/SSH, gated behind your
normal Ubuntu username/password (checked via PAM).

**Update notifications** — every time you open the dashboard, it checks GitHub for a newer commit than what's
installed and shows a banner if one exists (the same way Crafty notifies you about its own updates). Clicking
"Update now" runs `git pull` in the installer's own install directory and restarts its systemd service — this never
touches your Crafty setup, Docker containers, firewall rules, or Anvil Mod Manager install, since none of those
live inside this app's directory.

---

## What each button actually runs

Nothing here is hidden — every setup step is a fixed script in `app.py` (`FIREWALL_SCRIPT`, `DOCKER_SCRIPT`,
`CRAFTY_SCRIPT`, `COCKPIT_SCRIPT`, `MOD_MANAGER_SCRIPT`). The dashboard never executes arbitrary input from those
buttons; it only runs these predefined scripts, and streams their real terminal output back to your browser live.
The Terminal tab is the one place that runs whatever you type — that's its purpose, and it's gated behind PAM
authentication.

## Security notes

- The dashboard runs as root (via systemd) because it needs to run `ufw`, `apt`, `docker`, and `systemctl`. Treat
  its access token like a root password — it's generated once at install and stored at
  `/etc/anvilmc/token`.
- The Terminal tab raises the stakes of that token further: once unlocked, anyone with the dashboard URL has a root
  shell, no re-authentication needed. Keep the dashboard LAN-only, and use "Log out & clear saved sudo credentials"
  if you're ever unsure who has the link.
- Saved terminal credentials live encrypted at `/etc/anvilmc/sudo_session`
  (key at `/etc/anvilmc/enc.key`), both root-owned and mode 600.
- If you ever suspect the token leaked: regenerate one with
  `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`, save it to `/etc/anvilmc/token`, restart
  the service (`sudo systemctl restart anvil-installer`), and also re-run `sudo /etc/anvilmc/get-link.sh` to
  refresh the saved `dashboard_url` file so it doesn't hand out the old token. Since Anvil Server Manager shares
  this same token, it picks up the change automatically too.
- Forgot the token entirely and can't get a terminal? The dashboard's sign-in page also accepts your regular Linux
  username/password (via PAM) as a fallback — see "Trying it on Windows" note above, this needs PAM installed on
  the server (the Firewall &amp; Basics step installs it automatically).
- The "Update now" self-update button runs `git pull` only inside this app's own install directory
  (`/opt/anvilmc/installer`) — it doesn't touch `/etc/anvilmc` (your token/session files), Crafty, Docker, or
  Anvil Mod Manager.
- Your login session is signed with a key stored at `/etc/anvilmc/session_secret_installer` (root-only), generated
  once and reused — so a `systemctl restart` (including after a self-update) doesn't silently log you out. Deleting
  that file forces everyone to sign back in.

## Testing

```bash
pip install pytest
pytest
```

Covers token reading and session-secret persistence (`tests/test_auth.py`) — no real server or root access needed.

## Uninstall

Easiest: use the "Uninstall" button in this dashboard's own Home tab (Danger Zone) — it removes only what's
specific to this app, leaving the shared token/login (`/etc/anvilmc/token`) intact for Mod Manager and Server
Manager if they're still installed.

To do it by hand instead:

```bash
sudo systemctl disable --now anvil-installer
sudo rm -rf /opt/anvilmc/installer /etc/systemd/system/anvil-installer.service
sudo rm -f /etc/anvilmc/session_secret_installer /etc/anvilmc/sudo_session /etc/anvilmc/enc.key \
           /etc/anvilmc/firewall_confirmed /etc/anvilmc/firewall_autorevert.log
sudo systemctl daemon-reload
```

This does **not** remove Crafty, Docker, Cockpit, Anvil Mod Manager, or Anvil Server Manager — those are
independent of this dashboard. See the top-level [AnvilMC README](../README.md) to remove everything at once.
