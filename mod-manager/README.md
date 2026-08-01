# Anvil Mod Manager — Mod Manager for Crafty Controller

A small dashboard that sits next to [Crafty Controller](https://craftycontrol.com/) and handles the part Crafty
doesn't: finding, installing, and updating mods, plugins, and datapacks for a specific server, from **Modrinth**
and **CurseForge**.

This is part of the [AnvilMC](../README.md) monorepo — see that top-level README for the actual install command
(`../install.sh`), which sets this app up automatically alongside the Installer and Server Manager, sharing one
token/login. This file covers what this specific dashboard does once it's running.

Runs as a normal (non-root) user on purpose — see the top-level README's "Why Mod Manager still runs as its own
(non-root) service" for why, given it's the one app here that downloads and unzips files from the internet.

Useful commands once it's running:

```bash
sudo systemctl status anvil-mod-manager     # is it running?
sudo journalctl -u anvil-mod-manager -f     # live logs
```

(Updates are applied from the Installer dashboard's Home tab, which updates all three AnvilMC apps together.)

---

## Features

- **Server Setup** — connect your Crafty URL + API token once in Settings, then just pick your server by name from
  a dropdown; its folder, Minecraft version (read from `version.json` inside the server jar), loader/platform
  (Fabric / Quilt / Forge / NeoForge for modded servers, Paper / Purpur / Spigot / Bukkit / Folia for plugin
  servers, or vanilla), and world folder name (read from `server.properties`) are all detected in one step — no
  manual browsing. An **Advanced: point at a folder manually** section is still there for non-standard installs.
- **Browse & Install** — search Modrinth or CurseForge for mods, Modrinth for plugins, or Modrinth for datapacks.
  A sidebar lets you filter by loader/platform and Minecraft version. Results are paginated 20 at a time with page
  numbers at the bottom, same as Modrinth's own site, and the whole page scrolls together — the results list simply
  extends below the sidebar rather than scrolling in its own little box.
- **Installed** — see everything you've installed through the tool, grouped by mods / plugins / datapacks, with
  icon, exact version, source, and (if it came from a modpack import) which pack it was part of.
- **Check for Updates** — re-queries both APIs for every installed item against your server's *current* configured
  version: `Up to date`, `Update available`, or `No compatible version found yet` (nothing is touched in that case).
- **Changelog preview** — click "Changelog" next to any available update to read what's actually in it (Modrinth's
  version changelog, or CurseForge's changelog endpoint) before you commit to updating.
- **Update all** — a bulk button on the Installed tab that updates every item with an update available in one go.
- **Backup-before-update, one-click revert** — every time Update (or Update all) replaces a file, the old jar is
  copied into a `.backups/` folder next to it first. If the new version misbehaves, hit **Revert** to instantly
  restore the previous file — no re-downloading needed.
- **Restart hook (via Crafty's API)** — add your Crafty URL, API token, and server ID in Settings, then use the
  **Restart server** button on the Installed tab to trigger a real restart through Crafty right after an
  install/update, so the change actually takes effect.
- **Modpack import & export** — paste a Modrinth modpack page URL or a direct `.mrpack` link (or upload a `.mrpack`
  file) on the Modpack tab to install every server-compatible mod in it in one go. Export builds a `.mrpack` from
  everything currently installed (Modrinth-sourced mods are re-resolved to fresh download links; CurseForge-only
  mods are listed in a text note inside the pack instead, since CurseForge doesn't allow redistributing its files).
- **Incompatibility warnings** — installed CurseForge mods are cross-checked against each other's declared
  "incompatible" relationships and flagged with a conflict badge. Modrinth doesn't expose this data publicly, so
  this only ever covers CurseForge-sourced mods.
- **Update notifications for the dashboard itself** — every time you open the dashboard, it checks GitHub for a
  newer commit than what's installed and shows a banner if one exists, the same way Crafty notifies you about its
  own updates. Clicking "Update now" runs `git pull` and restarts the service — this only touches files tracked in
  the app's own git repo; `data/` (your saved servers, settings, and CurseForge key) is git-ignored and is never
  touched, and none of your installed mods/plugins/datapacks live in this install directory in the first place.
- Multiple saved server profiles, remembered across visits.
- **Preview mode** — a toggle (Settings tab) that lets you try the whole flow with zero risk: search still hits the
  real Modrinth/CurseForge APIs, but install/update/remove/revert are simulated instead of touching your disk. The
  **"Load demo server"** button on Server Setup seeds a fake `Preview Server` and flips preview mode on
  automatically.

## Requirements

- Python 3.9+
- Runs on the same machine as Crafty (it reads/writes directly into your server folders, the same way Crafty itself
  does) — or anywhere with filesystem access to your Crafty `servers/<id>/` directories, e.g. over a mounted volume.
- A free [CurseForge API key](https://console.curseforge.com/) if you want CurseForge search, changelogs, or
  incompatibility warnings (Modrinth needs no key). CurseForge search covers mods only — datapacks are
  Modrinth-only.
- A Crafty URL + API token (from your Crafty user profile — Settings tab has step-by-step instructions) if you
  want the "pick your server" dropdown, live running-status dots, the restart-hook button, or the running-server
  download guard. Without it, use the Advanced manual-folder-picker instead.

## Other setup options

### Windows (for trying it out / testing)

No terminal needed. Two double-clickable launchers are included at the top of the folder:

- **`Start Preview Demo.bat`** — sets everything up on first run, then opens your browser straight into a live
  demo: a fake "Preview Server" on Minecraft 1.20.1 / Fabric, preview mode already on, and a search for "sodium"
  already run. Click around, install things — nothing touches your disk. Best first thing to try, and the fastest
  way to see the new pagination, changelog preview, bulk update, and modpack tabs without a real server.
- **`Start Anvil Mod Manager.bat`** — normal startup. Sets up a Python virtual environment the first time (takes a
  minute), then every time after that just launches the app and opens your browser to it.

Both require [Python 3.9+](https://www.python.org/downloads/) installed with **"Add python.exe to PATH"** checked
during setup — the script will tell you if it's missing. Each launcher opens a console window that's running the
server; close that window to stop it. Note: the systemd-only features (self-update restart, the Crafty restart
hook's non-preview path) are Linux/Ubuntu-specific — everything else works the same on Windows.

### macOS / Linux / manual setup

```bash
git clone https://github.com/TomCodes-sys/Anvil-Mod-Manager.git
cd Anvil-Mod-Manager
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:5151** (or the machine's IP if you're running this on your server box and browsing
from elsewhere). Set `PORT=xxxx` as an environment variable if 5151 is already taken.

## Using it

0. **First run** — the dashboard opens behind a "Connect Crafty Controller" screen and won't let you into the rest
   of the app until you either save a Crafty URL + API token (step-by-step instructions right there) or click
   **"Just exploring? Try Preview Mode instead"**, which loads a seeded demo server with no token needed. This gate
   is skipped automatically when launched via the "Preview Demo" shortcut.
1. **Settings tab** — add or update your Crafty URL + API token any time (same instructions, step-by-step).
2. **Server Setup tab** — pick your server by name from the dropdown; version/loader are detected automatically.
3. **Browse & Install tab** — search and install. Files land straight in `mods/`, `plugins/`, or
   `<world>/datapacks/`, exactly where Crafty expects them. Use the page numbers at the bottom to see more results.
4. **Modpack tab** — or import a whole pack at once instead of searching mod-by-mod.
5. After you update your server's Minecraft version, go back to **Server Setup** — re-picking it refreshes the
   detected version automatically, or edit it manually via Advanced.
6. **Installed tab** → **Check for updates**. Review changelogs before updating, use **Update all** for bulk
   updates, and **Revert** if an update causes problems. Hit **Restart server** to apply the change immediately.

## Notes on data storage

Everything the tool tracks lives in `data/server_<hash>.json` per server, plus `data/settings.json` for your
CurseForge key, Crafty API details, and saved servers. `.backups/` folders live next to the real `mods/`,
`plugins/`, and datapacks folders on your actual Crafty server, not inside this app's own directory. Nothing is
sent anywhere except Modrinth's, CurseForge's, GitHub's, and (if configured) your own Crafty instance's APIs —
there's no telemetry.

If you manually drop a jar into `mods/` outside this tool, it won't show up in the Installed tab — install it
through the dashboard instead so update-checking (and backups) can find it later.

## Limitations

- CurseForge's search API is mods-only here; browse datapacks/plugins on Modrinth.
- Incompatibility warnings only ever apply to CurseForge-sourced mods, since Modrinth doesn't publicly expose an
  equivalent "conflicts with" field.
- Modpack export can only re-link Modrinth-sourced mods (CurseForge's terms don't allow redistributing its files);
  anything else installed gets listed in a text note inside the exported pack instead of being silently dropped.
- This tool doesn't restart or manage the server process itself beyond the optional Crafty API restart hook — make
  sure the Crafty URL/token/server ID in Settings are correct if you want that button to work.

## Testing

`tests/` has a pytest suite for the pure detection logic (loader/version/Bedrock detection, dependency filtering) —
the exact kind of function where a past bug (NeoForge misidentified as Forge) actually happened. Nothing here needs
a real Crafty instance or network access.

```bash
pip install pytest
pytest
```
