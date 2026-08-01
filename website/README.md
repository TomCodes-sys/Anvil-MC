# Anvil MC — website

A static marketing/docs site for the Anvil MC toolkit (Anvil Server Installer, Anvil Mod Manager, Anvil Server Manager). Same dark/green design system as the apps themselves — no build step, no framework, just HTML/CSS/JS.

## Pages

- `index.html` — landing page (hero, the three apps, how it fits together, screenshot showcase)
- `download.html` — one section per app, with install commands and a screenshot slot each
- `tutorial.html` — numbered, start-to-finish setup walkthrough
- `prerequisites.html` — hardware/OS/network/access requirements, plus a ports reference table
- `faq.html` — accordion FAQ

## Adding your own screenshots

Drop PNG/JPG files into `assets/screenshots/` using these exact names and they'll appear automatically on the landing page and Download page (no code changes needed):

```
assets/screenshots/installer.png
assets/screenshots/mod-manager.png
assets/screenshots/server-manager.png
```

Until a file exists at that path, the frame shows a dashed placeholder telling you what's expected. 1280×800 (or any 16:10-ish ratio) looks best — the frames crop to fill, so a browser window screenshot at a normal size works fine.

## Running it locally

No server needed — just open `index.html` in a browser. If your browser is picky about `file://` pages, run a tiny local server from this folder instead:

```bash
python3 -m http.server 8000
# then open http://localhost:8000/
```

## Hosting it for real

Any static host works, since there's no backend:

- **GitHub Pages** — push this folder to a repo, enable Pages on the `main` branch
- **Netlify / Vercel** — drag-and-drop deploy, zero config
- **Your own Ubuntu Server** — `sudo apt install nginx`, then copy these files into `/var/www/html/`

## Editing content

Each page repeats its own `<header class="nav">` and `<footer class="footer">` blocks (no templating, kept intentionally simple) — if you add a new page, copy those blocks from an existing one so the nav/footer stay consistent, and add a link to it in every page's nav.
