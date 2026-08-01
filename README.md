# AnvilMC

A self-hosted toolkit for running and managing Minecraft servers on your own Ubuntu Server box, built around
[Crafty Controller](https://craftycontrol.com/). One download, one install, one login — three dashboards come up
together, sharing everything they need to share.

- **Anvil Server Installer** (port 8090) — click-through setup for Docker, Crafty Controller, Cockpit, and the
  other two apps below. Your front door — start here.
- **Anvil Mod Manager** (port 5151) — mod/plugin/datapack management via Modrinth and CurseForge, scoped to
  whichever Minecraft version and loader your server is actually running.
- **Anvil Server Manager** (port 6161) — backups, health/crash monitoring, RCON, whitelist/ops/bans, log tailing,
  and update-checking for the whole stack.

## Install

SSH into the Ubuntu Server box that will run Crafty, then:

```bash
rm -rf Anvil-MC 2>/dev/null   # safe to re-run — clears out any previous/partial clone first
git clone https://github.com/TomCodes-sys/Anvil-MC.git
cd Anvil-MC
sudo bash install.sh
```

(`bash install.sh` rather than `./install.sh` — this works even if the executable bit didn't survive however you got
this repo onto GitHub, e.g. a drag-and-drop web upload, which silently strips it. No need to `chmod +x` first.)

By default this sets up **all three dashboards**, sharing one access token — nothing else to configure before
you're looking at a working Installer dashboard with Mod Manager and Server Manager already running alongside it.

Want just the Installer for now, and to add the other two later yourself? Answer `advanced` at the prompt, or run:

```bash
sudo bash install.sh --advanced
```

Either way, the very first thing `install.sh` prints is a link — **note it down**, it's only shown once:

```
sudo /etc/anvilmc/get-link.sh
```

reprints all three dashboards' current links any time (survives a reboot changing the server's IP). Lost SSH
access too? Every dashboard's sign-in page also accepts your regular Linux username/password as a fallback.

## Why one repo

All three apps used to live in separate repos with separate bootstraps, separate tokens, and separate sign-ins.
Merging them doesn't change how they run — each is still its own systemd service on its own port — but it does
mean:

- **One token, one login** for all three (Mod Manager previously had no login gate at all).
- **One Crafty connection** — enter your Crafty URL/API token in either Mod Manager or Server Manager and the
  other picks it up automatically, instead of typing it in twice.
- **One update action** — the Installer's Home tab pulls this whole repo and restarts all three services in one
  click, instead of three separate "Update now" buttons.
- **Nothing to re-download** — installing Mod Manager or Server Manager later (advanced mode) just sets up a venv
  and a systemd service from the code that's already sitting right here, rather than a separate `git clone`.

## Why Mod Manager still runs as its own (non-root) service

It would be simpler still to merge all three into one process. We didn't, on purpose: Mod Manager is the one app
that downloads and unzips files from the internet (Modrinth/CurseForge), which makes it the most exposed to a
malicious or malformed file. It runs as a normal, unprivileged user precisely so that if anything ever goes wrong
in that download/extract path, the blast radius is "can mess with mod files," not "can touch the whole system."
Installer and Server Manager run as root because they genuinely need to (apt, Docker, systemctl, ufw) — Mod
Manager doesn't, so it doesn't get root.

## Security notes

- **LAN-only.** None of the three ports (8090, 5151, 6161) should ever be port-forwarded to the public internet —
  Installer and Server Manager can run root commands on this machine.
- The shared token lives at `/etc/anvilmc/token`, group-readable by a dedicated `anvilmc` group (Mod Manager needs
  to read it despite not running as root) rather than root-only. Treat it like a password regardless.
- Session cookies are signed with a key persisted to disk (`/etc/anvilmc/session_secret_*`) so a service restart
  doesn't silently log everyone out, and expire after 30 minutes of inactivity (sliding — resets on activity).

## What's in each folder

- `install.sh` — the one script that sets everything up. Re-running it is safe (idempotent): existing tokens,
  venvs, and data are left alone.
- `installer/` — Anvil Server Installer. See `installer/README.md` for what its wizard actually does step by step.
- `mod-manager/` — Anvil Mod Manager. See `mod-manager/README.md` for how mod/plugin/datapack management works.
- `server-manager/` — Anvil Server Manager. See `server-manager/README.md` for backups, monitoring, RCON, etc.
- `website/` — the marketing/docs site (static HTML, not something you run on the server itself).

## Testing

Each app has its own `tests/` (pytest, no live server or root access needed):

```bash
cd mod-manager && pip install pytest && pytest       # 39 tests
cd installer && pip install pytest && pytest          # 5 tests
cd server-manager && pip install pytest && pytest     # 10 tests
```

## Uninstalling

Each app has its own uninstall in the Installer dashboard's "Danger Zone" (Home tab). Uninstalling Mod Manager or
Server Manager stops its service and clears its venv/data, but leaves its source code in place (it's part of this
shared checkout) — reinstalling later from the same wizard is instant. Uninstalling the Installer itself only
removes its own files; the shared token/login that Mod Manager and Server Manager still depend on is left alone.

To remove everything, including Crafty and Docker: use each Danger Zone entry individually, then:

```bash
sudo systemctl disable --now anvil-installer anvil-mod-manager anvil-server-manager
sudo rm -rf /opt/anvilmc /etc/anvilmc /etc/pam.d/anvilmc-auth /etc/systemd/system/anvil-*.service
sudo systemctl daemon-reload
```

This does **not** touch your actual Minecraft worlds/servers under `/opt/crafty/servers` — those are independent
of all three dashboards.
