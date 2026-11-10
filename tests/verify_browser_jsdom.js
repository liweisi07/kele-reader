#!/usr/bin/env node
/* Browser-level verification for manyue, using jsdom + node-canvas.
 * Loads the REAL pages (shelf + 4 readers) from the running server and
 * lets the REAL frontend JS run (PDF.js, epub.js, shelf/reader logic).
 * jsdom has no layout, so this installs small polyfills before the page
 * scripts run: IntersectionObserver (reports pages 1-3 as visible),
 * clientWidth/clientHeight and getBoundingClientRect (fixed phone size).
 * Checks the data-test-* markers the reader sets plus visible text.
 * Usage: node tests/verify_browser_jsdom.js  (server must be running;
 * the script starts its own server on port 18098).
 */
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const FIXTURES = path.join(ROOT, 'tests', 'fixtures', 'books');
const PORT = 18098;
const BASE = `http://127.0.0.1:${PORT}`;
const DATA_DIR = '/tmp/manyue-jsdom-data';

const results = [];
function check(label, ok, detail = '') {
  results.push([label, !!ok, detail]);
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label} ${String(detail).replace(/\s+/g, ' ').slice(0, 300)}`);
}

// A harness gap (e.g. a missing jsdom global) must fail loudly with a
// short message, not kill the process with a multi-MB stack dump.
process.on('unhandledRejection', (e) => {
  console.log('UNHANDLED REJECTION: ' + String(e && e.message || e).slice(0, 300));
  console.log('REJ STACK: ' + String(e && e.stack || '(no stack)').split('\n').slice(0, 10).join(' | ').slice(0, 1200));
  process.exit(4);
});
process.on('uncaughtException', (e) => {
  console.log('UNCAUGHT: ' + String(e && e.message || e).slice(0, 300));
  process.exit(5);
});

function getJson(pathname) {
  return new Promise((resolve, reject) => {
    http.get(BASE + pathname, (res) => {
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
  const env = { ...process.env, MANYUE_DATA_DIR: DATA_DIR, MANYUE_PORT: String(PORT) };
  // Server stderr goes to a file: a crash must leave evidence (spawn()
  // defaults to piped stdio that nobody reads, so tracebacks vanish).
  const serverLog = fs.openSync('/tmp/manyue-jsdom-server.log', 'w');
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
        let nodeInit = init;
        if (init && init.signal) {
          const ac = new AbortController();
          const sig = init.signal;
          if (sig.aborted) ac.abort();
          else sig.addEventListener('abort', () => ac.abort(), { once: true });
          nodeInit = { ...init, signal: ac.signal };
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
        if (!fw || fw.__manyuePatched) return;
        fw.__manyuePatched = true;
        const rect = () => ({ top: 0, bottom: 100, left: 0, right: 390, width: 390, height: 100, x: 0, y: 0 });
        if (fw.Range && fw.Range.prototype) {
          fw.Range.prototype.getBoundingClientRect = rect;
          fw.Range.prototype.getClientRects = () => [];
        }
        fw.scrollTo = function () {};
        fw.setImmediate = (fn, ...args) => fw.setTimeout(() => fn(...args), 0);
      };
      const patchIframes = () => {
        window.document.querySelectorAll('iframe').forEach((fr) => {
          try { patchFrameWindow(fr.contentWindow); } catch (e) { /* not ready yet */ }
        });
      };
      // The document has no root element yet inside beforeParse, so arm
      // the observer on a timer; the interval below is the backstop.
      window.setTimeout(() => {
        try {
          new window.MutationObserver(patchIframes).observe(window.document.documentElement, { childList: true, subtree: true });
        } catch (e) { /* polling below still covers it */ }
        patchIframes();
      }, 50);
      setInterval(patchIframes, 200);
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
    const { books } = await getJson('/api/books');
    const bigPdf = books.find((b) => b.title === '测试漫画-大文件');
    const epub = books.find((b) => b.format === 'epub');
    const txt = books.find((b) => b.title === '测试文章');
    const azw3 = books.find((b) => b.format === 'azw3');

    // ---- shelf -------------------------------------------------------
    {
      const { dom, errors } = await loadPage(BASE + '/');
      const ok = await waitFor(() => dom.window.document.querySelectorAll('.book').length >= 6, 20000, 'shelf grid');
      const doc = dom.window.document;
      check('shelf: 6 books rendered', ok, `(found ${doc.querySelectorAll('.book').length})`);
      check('shelf: big PDF title present', doc.body.textContent.includes('测试漫画-大文件'));
      check('shelf: EPUB title present', doc.body.textContent.includes('测试小说：满月之下'));
      check('shelf: no page JS errors', errors.length === 0, errors.slice(0, 2).join(' | '));
      // cover generation: wait for a PDF cover to be uploaded
      const coverOk = await waitFor(() => {
        const imgs = [...doc.querySelectorAll('.book .cover img')];
        return imgs.length > 0;
      }, 30000, 'shelf cover');
      check('shelf: PDF cover generated + shown', coverOk);
      check('shelf: no page JS errors (after covers)', errors.length === 0, errors.slice(0, 2).join(' | '));
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
      check('PDF: position label "第 1 / 36 页"', doc.getElementById('pos-label').textContent.includes('第 1 / 36 页'),
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

    // ---- EPUB reader ---------------------------------------------------
    {
      const { dom, errors } = await loadPage(`${BASE}/read?book=${epub.id}`);
      const doc = dom.window.document;
      const ok = await waitFor(() => doc.body.getAttribute('data-test-epub') === '1', 60000, 'epub relocate');
      check('EPUB: rendition relocated', ok);
      const label = doc.getElementById('pos-label').textContent;
      check('EPUB: position label set', /第 \d+/.test(label), `(${label})`);
      // The continuous manager keeps every rendered section as its own
      // iframe, so read ALL of them (the first is the cover page).
      const allIframeText = () => {
        let out = '';
        doc.querySelectorAll('#epub-viewer iframe').forEach((frame) => {
          try { if (frame.contentDocument) out += frame.contentDocument.body.textContent || ''; } catch (e) { /* ignore */ }
        });
        return out;
      };
      const coverOk = await waitFor(() => allIframeText().includes('测试小说：满月之下'), 30000, 'epub cover text');
      check('EPUB: cover page rendered in iframe', coverOk, `(len ${allIframeText().length})`);
      // Dummy blob: URLs from the createObjectURL shim cannot be fetched
      // by jsdom's resource loader (real browsers resolve them); ignore
      // those load failures, they are not JS errors.
      const realErrors = errors.filter((e) => !/Could not load (link|img|script): "blob:jsdom\//.test(e));
      check('EPUB: no page JS errors', realErrors.length === 0, realErrors.slice(0, 2).join(' | '));
      dom.window.close();
    }

    // ---- EPUB: chapter 1 via progress restore --------------------------
    // jsdom has no layout, so scroll-driven "next page" cannot advance
    // sections; instead verify chapter rendering through the app's own
    // resume feature: save a CFI for chapter 1, reopen the book, and
    // check chapter 1's text renders. The CFI mirrors the one epub.js
    // itself generates for a chapter start (spine /6/4 = ch1).
    {
      const cfi = 'epubcfi(/6/4!/4/4/1:0)';
      const saved = await fetch(`${BASE}/api/progress`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book: epub.id, loc: cfi, total: 1 }),
      }).then((r) => r.json()).catch(() => ({}));
      check('EPUB: progress saved for chapter 1', saved.ok === true, JSON.stringify(saved).slice(0, 80));
      const { dom, errors } = await loadPage(`${BASE}/read?book=${epub.id}`);
      const doc = dom.window.document;
      const ok = await waitFor(() => doc.body.getAttribute('data-test-epub') === '1', 60000, 'epub relocate ch1');
      check('EPUB: rendition relocated (resume)', ok);
      const allIframeText = () => {
        let out = '';
        doc.querySelectorAll('#epub-viewer iframe').forEach((frame) => {
          try { if (frame.contentDocument) out += frame.contentDocument.body.textContent || ''; } catch (e) { /* ignore */ }
        });
        return out;
      };
      const ch1 = await waitFor(() => allIframeText().includes('月亮升起来的时候'), 30000, 'epub ch1 text');
      check('EPUB: chapter 1 text rendered after resume', ch1, `(len ${allIframeText().length})`);
      const realErrors = errors.filter((e) => !/Could not load (link|img|script): "blob:jsdom\//.test(e));
      check('EPUB: no page JS errors (resume)', realErrors.length === 0, realErrors.slice(0, 2).join(' | '));
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
