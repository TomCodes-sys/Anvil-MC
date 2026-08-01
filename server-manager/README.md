# Anvil Server Manager — the central hub for the Anvil ecosystem

Ties the Installer and Mod Manager together: one home page with links to both, one place to check for and apply
Crafty/Docker/Cockpit updates, a fleet overview (Java and Bedrock servers alike, read from Crafty directly),
RCON, a whitelist/ops/ban manager, log tailing, health/crash monitoring, scheduled world backups (restic, local
or S3/Backblaze B2), and a Discord webhook notifier.

This is part of the [AnvilMC](../README.md) monorepo — see that top-level README for the actual install command
(`../install.sh`), which sets this app up automatically alongside the Installer and Mod Manager, sharing one
token/login and one Crafty API connection. This file covers what this specific dashboard does once it's running.

## Just want to look around first? (Windows preview)

Double-click **`Start Preview Demo.bat`**. It sets up a local Python virtual environment on first run, then
opens your browser straight into a live demo: a fake two-server fleet, preview mode already on. No
`docker`/`systemctl`/`restic` commands actually run — Crafty/Docker/Cockpit update checks, backups, restores,
and Discord notifications all stream realistic fake output, so it's completely safe to click through. Requires
[Python 3.9+](https://www.python.org/downloads/) with **"Add python.exe to PATH"** checked during setup.

## Why it runs as root

Unlike Anvil Mod Manager (which runs as a normal user, since it only ever touches mod/plugin files), this app
needs to run real `apt-get` upgrades for Docker/Cockpit and manage the Crafty Docker container — so it runs as
root, the same way Anvil Server Installer does, and is gated by the same kind of access token. **Keep it
LAN-only — never port-forward port 6161 to the internet.**

## What's here

- **Home** — links + live reachability for the Installer and Mod Manager
- **Updates** — check/apply for itself, Crafty (Docker image), Docker Engine, and Cockpit, each independent
- **Fleet** — reads Anvil Mod Manager's `data/server_*.json` files directly (read-only) for an at-a-glance table
- **RCON** — a web console per server (host/port/password saved per target) for running commands without SSH
- **Players** — a whitelist/ops/ban manager over `whitelist.json`, `ops.json`, `banned-players.json`, and
  `banned-ips.json`, with a Mojang username → UUID lookup so you never hand-craft a UUID
- **Logs** — tails each server's log file from the dashboard
- **Backups** — restic-backed scheduled backups to a local path or S3-compatible/B2 bucket, with snapshots and
  restore-to-a-staging-folder (never straight onto a live world)
- **Monitoring** — disk/SMART health, CPU temp, RAM, and network throughput, plus crash detection that watches
  for a server going down unexpectedly
- **Notifications** — a Discord webhook for backup results, update availability, health alerts, and crash detection
