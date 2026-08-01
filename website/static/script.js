// ---------------- Lucide icons ----------------
function icons() { if (window.lucide) lucide.createIcons(); }
document.addEventListener('DOMContentLoaded', icons);

// ---------------- Mobile nav ----------------
const navToggle = document.getElementById('nav-toggle');
const navLinks = document.getElementById('nav-links');
if (navToggle && navLinks) {
  navToggle.addEventListener('click', () => {
    const open = navLinks.classList.toggle('open');
    navToggle.innerHTML = open ? '<i data-lucide="x"></i>' : '<i data-lucide="menu"></i>';
    icons();
  });
  navLinks.querySelectorAll('a').forEach(a => a.addEventListener('click', () => {
    navLinks.classList.remove('open');
    navToggle.innerHTML = '<i data-lucide="menu"></i>';
    icons();
  }));
}

// ---------------- Active nav link ----------------
(function highlightActiveNav() {
  const path = location.pathname.split('/').pop() || 'index.html';
  document.querySelectorAll('.nav-link').forEach(a => {
    const href = a.getAttribute('href');
    if (href === path || (path === '' && href === 'index.html')) a.classList.add('active');
  });
})();

// ---------------- Scroll reveal ----------------
(function initReveal() {
  const items = document.querySelectorAll('.reveal');
  if (!items.length) return;
  const io = new IntersectionObserver((entries) => {
    entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
  }, { threshold: 0.15 });
  items.forEach(el => io.observe(el));
})();

// ---------------- Copy buttons on code boxes ----------------
function copySnippet(btn) {
  const box = btn.closest('.code-box');
  const codeEl = box && box.querySelector('.install-snippet');
  if (!codeEl) return;
  const text = codeEl.innerText;
  const iconCopy = btn.querySelector('.icon-copy');
  const iconCopied = btn.querySelector('.icon-copied');

  const showCopied = () => {
    btn.classList.add('copied');
    if (iconCopy) iconCopy.hidden = true;
    if (iconCopied) iconCopied.hidden = false;
    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(() => {
      btn.classList.remove('copied');
      if (iconCopy) iconCopy.hidden = false;
      if (iconCopied) iconCopied.hidden = true;
    }, 1800);
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(showCopied).catch(() => fallbackCopy(text, showCopied));
  } else {
    fallbackCopy(text, showCopied);
  }
}
function fallbackCopy(text, onDone) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (e) { /* ignore */ }
  document.body.removeChild(ta);
  onDone();
}

// ---------------- FAQ: smooth open/close instead of the native instant snap ----------------
(function initFaq() {
  document.querySelectorAll('.faq-item').forEach(item => {
    const summary = item.querySelector('summary');
    const content = item.querySelector('.faq-content');
    if (!summary || !content) return;

    if (item.open) { content.classList.add('expanded'); item.classList.add('is-open'); }

    summary.addEventListener('click', (e) => {
      e.preventDefault();
      if (item.classList.contains('is-open')) {
        // closing: animate first, then drop the native [open] state
        item.classList.remove('is-open');
        content.classList.remove('expanded');
        content.addEventListener('transitionend', function handler() {
          item.open = false;
          content.removeEventListener('transitionend', handler);
        }, { once: true });
      } else {
        // opening: set [open] immediately so content is in the DOM/measurable, then animate
        item.open = true;
        item.classList.add('is-open');
        requestAnimationFrame(() => content.classList.add('expanded'));
      }
    });
  });
})();
(function initTerminal() {
  const body = document.getElementById('terminal-body');
  if (!body) return;

  const SCRIPT = [
    { text: '$ sudo ./install.sh', cls: 'prompt' },
    { text: '>> Installing shared prerequisites...', cls: 'dim' },
    { text: '>> Anvil Server Installer is running on port 8090.', cls: 'ok' },
    { text: '>> Anvil Mod Manager is running on port 5151.', cls: 'ok' },
    { text: '>> Anvil Server Manager is running on port 6161.', cls: 'ok' },
    { text: '>> One shared token for all three — note the link down now.', cls: 'dim' },
    { text: '' },
    { text: '$ # Inside the Installer dashboard:', cls: 'prompt' },
    { text: '>> Installing Docker...', cls: 'dim' },
    { text: '>> Pulling Crafty Controller image...', cls: 'dim' },
    { text: '>> Crafty is running on port 8443.', cls: 'ok' },
    { text: '>> Backups scheduled every 24h.', cls: 'dim' },
  ];

  let cancelled = false;
  async function run() {
    while (!cancelled) {
      body.innerHTML = '';
      for (const line of SCRIPT) {
        if (cancelled) return;
        const el = document.createElement('div');
        el.className = 'terminal-line ' + (line.cls || '');
        body.appendChild(el);
        await typeLine(el, line.text);
        await sleep(line.text ? 220 : 60);
      }
      const cursor = document.createElement('span');
      cursor.className = 'terminal-cursor';
      body.lastChild && body.lastChild.appendChild(cursor);
      await sleep(2200);
    }
  }
  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  function typeLine(el, text) {
    return new Promise(resolve => {
      el.classList.add('shown');
      if (!text) return resolve();
      let i = 0;
      const speed = text.startsWith('$') ? 34 : 10;
      const iv = setInterval(() => {
        el.textContent = text.slice(0, i + 1);
        i++;
        if (i >= text.length) { clearInterval(iv); resolve(); }
      }, speed);
    });
  }

  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (prefersReducedMotion) {
    body.innerHTML = SCRIPT.map(l => `<div class="terminal-line shown ${l.cls || ''}">${l.text}</div>`).join('');
  } else {
    run();
  }
})();
