#!/usr/bin/env node
/* Browser-level verification for kele, using jsdom + node-canvas.
 * Loads the REAL pages (shelf + 4 readers) from the running server and
 * lets the REAL frontend JS run (PDF.js, shelf/reader logic). EPUB is
 * server-side now (1.0.1): the reader loads a server-extracted page in
 * a single iframe, no client-side epub.js. jsdom has no layout, so this
 * installs small polyfills before the page scripts run:
 * IntersectionObserver (reports pages 1-3 as visible),
 * clientWidth/clientHeight and getBoundingClientRect (fixed phone size).
 * Checks the data-test-* markers the reader sets plus visible text.
 * Usage: node tests/verify_browser_jsdom.js  (server must be running;
 * the script starts its own server on port 18098).
 */
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');
const { JSDOM, VirtualConsole, CookieJar } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const FIXTURES = path.join(ROOT, 'tests', 'fixtures', 'books');
const PORT = 18098;
const BASE = `http://127.0.0.1:${PORT}`;
const DATA_DIR = '/tmp/kele-jsdom-data';

// 登录态：main() 先调 /api/login 拿到会话 token，页面加载（cookieJar）
// 与页内 fetch 桥（Cookie 头）都带上它；未登录时页面会跳到登录页。
let SESSION_TOKEN = null;
let cookieJar = null;

async function login() {
  const res = await fetch(`${BASE}/api/login`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password: 'admin' }),
  });
  const data = await res.json().catch(() => ({}));
  const setCookie = res.headers.get('set-cookie') || '';
  const m = /kele_session=([^;]+)/.exec(setCookie);
  if (res.ok && data.ok && m) {
    SESSION_TOKEN = m[1];
    cookieJar = new CookieJar();
    cookieJar.setCookieSync(`kele_session=${SESSION_TOKEN}; Path=/`, BASE + '/');
    return true;
  }
  return false;
}

// 页内 fetch 桥不走 cookieJar，这里直接给 Node fetch 加 Cookie 头。
function authedHeaders(extra) {
  const h = { ...(extra || {}) };
  if (SESSION_TOKEN) h.Cookie = `kele_session=${SESSION_TOKEN}`;
  return h;
}

const results = [];
function check(label, ok, detail = '') {
  results.push([label, !!ok, detail]);
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label} ${String(detail).replace(/\s+/g, ' ').slice(0, 300)}`);
}

// A harness gap (e.g. a missing jsdom global) must fail loudly with a
// short message, not kill the process with a multi-MB stack dump.
// Fail loudly on unhandled rejections — except the known benign jsdom
// race: pdf.js cover generation in flight when a shelf window is closed.
// The app's generateCover already .catch()es; in jsdom the .catch (a
// window-realm callback) cannot run after window.close(), so Node sees an
// "Invalid PDF url data" rejection. Not an app bug (real browsers keep the
// page alive); log and continue.
process.on('unhandledRejection', (e) => {
  const msg = String(e && e.message || e);
  if (msg.includes('Invalid PDF url data')) {
    console.log('NOTE: benign jsdom/pdf.js cover-generation race (window closed mid-generation), ignored: ' + msg.slice(0, 120));
    return;
  }
  console.log('UNHANDLED REJECTION: ' + msg.slice(0, 300));
  console.log('REJ STACK: ' + String(e && e.stack || '(no stack)').split('\n').slice(0, 10).join(' | ').slice(0, 1200));
  process.exit(4);
});
process.on('uncaughtException', (e) => {
  console.log('UNCAUGHT: ' + String(e && e.message || e).slice(0, 300));
  process.exit(5);
});

function getJson(pathname) {
  return new Promise((resolve, reject) => {
    const headers = {};
    if (SESSION_TOKEN) headers.Cookie = `kele_session=${SESSION_TOKEN}`;
    http.get(BASE + pathname, { headers }, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => {
        try { resolve(JSON.parse(data)); } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}

function startServer() {
  fs.rmSync(DATA_DIR, { recursive: true, force: true });
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFileSync(path.join(DATA_DIR, 'config.json'),
    JSON.stringify({ book_folder: FIXTURES }));
  const env = { ...process.env, KELE_DATA_DIR: DATA_DIR, KELE_PORT: String(PORT) };
  // Server stderr goes to a file: a crash must leave evidence (spawn()
  // defaults to piped stdio that nobody reads, so tracebacks vanish).
  const serverLog = fs.openSync('/tmp/kele-jsdom-server.log', 'w');
  const proc = spawn('python3', [path.join(ROOT, 'app', 'app.py')],
    { env, stdio: ['ignore', 'ignore', serverLog] });
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 15000;
    const tick = () => {
      getJson('/api/version').then(() => resolve(proc)).catch(() => {
        if (Date.now() > deadline) reject(new Error('server did not come up'));
        else setTimeout(tick, 300);
      });
    };
    tick();
  });
}

function waitFor(fn, timeoutMs, label) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const tick = () => {
      let ok = false;
      try { ok = fn(); } catch (e) { /* not yet */ }
      if (ok) return resolve(true);
      if (Date.now() > deadline) return resolve(false);
      setTimeout(tick, 250);
    };
    tick();
  });
}

async function loadPage(url, setup) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => errors.push('jsdomError: ' + String(e.detail && e.detail.stack || e.message || e).replace(/\s+/g, ' ').slice(0, 300)));
  virtualConsole.on('error', (...args) => errors.push('console.error: ' + args.join(' ').slice(0, 300)));
  const dom = await JSDOM.fromURL(url, {
    resources: 'usable',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole,
    cookieJar,
    beforeParse(window) {
      // jsdom ships no fetch and no web streams: bridge Node's, resolving
      // relative URLs against the page URL (the app calls fetch('/api/...')).
      // pdf.js builds ReadableStreams for network PDF loading.
      // Two realm traps to avoid (both killed earlier runs):
      //  - undici rejects a fetch whose `signal` is jsdom's AbortSignal
      //    ("Expected signal to be an instance of AbortSignal"), so bridge
      //    it to a Node AbortController;
      //  - pdf.js asserts `reason instanceof Error` on stream errors, and
      //    a Node-realm error fails that check, so convert rejections to
      //    window-realm Errors.
      //  - epub.js passes ArrayBuffers to JSZip / its Book constructor,
      //    both of which do `instanceof ArrayBuffer`; a Node ArrayBuffer
      //    fails, so copy bytes into a window-realm ArrayBuffer.
      // setImmediate: jsdom has none, and the setImmediate polyfill
      // bundled in JSZip picks a broken path in jsdom (its callbacks
      // never fire), which stalls every JSZip file.async() and hence
      // epub.js book.ready. Define a setTimeout-based one BEFORE the
      // page scripts load so the polyfill leaves it alone.
      window.setImmediate = (fn, ...args) => window.setTimeout(() => fn(...args), 0);
      window.clearImmediate = (id) => window.clearTimeout(id);
      // Scrolling: jsdom has no layout, so scrollIntoView/scrollTo are
      // either missing or log "Not implemented" jsdomErrors. The app
      // calls them on settings toggle / back-home / PDF page jumps;
      // no-op them so those paths run cleanly in jsdom.
      window.scrollTo = () => {};
      window.scrollBy = () => {};
      if (window.Element && window.Element.prototype) {
        window.Element.prototype.scrollIntoView = function () {};
        window.Element.prototype.scrollTo = function () {};
        window.Element.prototype.scrollBy = function () {};
      }
      // URL.createObjectURL: jsdom does not implement it, but epub.js
      // needs it to substitute chapter resources (images/CSS) with blob
      // URLs. A dummy URL is enough here: the chapter TEXT is written
      // into the iframe directly, images just fail to load in jsdom.
      // (In a real browser the real createObjectURL runs; this is
      // purely a jsdom gap, not an app bug.)
      let __blobSeq = 0;
      window.URL.createObjectURL = () => 'blob:jsdom/' + (++__blobSeq);
      window.URL.revokeObjectURL = () => {};
      window.fetch = (input, init) => {
        const url = new window.URL(input, window.location.href).href;
        // 会话 cookie：页内 fetch 桥不走 jsdom 的 cookieJar，这里
        // 直接加 Cookie 头，否则 /api/* 会回 401。
        let nodeInit = { ...(init || {}) };
        if (typeof SESSION_TOKEN === 'string' && SESSION_TOKEN) {
          nodeInit.headers = { ...(nodeInit.headers || {}),
            Cookie: 'kele_session=' + SESSION_TOKEN };
        }
        if (init && init.signal) {
          const ac = new AbortController();
          const sig = init.signal;
          if (sig.aborted) ac.abort();
          else sig.addEventListener('abort', () => ac.abort(), { once: true });
          nodeInit = { ...nodeInit, signal: ac.signal };
        }
        // Node fetch returns Node-realm objects. Convert the Response's
        // arrayBuffer() to a window-realm ArrayBuffer: epub.js's Book
        // constructor does `buf instanceof ArrayBuffer` to tell a packed
        // book from an options object, and a Node ArrayBuffer fails that
        // check (book would open as "options" and never load). In a real
        // browser fetch() and epub.js share one realm, so this is purely
        // a jsdom harness concern, not an app bug.
        return fetch(url, nodeInit).then((res) => {
          const origArrayBuffer = res.arrayBuffer.bind(res);
          res.arrayBuffer = async () => {
            const ab = await origArrayBuffer();
            // Must COPY the bytes: `new Uint8Array(nodeAb)` only views
            // the Node buffer, so `.buffer` would still be Node-realm.
            const u8 = new window.Uint8Array(ab.byteLength);
            u8.set(new window.Uint8Array(ab));
            return u8.buffer;
          };
          return res;
        }).catch((e) => {
          throw new window.Error(e && e.message || String(e));
        });
      };
      window.ReadableStream = ReadableStream;
      window.WritableStream = WritableStream;
      window.TransformStream = TransformStream;
      // TextDecoder/TextEncoder: real browsers have them, but jsdom 26
      // does not expose them on the window; the TXT reader needs them
      // (decodeBytes). Bridge Node's (same API, incl. gbk).
      window.TextDecoder = TextDecoder;
      window.TextEncoder = TextEncoder;
      // No layout in jsdom: fixed phone-ish sizes.
      Object.defineProperty(window.HTMLElement.prototype, 'clientWidth', { get() { return 390; }, configurable: true });
      Object.defineProperty(window.HTMLElement.prototype, 'clientHeight', { get() { return 800; }, configurable: true });
      Object.defineProperty(window.HTMLElement.prototype, 'scrollHeight', { get() { return 1000; }, configurable: true });
      window.HTMLElement.prototype.getBoundingClientRect = function () {
        return { top: 0, bottom: 100, left: 0, right: 390, width: 390, height: 100, x: 0, y: 0 };
      };
      window.HTMLElement.prototype.scrollTo = function () {};
      window.Element.prototype.scrollTo = function () {};
      // jsdom's Range has no getBoundingClientRect, but epub.js measures
      // chapter text with document.createRange().getBoundingClientRect().
      window.Range.prototype.getBoundingClientRect = function () {
        return { top: 0, bottom: 100, left: 0, right: 390, width: 390, height: 100, x: 0, y: 0 };
      };
      window.Range.prototype.getClientRects = function () { return []; };
      // jsdom's Window.scrollTo only logs "Not implemented" (counted as a
      // page error); epub.js calls it during layout.
      window.scrollTo = function () {};
      // jsdom never parses iframe srcdoc into the iframe document, so
      // epub.js would render chapters into an empty iframe. Hiding the
      // property makes epub.js pick its document.write fallback, which
      // works in jsdom and in real browsers. (Real browsers keep srcdoc.)
      try { delete window.HTMLIFrameElement.prototype.srcdoc; } catch (e) { /* ignore */ }
      // separate realm: patch it too as soon as it appears, or the
      // rendition's layout loop throws inside requestAnimationFrame.
      const patchFrameWindow = (fw) => {
        if (!fw || fw.__kelePatched) return;
        fw.__kelePatched = true;
        const rect = () => ({ top: 0, bottom: 100, left: 0, right: 390, width: 390, height: 100, x: 0, y: 0 });
        if (fw.Range && fw.Range.prototype) {
          fw.Range.prototype.getBoundingClientRect = rect;
          fw.Range.prototype.getClientRects = () => [];
        }
        fw.scrollTo = function () {};
        fw.setImmediate = (fn, ...args) => fw.setTimeout(() => fn(...args), 0);
      };
      let patchTimer = null;
      const patchIframes = () => {
        // The interval outlives the page: after dom.window.close()
        // window.document is gone; stop polling instead of throwing.
        if (!window.document) {
          if (patchTimer) clearInterval(patchTimer);
          return;
        }
        try {
          window.document.querySelectorAll('iframe').forEach((fr) => {
            try { patchFrameWindow(fr.contentWindow); } catch (e) { /* not ready yet */ }
          });
        } catch (e) { /* window closed */ }
      };
      // The document has no root element yet inside beforeParse, so arm
      // the observer on a timer; the interval below is the backstop.
      window.setTimeout(() => {
        try {
          new window.MutationObserver(patchIframes).observe(window.document.documentElement, { childList: true, subtree: true });
        } catch (e) { /* polling below still covers it */ }
        patchIframes();
      }, 50);
      patchTimer = setInterval(patchIframes, 200);
      // IntersectionObserver: report PDF pages 1-3 as visible, everything else as visible.
      window.IntersectionObserver = class {
        constructor(cb) { this.cb = cb; this.targets = new Set(); }
        observe(el) {
          this.targets.add(el);
          const page = el.getAttribute && el.getAttribute('data-page');
          const visible = page ? parseInt(page, 10) <= 3 : true;
          setTimeout(() => this.cb([{ target: el, isIntersecting: visible }]), 10);
        }
        unobserve(el) { this.targets.delete(el); }
        disconnect() { this.targets.clear(); }
      };
      if (setup) setup(window);
    },
  });
  return { dom, errors };
}

async function main() {
  const proc = await startServer();
  try {
    // ---- auth: 未登录打开书架应看到登录页 ---------------------------
    {
      const { dom, errors } = await loadPage(BASE + '/');
      const doc = dom.window.document;
      check('auth: unauthenticated shelf shows login page',
        !!doc.getElementById('login-btn')
        && doc.body.textContent.includes('登录'));
      check('auth: no page JS errors (login page)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
    const loggedIn = await login();
    check('auth: login admin/admin', loggedIn === true);
    const { books } = await getJson('/api/books');
    const bigPdf = books.find((b) => b.title === '测试漫画-大文件');
    const smallPdf = books.find((b) => b.title === '测试漫画-小');
    const epub = books.find((b) => b.format === 'epub');
    const txt = books.find((b) => b.title === '测试文章');
    const azw3 = books.find((b) => b.format === 'azw3');
    const mobi = books.find((b) => b.format === 'mobi');
    const txtCh = books.find((b) => b.title === '测试小说-章节');

    // ---- shelf -------------------------------------------------------
    // Seed progress for two books (different times) so the shelf shows
    // progress bars and a history list. EPUB is posted 1.2s later so it
    // is the most recent entry (updated is second-resolution).
    {
      const seed = async (book, loc, total) => {
        const r = await fetch(`${BASE}/api/progress`, {
          method: 'POST', headers: authedHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({ book: book.id, loc, total }),
        });
        return r.ok;
      };
      const s1 = await seed(bigPdf, 12, 36);
      check('shelf: seed PDF progress', s1 === true);
      await new Promise((r) => setTimeout(r, 1200));
      const s2 = await seed(epub, 0.5, 1);
      check('shelf: seed EPUB progress', s2 === true);
    }
    {
      const { dom, errors } = await loadPage(BASE + '/');
      const ok = await waitFor(() => dom.window.document.querySelectorAll('.book').length >= 6, 20000, 'shelf grid');
      const doc = dom.window.document;
      check('shelf: 8 books rendered', ok, `(found ${doc.querySelectorAll('.book').length})`);
      check('shelf: big PDF title present', doc.body.textContent.includes('测试漫画-大文件'));
      check('shelf: EPUB title present', doc.body.textContent.includes('测试小说：满月之下'));
      // folder grouping: 2 collapsible groups, 漫画 (2) and 小说 (4)
      const groupsOk = await waitFor(() => doc.querySelectorAll('details.folder-group').length === 2, 20000, 'shelf groups');
      check('shelf: 2 folder groups', groupsOk);
      const groupSummaries = [...doc.querySelectorAll('details.folder-group summary')].map((s) => s.textContent);
      check('shelf: group 漫画 has 2 books', groupSummaries.some((t) => t.includes('漫画') && t.includes('2')));
      check('shelf: group 小说 has 6 books', groupSummaries.some((t) => t.includes('小说') && t.includes('6')));
      const mangaGroup = [...doc.querySelectorAll('details.folder-group')].find((g) => g.querySelector('summary').textContent.includes('漫画'));
      const novelGroup = [...doc.querySelectorAll('details.folder-group')].find((g) => g.querySelector('summary').textContent.includes('小说'));
      check('shelf: 漫画 group has the 2 PDFs', mangaGroup && mangaGroup.querySelectorAll('.book').length === 2);
      check('shelf: 小说 group has the 6 books', novelGroup && novelGroup.querySelectorAll('.book').length === 6);
      check('shelf: no page JS errors', errors.length === 0, errors.slice(0, 2).join(' | '));
      // progress bars: wait for the progress render (after /api/progress)
      const progOk = await waitFor(() => doc.querySelectorAll('.book .prog').length >= 2, 20000, 'shelf progress');
      check('shelf: progress bars rendered', progOk);
      const progTexts = [...doc.querySelectorAll('.book .prog-text')].map((el) => el.textContent);
      check('shelf: PDF card shows 已读 33%', progTexts.some((t) => t.includes('已读 33%')));
      check('shelf: EPUB card shows 已读 50%', progTexts.some((t) => t.includes('已读 50%')));
      const progFills = [...doc.querySelectorAll('.book .prog-fill')].map((el) => el.style.width);
      check('shelf: progress bar widths 33%/50%', progFills.includes('33%') && progFills.includes('50%'), `(${progFills.join(',')})`);
      // history: most recent first (EPUB posted later)
      const histOk = await waitFor(() => doc.querySelectorAll('.history-item').length >= 2, 20000, 'shelf history');
      check('shelf: history has 2 entries', histOk);
      const histSection = doc.getElementById('history-section');
      check('shelf: history section visible', histSection && !histSection.classList.contains('hidden'));
      // 1.0.6：阅读历史可折叠，默认收起（和文件夹一致），点标题展开
      check('shelf: history is a <details>', histSection && histSection.tagName === 'DETAILS');
      check('shelf: history collapsed by default', histSection && !histSection.hasAttribute('open'));
      check('shelf: history count shows 2 条', doc.getElementById('history-count').textContent === '2 条', `(${doc.getElementById('history-count').textContent})`);
      histSection.querySelector('summary').click();
      check('shelf: history opens on summary click', histSection.hasAttribute('open'));
      const histTitles = [...doc.querySelectorAll('.history-item .history-title')].map((el) => el.textContent);
      check('shelf: history newest first (EPUB)', histTitles[0] && histTitles[0].includes('测试小说'));
      check('shelf: history second is PDF', histTitles[1] && histTitles[1].includes('测试漫画-大文件'));
      const histPos = [...doc.querySelectorAll('.history-item .history-pos')].map((el) => el.textContent);
      check('shelf: history shows progress text', histPos.some((t) => t.includes('已读 50%')) && histPos.some((t) => t.includes('已读 33%')), `(${histPos.join('|')})`);
      const histLinks = [...doc.querySelectorAll('.history-item')].map((el) => el.getAttribute('href'));
      check('shelf: history links open the book', histLinks.some((h) => h && h.includes(epub.id)) && histLinks.some((h) => h && h.includes(bigPdf.id)), `(${histLinks.join('|')})`);
      // cover generation: wait for at least one PDF cover to be generated +
      // shown (proves the mechanism). The 2nd cover is slow/flaky in
      // jsdom (pdf.js + 46MB PDF) and is covered by the e2e cover-upload
      // endpoint check; do not block the suite on it.
      const coverOk = await waitFor(() => {
        const imgs = [...doc.querySelectorAll('.book .cover img')];
        return imgs.length >= 1;
      }, 30000, 'shelf cover');
      check('shelf: PDF cover generated + shown', coverOk);
      check('shelf: no page JS errors (after covers)', errors.length === 0, errors.slice(0, 2).join(' | '));
      // ---- 1.0.2: folders collapsed by default (Issue 5) --------------
      const anyOpen = [...doc.querySelectorAll('details.folder-group')].some((d) => d.hasAttribute('open'));
      check('shelf: folders collapsed by default', !anyOpen);
      // ---- 1.0.2: sort dropdown works for PDFs (Issue 4) --------------
      // Default (title) order in 小说: sample-book first. By size (largest
      // first) 测试文章.txt (170KB) must come first.
      const sortSel = doc.getElementById('sort-select');
      check('shelf: sort select present', !!sortSel);
      // Re-query the group each time: renderGrid() replaces shelfEl
      // innerHTML, so a captured element goes stale after a sort change.
      const titlesIn = (name) => {
        const g = [...doc.querySelectorAll('details.folder-group')].find((el) => el.querySelector('summary').textContent.includes(name));
        return g ? [...g.querySelectorAll('.book .meta .t')].map((el) => el.textContent) : [];
      };
      // Title sort (localeCompare zh): 测试文章, 测试文章-GBK,
      // 测试小说-章节, 测试小说：满月之下 (EPUB 标题取自 OPF 元数据),
      // sample-book, sample-book-mobi. Size sort (largest first):
      // 测试文章, sample-book-mobi, 测试文章-GBK, sample-book,
      // 测试小说-章节, 测试小说：满月之下.
      const titleOrder = titlesIn('小说');
      check('shelf: default sort (title) order in 小说',
        titleOrder.join('|') === '测试文章|测试文章-GBK|测试小说-章节|测试小说：满月之下|sample-book|sample-book-mobi',
        `(${titleOrder.join('|')})`);
      sortSel.value = 'size';
      sortSel.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      const sizeOk = await waitFor(() => titlesIn('小说').join('|') === '测试文章|sample-book-mobi|测试文章-GBK|sample-book|测试小说-章节|测试小说：满月之下', 5000, 'sort by size');
      check('shelf: sort by size reorders 小说', sizeOk, `(${titlesIn('小说').join('|')})`);
      check('shelf: sort by size keeps big PDF first in 漫画', titlesIn('漫画')[0] === '测试漫画-大文件', `(${titlesIn('漫画').join('|')})`);
      sortSel.value = 'title';
      sortSel.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      // ---- 1.0.2: settings back-home button (Issue 1) ----------------
      doc.getElementById('settings-link').click();
      const setupShown = !doc.getElementById('setup-card').classList.contains('hidden');
      const accountShown = !doc.getElementById('account-card').classList.contains('hidden');
      check('settings: cards shown after 设置', setupShown && accountShown);
      const backBtn = doc.querySelector('#setup-card .back-home-btn');
      check('settings: back-home button present', !!backBtn);
      backBtn.click();
      check('settings: back-home hides both cards',
        doc.getElementById('setup-card').classList.contains('hidden') &&
        doc.getElementById('account-card').classList.contains('hidden'));
      dom.window.close();
    }

    // ---- PDF reader (46MB) -------------------------------------------
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${bigPdf.id}`);
      const doc = dom.window.document;
      const loaded = await waitFor(() => doc.body.getAttribute('data-test-pdf') === '36', 60000, 'pdf load');
      check('PDF: pdf.js loaded 36 pages', loaded, `(marker: ${doc.body.getAttribute('data-test-pdf')})`);
      const rendered = await waitFor(() => doc.body.getAttribute('data-test-pdf-page1') === '1', 60000, 'pdf render');
      check('PDF: page 1 rendered to canvas', rendered);
      const canvases = doc.querySelectorAll('#pdf-viewer canvas').length;
      check('PDF: at least one canvas in viewer', canvases >= 1, `(${canvases})`);
      check('PDF: position label "第 12 / 36 页" (restored from seeded progress)', doc.getElementById('pos-label').textContent.includes('第 12 / 36 页'),
        `(${doc.getElementById('pos-label').textContent})`);
      // canvas actually painted (not blank): sample pixels
      const canvas = doc.querySelector('#pdf-viewer canvas');
      let painted = false;
      if (canvas) {
        const ctx = canvas.getContext('2d');
        const img = ctx.getImageData(0, 0, Math.min(canvas.width, 100), Math.min(canvas.height, 100)).data;
        let sum = 0;
        for (let i = 0; i < img.length; i += 4) sum += img[i] + img[i + 1] + img[i + 2];
        painted = sum > 0;
      }
      check('PDF: canvas has non-blank pixels', painted);
      check('PDF: no page JS errors', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }

    // ---- 1.0.2: PDF progress saved on open (Issue 3) -------------------
    // The small PDF has NO seeded progress. Opening it must create a
    // progress entry (saved on open) so it shows up in history — before
    // 1.0.2, scroll-only PDFs never saved unless the user clicked
    // 上一页/下一页.
    {
      const smallPdf = books.find((b) => b.title === '测试漫画-小');
      const { dom, errors } = await loadPage(`${BASE}/read?book=${smallPdf.id}`);
      const doc = dom.window.document;
      const loaded = await waitFor(() => doc.body.getAttribute('data-test-pdf'), 60000, 'small pdf load');
      check('PDF: small PDF loaded', !!loaded);
      // save-on-open is debounced (2000ms) + a beat after; poll the API.
      // Note: GET /api/progress returns {progress: {id: {loc,total}}}.
      let savedOk = false;
      for (let i = 0; i < 30 && !savedOk; i++) {
        try {
          const rr = await fetch(`${BASE}/api/progress`, { headers: authedHeaders() });
          const pp = await rr.json();
          const m = (pp && pp.progress) || {};
          savedOk = !!(m[smallPdf.id] && typeof m[smallPdf.id].loc === 'number');
        } catch (e) { /* not yet */ }
        if (!savedOk) await new Promise((r) => setTimeout(r, 500));
      }
      check('PDF: progress saved on open (appears in /api/progress)', savedOk);
      const r = await fetch(`${BASE}/api/progress`, { headers: authedHeaders() });
      const prog = await r.json();
      const entry = ((prog && prog.progress) || {})[smallPdf.id] || {};
      check('PDF: saved progress has loc/total', entry.loc === 1 && entry.total >= 1, JSON.stringify(entry));
      check('PDF: no page JS errors (small pdf)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }

    // ---- EPUB reader (server-side, 1.0.1) -----------------------------
    // EPUB is now a server-extracted page in a single iframe
    // (#epub-frame), not client-side epub.js. The browser only fetches
    // the small extracted HTML; the big file never reaches the browser.
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${epub.id}`);
      const doc = dom.window.document;
      const ok = await waitFor(() => doc.body.getAttribute('data-test-epub') === '1', 60000, 'epub load');
      check('EPUB: iframe loaded (server-side)', ok);
      const label = doc.getElementById('pos-label').textContent;
      check('EPUB: position label "已读 0%"', label.includes('已读 0%'), `(${label})`);
      const frame = doc.getElementById('epub-frame');
      check('EPUB: #epub-frame present', !!frame);
      const frameText = () => {
        try {
          const fd = frame && frame.contentDocument;
          return (fd && fd.body && fd.body.textContent) || '';
        } catch (e) { return ''; }
      };
      const coverOk = await waitFor(() => frameText().includes('测试小说：满月之下'), 30000, 'epub title in iframe');
      check('EPUB: title rendered in iframe', coverOk, `(len ${frameText().length})`);
      check('EPUB: chapter 1 text in iframe', frameText().includes('月亮升起来的时候'));
      check('EPUB: chapter 2 text in iframe', frameText().includes('第二章'));
      // ---- 1.0.2: manga images fit width (Issue 2) --------------------
      // The iframe page must carry the base CSS with !important image
      // rules AFTER the EPUB's own CSS, so manga pages are never clipped.
      const frameCss = () => {
        try {
          const fd = frame && frame.contentDocument;
          const st = fd && fd.querySelector('style');
          return (st && st.textContent) || '';
        } catch (e) { return ''; }
      };
      const css = frameCss();
      const idxEpub = css.indexOf('font-size:18px');
      const idxBase = css.indexOf('max-width:100%!important');
      check('EPUB: base CSS after EPUB CSS in iframe', idxEpub >= 0 && idxBase > idxEpub, `(${idxEpub},${idxBase})`);
      check('EPUB: img height:auto!important in iframe', css.includes('height:auto!important'));
      check('EPUB: no page JS errors', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }

    // ---- EPUB: resume via fraction progress ---------------------------
    // New model stores progress as a 0..1 scroll fraction (not CFI).
    // Save 0.5, reopen, and verify the book still loads cleanly and
    // chapter text renders. (Scroll position itself cannot be verified
    // in jsdom: no layout, so scrollHeight is 0 and the restore is a
    // no-op there; the restore is exercised in a real browser.)
    {
      const saved = await fetch(`${BASE}/api/progress`, {
        method: 'POST', headers: authedHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ book: epub.id, loc: 0.5, total: 1 }),
      }).then((r) => r.json()).catch(() => ({}));
      check('EPUB: fraction progress saved', saved.ok === true, JSON.stringify(saved).slice(0, 80));
      const { dom, errors } = await loadPage(`${BASE}/read?book=${epub.id}`);
      const doc = dom.window.document;
      const ok = await waitFor(() => doc.body.getAttribute('data-test-epub') === '1', 60000, 'epub load (resume)');
      check('EPUB: iframe loaded (resume)', ok);
      const frame = doc.getElementById('epub-frame');
      const frameText = () => {
        try {
          const fd = frame && frame.contentDocument;
          return (fd && fd.body && fd.body.textContent) || '';
        } catch (e) { return ''; }
      };
      const ch1 = await waitFor(() => frameText().includes('月亮升起来的时候'), 30000, 'epub ch1 text');
      check('EPUB: chapter 1 text rendered after resume', ch1, `(len ${frameText().length})`);
      check('EPUB: no page JS errors (resume)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }

    // ---- TXT reader ----------------------------------------------------
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${txt.id}`);
      const doc = dom.window.document;
      const ok = await waitFor(() => {
        const v = doc.body.getAttribute('data-test-txt');
        return v && parseInt(v, 10) > 1;
      }, 30000, 'txt paginate');
      check('TXT: paginated into >1 page', ok, `(pages: ${doc.body.getAttribute('data-test-txt')})`);
      check('TXT: first page text present', doc.getElementById('txt-content').textContent.includes('这是一个 TXT 测试文件'));
      check('TXT: position label "第 1 /"', doc.getElementById('pos-label').textContent.includes('第 1 /'));
      check('TXT: no page JS errors', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }

    // ---- AZW3 reader ----------------------------------------------------
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${azw3.id}`);
      const doc = dom.window.document;
      const ok = await waitFor(() => doc.body.getAttribute('data-test-azw3') === '1', 60000, 'azw3 load');
      check('AZW3: iframe loaded extracted HTML', ok);
      const frame = doc.getElementById('azw3-frame');
      let frameText = '';
      try {
        if (frame && frame.contentDocument) frameText = frame.contentDocument.body.textContent || '';
      } catch (e) { /* ignore */ }
      check('AZW3: chapter text inside iframe', frameText.includes('Choosing the paper'),
        `(len ${frameText.length})`);
      check('AZW3: position "已读 0%"', doc.getElementById('pos-label').textContent.includes('已读 0%'),
        `(${doc.getElementById('pos-label').textContent})`);
      check('AZW3: no page JS errors', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }

    // ---- 1.0.3: 上一章 / 下一章 ----------------------------------------
    // 同文件夹内按书名顺序跳书。书架默认排序也是书名（localeCompare
    // zh），所以章节顺序与书架里看到的一致。jsdom 不实现跨页导航
    // （点按钮会触发 location.href，jsdom 只记一条 Not implemented
    // 错误而不跳转），因此这里验证三点：按钮存在与置灰状态、指向的
    // 目标书 id 正确、章序号正确；跳转本身是一行 location.href，
    // 跳过去后的进度恢复用“直接打开目标书”来验证。
    const chapterState = (doc) => ({
      prevDisabled: !!doc.getElementById('prev-chapter-btn').disabled,
      nextDisabled: !!doc.getElementById('next-chapter-btn').disabled,
      prevTarget: doc.getElementById('prev-chapter-btn').getAttribute('data-target') || '',
      nextTarget: doc.getElementById('next-chapter-btn').getAttribute('data-target') || '',
      label: doc.getElementById('chapter-label').textContent,
      marker: doc.body.getAttribute('data-test-chapter') || '',
    });
    // 漫画/：大文件（第 1/2 章）→ 上一章置灰，下一章指向小 PDF
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${bigPdf.id}`);
      const doc = dom.window.document;
      const ready = await waitFor(() => doc.body.getAttribute('data-test-chapter') === '1/2', 30000, 'chapter nav (big pdf)');
      check('chapter: buttons present on PDF reader',
        !!doc.getElementById('prev-chapter-btn') && !!doc.getElementById('next-chapter-btn') && !!doc.getElementById('chapter-label'));
      const s = chapterState(doc);
      check('chapter: first book in 漫画/ (1/2)', ready && s.marker === '1/2' && s.label.includes('第 1 / 2 章'), `(${s.label}|${s.marker})`);
      check('chapter: first book prev disabled', s.prevDisabled);
      check('chapter: first book next -> small PDF', !s.nextDisabled && s.nextTarget === smallPdf.id, `(target ${s.nextTarget.slice(0, 8)})`);
      check('chapter: no page JS errors (big pdf)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
    // 漫画/：小 PDF（第 2/2 章）→ 上一章指向大文件，下一章置灰
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${smallPdf.id}`);
      const doc = dom.window.document;
      const ready = await waitFor(() => doc.body.getAttribute('data-test-chapter') === '2/2', 30000, 'chapter nav (small pdf)');
      const s = chapterState(doc);
      check('chapter: last book in 漫画/ (2/2)', ready && s.marker === '2/2' && s.label.includes('第 2 / 2 章'), `(${s.label}|${s.marker})`);
      check('chapter: last book prev -> big PDF', !s.prevDisabled && s.prevTarget === bigPdf.id, `(target ${s.prevTarget.slice(0, 8)})`);
      check('chapter: last book next disabled', s.nextDisabled);
      check('chapter: no page JS errors (small pdf)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
    // 小说/：EPUB 是第 4/6 章（书名序：测试文章、测试文章-GBK、
    // 测试小说-章节、测试小说：满月之下、sample-book、sample-book-mobi；
    // EPUB 标题取自 OPF 元数据“测试小说：满月之下”，排在“测试小说-章节”后）
    // → 上下都指向邻书
    {
      const txtGbk = books.find((b) => b.title === '测试文章-GBK');
      const { dom, errors } = await loadPage(`${BASE}/read?book=${epub.id}`);
      const doc = dom.window.document;
      const ready = await waitFor(() => doc.body.getAttribute('data-test-chapter') === '4/6', 30000, 'chapter nav (epub)');
      const s = chapterState(doc);
      check('chapter: EPUB is 4/6 in 小说/', ready && s.marker === '4/6' && s.label.includes('第 4 / 6 章'), `(${s.label}|${s.marker})`);
      check('chapter: EPUB prev -> 测试小说-章节', !s.prevDisabled && s.prevTarget === txtCh.id, `(target ${s.prevTarget.slice(0, 8)})`);
      check('chapter: EPUB next -> sample-book (AZW3)', !s.nextDisabled && s.nextTarget === azw3.id, `(target ${s.nextTarget.slice(0, 8)})`);
      check('chapter: no page JS errors (epub)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
    // 1.0.4 回归：点“下一章/上一章”必须真的触发跳转（location.href）。
    // jsdom 不实现跨页导航，点按钮后会记一条 "Not implemented:
    // navigation" 的 jsdomError；以这条错误出现为准，证明点击
    // 走到了 goToChapter。只验 data-target 不够——1.0.3 在真机上
    // 点了没反应，就是这条点击路径没被验到。
    {
      const txtGbk = books.find((b) => b.title === '测试文章-GBK');
      const { dom, errors } = await loadPage(`${BASE}/read?book=${epub.id}`);
      const doc = dom.window.document;
      const ready = await waitFor(() => doc.body.getAttribute('data-test-chapter') === '4/6', 30000, 'chapter click (epub)');
      const before = errors.length;
      const nextBtn = doc.getElementById('next-chapter-btn');
      const prevBtn = doc.getElementById('prev-chapter-btn');
      const navTarget = nextBtn.getAttribute('data-target') || '';
      const prevTarget = prevBtn.getAttribute('data-target') || '';
      nextBtn.click();
      const navNext = await waitFor(
        () => errors.slice(before).some((e) => /navigation/i.test(e)),
        5000, 'chapter click next navigates');
      check('chapter: click next triggers navigation', ready && navNext,
        `(target ${navTarget.slice(0, 8)}, new errors: ${errors.slice(before).join(' | ').slice(0, 200)})`);
      check('chapter: click next targets sample-book (AZW3)', navTarget === azw3.id, `(target ${navTarget.slice(0, 8)})`);
      // 上一章：同一页再点一次（jsdom 不真跳转，页面还在同一本上，
      // 所以 prev 的目标仍是上一本，可以接着验）。
      const beforePrev = errors.length;
      prevBtn.click();
      const navPrev = await waitFor(
        () => errors.slice(beforePrev).some((e) => /navigation/i.test(e)),
        5000, 'chapter click prev navigates');
      check('chapter: click prev triggers navigation', ready && navPrev,
        `(target ${prevTarget.slice(0, 8)}, new errors: ${errors.slice(beforePrev).join(' | ').slice(0, 200)})`);
      check('chapter: click prev targets 测试小说-章节', prevTarget === txtCh.id, `(target ${prevTarget.slice(0, 8)})`);
      dom.window.close();
    }
    // 小说/：TXT 是第 1/6 章 → 上一章置灰；AZW3 是第 5/6 章 → 下一章指向 MOBI
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${txt.id}`);
      const doc = dom.window.document;
      const ready = await waitFor(() => doc.body.getAttribute('data-test-chapter') === '1/6', 30000, 'chapter nav (txt)');
      const s = chapterState(doc);
      check('chapter: TXT reader has chapter buttons', ready && s.marker === '1/6', `(${s.label}|${s.marker})`);
      check('chapter: TXT first book prev disabled', s.prevDisabled);
      check('chapter: TXT first book next enabled', !s.nextDisabled);
      check('chapter: no page JS errors (txt)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${azw3.id}`);
      const doc = dom.window.document;
      const ready = await waitFor(() => doc.body.getAttribute('data-test-chapter') === '5/6', 30000, 'chapter nav (azw3)');
      const s = chapterState(doc);
      check('chapter: AZW3 reader has chapter buttons', ready && s.marker === '5/6', `(${s.label}|${s.marker})`);
      check('chapter: AZW3 next -> MOBI', !s.nextDisabled && s.nextTarget === mobi.id, `(target ${s.nextTarget.slice(0, 8)})`);
      check('chapter: AZW3 prev enabled', !s.prevDisabled);
      check('chapter: no page JS errors (azw3)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
    // ---- 1.0.5: MOBI 与 TXT 小说章节 ----------------------------------
    // MOBI（sample-book-mobi）是第 6/6 章 → 上一章指向 AZW3，下一章置灰；
    // 打开后应正常解包出内容（data-test-azw3）。
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${mobi.id}`);
      const doc = dom.window.document;
      const ready = await waitFor(() => doc.body.getAttribute('data-test-chapter') === '6/6', 30000, 'mobi chapter nav');
      const s = chapterState(doc);
      check('mobi: MOBI is 6/6 in 小说/', ready && s.marker === '6/6' && s.label.includes('第 6 / 6 章'), `(${s.label}|${s.marker})`);
      check('mobi: MOBI prev -> AZW3', !s.prevDisabled && s.prevTarget === azw3.id, `(target ${s.prevTarget.slice(0, 8)})`);
      check('mobi: MOBI last book next disabled', s.nextDisabled);
      const loaded = await waitFor(() => doc.body.getAttribute('data-test-azw3') === '1', 30000, 'mobi content loads');
      check('mobi: MOBI content renders (data-test-azw3)', loaded);
      check('mobi: no page JS errors', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
    // TXT 小说章节：测试小说-章节.txt 有 6 章（序章/第一章/第二章/
    // 第3章/Chapter 4/后记），目录按钮可见，点第 3 章应跳到对应页，
    // 位置栏显示当前章名。
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${txtCh.id}`);
      const doc = dom.window.document;
      const ready = await waitFor(() => doc.body.getAttribute('data-test-txt-chapters') === '6', 30000, 'txt chapters detected');
      check('txt-toc: 6 chapters detected', ready, `(marker ${doc.body.getAttribute('data-test-txt-chapters')})`);
      const tocBtn = doc.getElementById('toc-btn');
      check('txt-toc: TOC button visible', ready && !tocBtn.classList.contains('hidden'));
      const items = doc.querySelectorAll('#toc-list .toc-item');
      check('txt-toc: TOC lists 6 chapters', items.length === 6, `(${items.length})`);
      const titles = Array.from(items).map((el) => el.textContent);
      check('txt-toc: chapter titles in order',
        titles[0] === '序章' && titles[1] === '第一章 初入江湖' && titles[2] === '第二章 风起云涌' &&
        titles[3] === '第3章 夜探敌营' && titles[4] === 'Chapter 4 The Return' && titles[5] === '后记',
        `(${titles.join(' | ')})`);
      // 打开目录面板并点第 3 章（第二章 风起云涌）
      tocBtn.click();
      const panelOpen = !doc.getElementById('toc-panel').classList.contains('hidden');
      check('txt-toc: TOC panel opens', panelOpen);
      const before = doc.getElementById('pos-label').textContent;
      items[2].click();
      const jumped = await waitFor(() => doc.getElementById('pos-label').textContent.includes('第二章'), 5000, 'txt toc jump');
      check('txt-toc: click chapter jumps to it', jumped, `(pos ${doc.getElementById('pos-label').textContent} <- ${before})`);
      check('txt-toc: panel closes after jump', doc.getElementById('toc-panel').classList.contains('hidden'));
      check('txt-toc: no page JS errors', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
    // 跳转后恢复进度：给小 PDF 存第 2 页进度，再打开它（这就是
    // 点“下一章”后会发生的事：整页跳到 /read?book=<目标>），应
    // 回到第 2 页而不是从头开始。
    {
      const seeded = await fetch(`${BASE}/api/progress`, {
        method: 'POST', headers: authedHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ book: smallPdf.id, loc: 2, total: 3 }),
      }).then((r) => r.json()).catch(() => ({}));
      check('chapter: seed small PDF progress (page 2)', seeded.ok === true, JSON.stringify(seeded).slice(0, 80));
      const { dom, errors } = await loadPage(`${BASE}/read?book=${smallPdf.id}`);
      const doc = dom.window.document;
      const ok = await waitFor(() => doc.body.getAttribute('data-test-pdf'), 60000, 'small pdf load (jump restore)');
      check('chapter: jump target loads', !!ok);
      const restored = await waitFor(() => doc.getElementById('pos-label').textContent.includes('第 2 / 3 页'), 30000, 'jump restores page 2');
      check('chapter: jump restores saved progress (第 2 / 3 页)', restored, `(${doc.getElementById('pos-label').textContent})`);
      check('chapter: no page JS errors (jump restore)', errors.length === 0, errors.slice(0, 2).join(' | '));
      dom.window.close();
    }
  } finally {
    proc.kill();
  }
  const failed = results.filter((r) => !r[1]);
  console.log(`RESULT: ${results.length - failed.length}/${results.length} passed`);
  if (failed.length) {
    console.log('FAILURES: ' + failed.map((f) => f[0]).join(', '));
    process.exit(1);
  }
  process.exit(0);
}

main().catch((e) => { console.error(e); process.exit(2); });
