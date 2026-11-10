/* 满月阅读 — 阅读器：PDF / EPUB / TXT / AZW3 */
(function () {
  'use strict';

  try {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(function () {});
    }
  } catch (e) { /* 忽略 */ }

  var params = new URLSearchParams(window.location.search);
  var bookId = params.get('book') || '';

  var body = document.getElementById('body');
  var viewer = document.getElementById('viewer');
  var titleEl = document.getElementById('book-title');
  var badgeEl = document.getElementById('book-badge');
  var errorBox = document.getElementById('error-box');
  var loadingEl = document.getElementById('loading');
  var posLabel = document.getElementById('pos-label');
  var prevBtn = document.getElementById('prev-btn');
  var nextBtn = document.getElementById('next-btn');
  var fontMinus = document.getElementById('font-minus');
  var fontPlus = document.getElementById('font-plus');
  var zoomOut = document.getElementById('zoom-out');
  var zoomIn = document.getElementById('zoom-in');
  var zoomFit = document.getElementById('zoom-fit');

  var book = null;          // {id,title,format,...}
  var savedLoc = null;      // 恢复用的 loc
  var savedTotal = null;
  var saveTimer = null;
  var lastSavedKey = '';

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.classList.remove('hidden');
    loadingEl.classList.add('hidden');
  }
  function setPos(text) { posLabel.textContent = text; }
  function setTitle(t) {
    titleEl.textContent = t;
    document.title = t + ' — 满月阅读';
  }

  /* ---------- 进度保存（2 秒节流 + 页面隐藏时保存） ---------- */
  function saveProgress(loc, total) {
    if (!bookId) return;
    var key = JSON.stringify([loc, total]);
    if (key === lastSavedKey) return;
    lastSavedKey = key;
    fetch('/api/progress', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book: bookId, loc: loc, total: total }),
      keepalive: true
    }).catch(function () { /* 保存失败不打扰阅读 */ });
  }
  var pendingSave = null;
  function scheduleSave(loc, total) {
    pendingSave = { loc: loc, total: total };
    if (saveTimer) return;
    saveTimer = setTimeout(function () {
      saveTimer = null;
      if (pendingSave) saveProgress(pendingSave.loc, pendingSave.total);
    }, 2000);
  }
  function flushSave() {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    if (pendingSave) { saveProgress(pendingSave.loc, pendingSave.total); pendingSave = null; }
  }
  window.addEventListener('pagehide', flushSave);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') flushSave();
  });
  window.addEventListener('beforeunload', flushSave);

  /* ---------- 入口：找书 + 恢复进度 ---------- */
  if (!bookId) {
    showError('没有指定要打开的书');
    setTitle('满月阅读');
  } else {
    fetch('/api/books')
      .then(function (r) { return r.json().catch(function () { return { books: [] }; }); })
      .then(function (data) {
        var list = Array.isArray(data.books) ? data.books : [];
        book = null;
        for (var i = 0; i < list.length; i++) {
          if (list[i].id === bookId) { book = list[i]; break; }
        }
        if (!book) { showError('找不到这本书'); setTitle('找不到这本书'); return null; }
        setTitle(book.title || '未命名');
        badgeEl.textContent = (book.format || '').toUpperCase();
        badgeEl.classList.add((book.format || '').toLowerCase());
        return fetch('/api/progress')
          .then(function (r) { return r.json().catch(function () { return {}; }); })
          .catch(function () { return {}; })
          .then(function (pdata) {
            var p = (pdata.progress || {})[bookId];
            if (p) { savedLoc = p.loc; savedTotal = p.total; }
            openBook();
          });
      })
      .catch(function () { showError('打开失败，请检查网络后重试'); });
  }

  function openBook() {
    var fmt = (book.format || '').toLowerCase();
    loadingEl.classList.add('hidden');
    if (fmt === 'pdf') initPdf();
    else if (fmt === 'epub') initEpub();
    else if (fmt === 'txt') initTxt();
    else if (fmt === 'azw3') initAzw3();
    else showError('暂不支持这种格式');
  }

  /* ================= PDF ================= */
  var pdfDoc = null, pdfTotal = 0, pdfCurrent = 1, pdfZoom = 1;
  var pdfRendered = {};   // pageNum -> true
  var pdfRendering = {};
  var pdfObserver = null;
  var pdfAspectRatio = ''; // "宽 / 高"，取第 1 页，用于占位高度

  function initPdf() {
    body.classList.add('pdf-mode');
    zoomOut.classList.remove('hidden');
    zoomIn.classList.remove('hidden');
    zoomFit.classList.remove('hidden');
    document.getElementById('pdf-viewer').classList.remove('hidden');
    if (typeof pdfjsLib === 'undefined') { showError('PDF 阅读组件加载失败'); return; }
    pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/pdf.worker.min.js';
    setPos('加载中…');
    pdfjsLib.getDocument('/api/file/' + encodeURIComponent(bookId)).promise
      .then(function (pdf) {
        pdfDoc = pdf;
        pdfTotal = pdf.numPages;
        document.body.setAttribute('data-test-pdf', String(pdfTotal));
        pdfCurrent = (typeof savedLoc === 'number' && savedLoc >= 1 && savedLoc <= pdfTotal) ? savedLoc : 1;
        buildPdfPlaceholders();
        // 先按第 1 页的宽高比给所有占位定高：否则占位只有 200px，
        // 跳到恢复页会落错位置，未渲染的页也会挤在顶部被一次性全渲染。
        pdfDoc.getPage(1).then(function (page) {
          var vp = page.getViewport({ scale: 1 });
          pdfAspectRatio = vp.width + ' / ' + vp.height;
          document.querySelectorAll('.pdf-page').forEach(function (el) {
            el.style.aspectRatio = pdfAspectRatio;
          });
          afterPdfSized();
        }).catch(function () { afterPdfSized(); });
      })
      .catch(function () { showError('这本 PDF 打不开'); });
  }

  function afterPdfSized() {
    observePdfPages();
    updatePdfPos();
    // 跳到恢复页
    requestAnimationFrame(function () { scrollToPdfPage(pdfCurrent, false); });
  }

  function buildPdfPlaceholders() {
    var container = document.getElementById('pdf-viewer');
    container.innerHTML = '';
    for (var i = 1; i <= pdfTotal; i++) {
      var div = document.createElement('div');
      div.className = 'pdf-page';
      div.setAttribute('data-page', i);
      div.innerHTML = '<div class="ph">第 ' + i + ' 页</div>';
      container.appendChild(div);
    }
  }

  function pdfScaleFor(page) {
    var container = document.getElementById('pdf-viewer');
    var avail = container.clientWidth - 16; // 左右 padding
    var viewport = page.getViewport({ scale: 1 });
    return (avail / viewport.width) * pdfZoom;
  }

  function renderPdfPage(num) {
    if (!pdfDoc || pdfRendered[num] || pdfRendering[num]) return;
    var el = document.querySelector('.pdf-page[data-page="' + num + '"]');
    if (!el) return;
    pdfRendering[num] = true;
    pdfDoc.getPage(num).then(function (page) {
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      var scale = pdfScaleFor(page) * dpr;
      var viewport = page.getViewport({ scale: scale });
      var canvas = document.createElement('canvas');
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      canvas.style.width = '100%';
      var ctx = canvas.getContext('2d');
      return page.render({ canvasContext: ctx, viewport: viewport }).promise.then(function () {
        el.innerHTML = '';
        el.appendChild(canvas);
        if (num === 1) document.body.setAttribute('data-test-pdf-page1', '1');
        // 渲染后用画布真实高度，不再用第 1 页的宽高比占位
        el.style.aspectRatio = '';
        pdfRendered[num] = true;
        delete pdfRendering[num];
      });
    }).catch(function () { delete pdfRendering[num]; });
  }

  function observePdfPages() {
    if (pdfObserver) pdfObserver.disconnect();
    pdfObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        var num = parseInt(en.target.getAttribute('data-page'), 10);
        if (en.isIntersecting) renderPdfPage(num);
      });
    }, { root: viewer, rootMargin: '600px 0px' });
    document.querySelectorAll('.pdf-page').forEach(function (el) { pdfObserver.observe(el); });
  }

  function updatePdfPos() {
    setPos('第 ' + pdfCurrent + ' / ' + pdfTotal + ' 页');
  }

  function scrollToPdfPage(num, smooth) {
    num = Math.max(1, Math.min(pdfTotal, num));
    var el = document.querySelector('.pdf-page[data-page="' + num + '"]');
    if (!el) return;
    renderPdfPage(num);
    var top = viewer.scrollTop + el.getBoundingClientRect().top - viewer.getBoundingClientRect().top;
    viewer.scrollTo({ top: top, behavior: smooth === false ? 'auto' : 'smooth' });
  }

  // 滚动时判定最上方可见页（页按顺序排，第一个底部越过顶栏的就是）
  var pdfScrollTimer = null;
  viewer.addEventListener('scroll', function () {
    if (!pdfDoc) return;
    if (pdfScrollTimer) return;
    pdfScrollTimer = setTimeout(function () {
      pdfScrollTimer = null;
      var vRect = viewer.getBoundingClientRect();
      var pages = document.querySelectorAll('.pdf-page');
      for (var i = 0; i < pages.length; i++) {
        var r = pages[i].getBoundingClientRect();
        if (r.bottom > vRect.top + 60) {
          var num = parseInt(pages[i].getAttribute('data-page'), 10);
          if (num !== pdfCurrent) {
            pdfCurrent = num;
            updatePdfPos();
            scheduleSave(pdfCurrent, pdfTotal);
          }
          break;
        }
      }
    }, 200);
  });

  function rerenderPdfVisible() {
    pdfRendered = {};
    pdfRendering = {};
    var vRect = viewer.getBoundingClientRect();
    document.querySelectorAll('.pdf-page').forEach(function (el) {
      var num = parseInt(el.getAttribute('data-page'), 10);
      var r = el.getBoundingClientRect();
      if (r.top > vRect.bottom + 600) return; // 已过视口下方，后面的更远
      if (r.bottom >= vRect.top - 600 && r.top <= vRect.bottom + 600) {
        el.innerHTML = '<div class="ph">第 ' + num + ' 页</div>';
        if (pdfAspectRatio) el.style.aspectRatio = pdfAspectRatio;
        renderPdfPage(num);
      } else {
        el.innerHTML = '<div class="ph">第 ' + num + ' 页</div>';
        if (pdfAspectRatio) el.style.aspectRatio = pdfAspectRatio;
      }
    });
  }

  /* ================= EPUB ================= */
  var epubBook = null, epubRendition = null;
  var epubFontPx = 18;

  function initEpub() {
    document.getElementById('epub-viewer').classList.remove('hidden');
    if (typeof ePub === 'undefined') { showError('EPUB 阅读组件加载失败'); return; }
    setPos('加载中…');
    // 注意：不能直接把 /api/file/<id> 传给 ePub——它没有 .epub 后缀，
    // epub.js 会把它当成已解包的目录去取 META-INF/container.xml（404）。
    // 先自己取回 ArrayBuffer 再传，epub.js 即按打包文件处理。
    fetch('/api/file/' + encodeURIComponent(bookId))
      .then(function (r) {
        if (!r.ok) throw new Error('http ' + r.status);
        return r.arrayBuffer();
      })
      .then(function (buf) {
        try {
          epubBook = ePub(buf);
        } catch (e) { showError('这本 EPUB 打不开'); return; }
        epubRendition = epubBook.renderTo('epub-viewer', {
          width: '100%', height: '100%', flow: 'scrolled', manager: 'continuous'
        });
        epubRendition.themes.fontSize(epubFontPx + 'px');
        var displayed = (typeof savedLoc === 'string' && savedLoc) ? epubRendition.display(savedLoc) : epubRendition.display();
        displayed.catch(function () { epubRendition.display(); });
        epubRendition.on('relocated', function (location) {
          document.body.setAttribute('data-test-epub', '1');
          var cfi = location && location.start ? location.start.cfi : null;
          var page = location && location.start ? location.start.displayed.page : null;
          var total = location && location.start ? location.start.displayed.total : null;
          if (page && total) setPos('第 ' + page + ' / ' + total + ' 页');
          else if (page) setPos('第 ' + page + ' 页');
          if (cfi) scheduleSave(cfi, total || 0);
        });
        epubBook.ready.catch(function () { showError('这本 EPUB 打不开'); });
      })
      .catch(function () { showError('这本 EPUB 打不开'); });
  }

  /* ================= TXT ================= */
  var txtPages = [];   // {start, text}
  var txtTotalChars = 0;
  var txtCurrent = 0;  // 0-based 页下标
  var txtFontPx = 18;
  var CHUNK = 200 * 1024;
  var PAGE_CHARS = 1800;

  function initTxt() {
    document.getElementById('txt-viewer').classList.remove('hidden');
    setPos('加载中…');
    fetchTxtChunks()
      .then(function (bytes) {
        var text = decodeBytes(bytes);
        txtTotalChars = text.length;
        txtPages = paginateTxt(text);
        if (!txtPages.length) { showError('这本 TXT 是空的'); return; }
        var startPage = 0;
        if (typeof savedLoc === 'number' && savedLoc > 0) {
          for (var i = 0; i < txtPages.length; i++) {
            if (txtPages[i].start <= savedLoc) startPage = i; else break;
          }
        }
        showTxtPage(startPage, false);
        document.body.setAttribute('data-test-txt', String(txtPages.length));
        var slider = document.getElementById('txt-slider');
        slider.max = txtPages.length;
        document.getElementById('txt-slider-wrap').classList.remove('hidden');
      })
      .catch(function () { showError('这本 TXT 打不开'); });
  }

  function fetchTxtChunks() {
    var parts = [];
    var total = null;
    function fetchRange(start) {
      return fetch('/api/file/' + encodeURIComponent(bookId), {
        headers: { Range: 'bytes=' + start + '-' + (start + CHUNK - 1) }
      }).then(function (r) {
        if (!r.ok && r.status !== 206 && r.status !== 200) throw new Error('http ' + r.status);
        var cr = r.headers.get('Content-Range');
        if (cr) {
          var m = /\/(\d+)\s*$/.exec(cr);
          if (m) total = parseInt(m[1], 10);
        }
        return r.arrayBuffer();
      }).then(function (buf) {
        parts.push(new Uint8Array(buf));
        var got = parts.reduce(function (n, p) { return n + p.length; }, 0);
        if (total == null) {
          // 服务器不支持 Range：一次已取完全部
          return concatBytes(parts);
        }
        if (got >= total) return concatBytes(parts);
        return fetchRange(got);
      });
    }
    return fetchRange(0);
  }

  function concatBytes(parts) {
    var len = parts.reduce(function (n, p) { return n + p.length; }, 0);
    var out = new Uint8Array(len);
    var off = 0;
    parts.forEach(function (p) { out.set(p, off); off += p.length; });
    return out;
  }

  function decodeBytes(bytes) {
    try {
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    } catch (e) {
      try {
        return new TextDecoder('gbk').decode(bytes);
      } catch (e2) {
        return new TextDecoder('utf-8').decode(bytes);
      }
    }
  }

  function paginateTxt(text) {
    var pages = [];
    var i = 0;
    while (i < text.length) {
      var end = Math.min(text.length, i + PAGE_CHARS);
      if (end < text.length) {
        // 尽量在行边界断开
        var cut = text.lastIndexOf('\n', end);
        if (cut > i + 200) end = cut + 1;
      }
      pages.push({ start: i, text: text.slice(i, end) });
      i = end;
    }
    return pages;
  }

  function showTxtPage(idx, save) {
    idx = Math.max(0, Math.min(txtPages.length - 1, idx));
    txtCurrent = idx;
    var content = document.getElementById('txt-content');
    content.style.fontSize = txtFontPx + 'px';
    content.textContent = txtPages[idx].text;
    var slider = document.getElementById('txt-slider');
    slider.value = idx + 1;
    setPos('第 ' + (idx + 1) + ' / ' + txtPages.length + ' 页');
    viewer.scrollTop = 0;
    if (save !== false) scheduleSave(txtPages[idx].start, txtTotalChars);
  }

  /* ================= AZW3 ================= */
  var azw3Frame = null;
  var azw3ScrollTimer = null;
  var azw3Restored = false;

  function initAzw3() {
    azw3Frame = document.getElementById('azw3-frame');
    azw3Frame.classList.remove('hidden');
    azw3Frame.style.height = '100%';
    setPos('加载中…');
    var loaded = false;
    azw3Frame.addEventListener('load', function () {
      loaded = true;
      var doc = null;
      try { doc = azw3Frame.contentDocument || (azw3Frame.contentWindow && azw3Frame.contentWindow.document); } catch (e) { doc = null; }
      if (!doc) { showAzw3Error(); return; }
      var bodyText = '';
      try { bodyText = (doc.body && doc.body.innerText || '').trim(); } catch (e) { bodyText = ''; }
      // 后端出错时 iframe 里可能是 JSON 错误体
      if (bodyText && (bodyText.indexOf('"ok":false') !== -1 || bodyText.indexOf('"ok": false') !== -1)) {
        showAzw3Error();
        return;
      }
      var de = doc.documentElement;
      var scrollEl = doc.scrollingElement || de;
      document.body.setAttribute('data-test-azw3', '1');
      // 恢复滚动位置
      if (!azw3Restored && typeof savedLoc === 'number' && savedLoc > 0 && savedLoc < 1) {
        azw3Restored = true;
        var max = scrollEl.scrollHeight - scrollEl.clientHeight;
        if (max > 0) scrollEl.scrollTop = Math.round(savedLoc * max);
      }
      updateAzw3Pos();
      // 监听 iframe 内滚动
      var win = azw3Frame.contentWindow;
      if (win) {
        win.addEventListener('scroll', function () {
          if (azw3ScrollTimer) return;
          azw3ScrollTimer = setTimeout(function () {
            azw3ScrollTimer = null;
            updateAzw3Pos();
          }, 300);
        }, { passive: true });
      }
    });
    // 超时仍未 load 视为失败
    setTimeout(function () { if (!loaded) showAzw3Error(); }, 15000);
    azw3Frame.src = '/api/azw3/' + encodeURIComponent(bookId) + '/';
  }

  function updateAzw3Pos() {
    var doc = null;
    try { doc = azw3Frame.contentDocument; } catch (e) { return; }
    if (!doc) return;
    var el = doc.scrollingElement || doc.documentElement;
    var max = el.scrollHeight - el.clientHeight;
    var frac = max > 0 ? (el.scrollTop / max) : 0;
    frac = Math.max(0, Math.min(1, frac));
    setPos('已读 ' + Math.round(frac * 100) + '%');
    scheduleSave(frac, 1);
  }

  function showAzw3Error() {
    azw3Frame.classList.add('hidden');
    showError('这本 AZW3 打不开（可能带 DRM 或解析失败）');
    setPos('—');
  }

  /* ================= 工具栏 ================= */
  prevBtn.addEventListener('click', function () {
    if (!book) return;
    var fmt = (book.format || '').toLowerCase();
    if (fmt === 'pdf') { pdfCurrent = Math.max(1, pdfCurrent - 1); scrollToPdfPage(pdfCurrent, true); updatePdfPos(); scheduleSave(pdfCurrent, pdfTotal); }
    else if (fmt === 'epub') { if (epubRendition) epubRendition.prev(); }
    else if (fmt === 'txt') { showTxtPage(txtCurrent - 1); }
    else if (fmt === 'azw3') { azw3ScrollBy(-0.9); }
  });

  nextBtn.addEventListener('click', function () {
    if (!book) return;
    var fmt = (book.format || '').toLowerCase();
    if (fmt === 'pdf') { pdfCurrent = Math.min(pdfTotal, pdfCurrent + 1); scrollToPdfPage(pdfCurrent, true); updatePdfPos(); scheduleSave(pdfCurrent, pdfTotal); }
    else if (fmt === 'epub') { if (epubRendition) epubRendition.next(); }
    else if (fmt === 'txt') { showTxtPage(txtCurrent + 1); }
    else if (fmt === 'azw3') { azw3ScrollBy(0.9); }
  });

  function azw3ScrollBy(fracScreens) {
    if (!azw3Frame) return;
    try {
      var doc = azw3Frame.contentDocument;
      if (!doc) return;
      var el = doc.scrollingElement || doc.documentElement;
      el.scrollTop += fracScreens * el.clientHeight;
      updateAzw3Pos();
    } catch (e) { /* 忽略 */ }
  }

  fontMinus.addEventListener('click', function () { changeFont(-2); });
  fontPlus.addEventListener('click', function () { changeFont(2); });

  function changeFont(delta) {
    if (!book) return;
    var fmt = (book.format || '').toLowerCase();
    if (fmt === 'epub') {
      epubFontPx = Math.max(12, Math.min(32, epubFontPx + delta));
      if (epubRendition) epubRendition.themes.fontSize(epubFontPx + 'px');
    } else if (fmt === 'txt' || fmt === 'azw3') {
      if (fmt === 'txt') {
        txtFontPx = Math.max(12, Math.min(32, txtFontPx + delta));
        document.getElementById('txt-content').style.fontSize = txtFontPx + 'px';
      } else {
        // AZW3：改 iframe 内根字号
        try {
          var doc = azw3Frame.contentDocument;
          if (doc && doc.documentElement) {
            var cur = parseInt(doc.documentElement.style.fontSize || '18', 10) || 18;
            doc.documentElement.style.fontSize = Math.max(12, Math.min(32, cur + delta)) + 'px';
          }
        } catch (e) { /* 跨域时忽略 */ }
      }
    }
  }

  zoomOut.addEventListener('click', function () {
    pdfZoom = Math.max(0.7, +(pdfZoom - 0.2).toFixed(2));
    rerenderPdfVisible();
  });
  zoomIn.addEventListener('click', function () {
    pdfZoom = Math.min(2.0, +(pdfZoom + 0.2).toFixed(2));
    rerenderPdfVisible();
  });
  zoomFit.addEventListener('click', function () {
    pdfZoom = 1;
    rerenderPdfVisible();
    scrollToPdfPage(pdfCurrent, false);
  });

  document.getElementById('txt-slider').addEventListener('input', function (e) {
    showTxtPage(parseInt(e.target.value, 10) - 1);
  });
})();
