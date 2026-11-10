/* 满月阅读 — 书架逻辑 */
(function () {
  'use strict';

  // 注册 Service Worker（失败也不影响页面）
  try {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(function () {});
    }
  } catch (e) { /* 忽略 */ }

  var grid = document.getElementById('grid');
  var emptyEl = document.getElementById('empty');
  var errorBox = document.getElementById('error-box');
  var countEl = document.getElementById('book-count');
  var searchInput = document.getElementById('search-input');
  var setupCard = document.getElementById('setup-card');
  var folderInput = document.getElementById('folder-input');
  var setupStatus = document.getElementById('setup-status');
  var continueStrip = document.getElementById('continue-strip');
  var toastEl = document.getElementById('toast');

  var books = [];
  var booksById = {};
  var toastTimer = null;
  var coverDone = {};      // 本次加载已尝试过的书
  var coverQueue = [];     // 待生成封面的书 id
  var coverWorking = false;
  var coverObserver = null;

  function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.add('hidden'); }, 2500);
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.classList.remove('hidden');
  }
  function hideError() { errorBox.classList.add('hidden'); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtBadge(fmt) { return (fmt || '').toUpperCase(); }

  /* ---------- 加载书库 ---------- */
  function loadBooks() {
    return fetch('/api/books')
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: '服务器返回异常' }; }); })
      .then(function (data) {
        if (data.error) { showError(data.error); }
        books = Array.isArray(data.books) ? data.books : [];
        booksById = {};
        books.forEach(function (b) { booksById[b.id] = b; });
        renderGrid();
        return books;
      })
      .catch(function () {
        showError('加载书库失败，请检查网络或稍后再试');
        return [];
      });
  }

  function loadSettings() {
    return fetch('/api/settings')
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (data) {
        var folder = data.book_folder || '';
        folderInput.value = folder;
        if (!folder || data.error || data.readable === false) {
          setupCard.classList.remove('hidden');
          if (data.error) { setupStatus.textContent = data.error; }
          else if (folder && data.readable === false) { setupStatus.textContent = '该路径不可读，请检查路径和权限'; }
        } else {
          setupCard.classList.add('hidden');
        }
      })
      .catch(function () { setupCard.classList.remove('hidden'); });
  }

  /* ---------- 继续阅读 ---------- */
  function loadContinue() {
    fetch('/api/progress')
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (data) {
        var progress = data.progress || {};
        var ids = Object.keys(progress);
        if (!ids.length) return;
        // 取最近更新的一条
        var best = null, bestTime = '';
        ids.forEach(function (id) {
          var p = progress[id];
          var t = p.updated || '';
          if (!best || t >= bestTime) { best = { id: id, p: p }; bestTime = t; }
        });
        if (!best) return;
        var book = booksById[best.id];
        var title = book ? book.title : '未知书籍';
        var pos = formatPosition(best.p, book ? book.format : '');
        document.getElementById('continue-title').textContent = title;
        document.getElementById('continue-pos').textContent = pos;
        document.getElementById('continue-link').href = '/read?book=' + encodeURIComponent(best.id);
        continueStrip.classList.remove('hidden');
      })
      .catch(function () { /* 没有进度也不影响书架 */ });
  }

  // 进度里只存 loc/total/updated，格式从书库里取
  function formatPosition(p, fmt) {
    fmt = (fmt || '').toLowerCase();
    var loc = p.loc, total = p.total;
    if (fmt === 'pdf' && typeof loc === 'number') {
      return total ? ('第 ' + loc + ' / ' + total + ' 页') : ('第 ' + loc + ' 页');
    }
    if (fmt === 'txt' && typeof loc === 'number') {
      return total ? ('已读 ' + Math.round(loc / total * 100) + '%') : '上次读到一半';
    }
    if (fmt === 'azw3' && typeof loc === 'number') {
      return '已读 ' + Math.round(loc * 100) + '%';
    }
    if (fmt === 'epub') {
      return '上次读到的位置';
    }
    return '继续阅读';
  }

  /* ---------- 渲染书架 ---------- */
  function renderGrid() {
    var q = (searchInput.value || '').trim().toLowerCase();
    var list = books.filter(function (b) {
      return !q || (b.title || '').toLowerCase().indexOf(q) !== -1;
    });
    countEl.textContent = list.length ? ('共 ' + list.length + ' 本') : '';
    emptyEl.classList.toggle('hidden', list.length > 0 || books.length > 0);
    if (!books.length) { grid.innerHTML = ''; emptyEl.classList.remove('hidden'); return; }
    if (!list.length) { grid.innerHTML = ''; emptyEl.classList.remove('hidden'); emptyEl.innerHTML = '没有找到匹配的书'; return; }
    emptyEl.classList.add('hidden');

    grid.innerHTML = list.map(function (b) {
      var coverHtml;
      if (b.cover_url) {
        coverHtml = '<img src="' + esc(b.cover_url) + '" alt="" loading="lazy">';
      } else {
        var first = (b.title || '?').trim().charAt(0) || '?';
        coverHtml = '<div class="tile"><div class="first-char">' + esc(first) + '</div>' +
          '<div class="tile-title">' + esc(b.title || '') + '</div></div>';
      }
      var folderHtml = b.folder ? '<div class="folder">' + esc(b.folder) + '</div>' : '';
      return '<div class="book" data-id="' + esc(b.id) + '" data-need-cover="' +
        ((!b.cover_url && b.format === 'pdf') ? '1' : '0') + '">' +
        '<div class="cover">' + coverHtml + '</div>' +
        '<div class="meta"><div class="t">' + esc(b.title || '未命名') + '</div>' +
        '<div class="sub"><span class="badge ' + esc((b.format || '').toLowerCase()) + '">' + esc(fmtBadge(b.format)) + '</span>' +
        '<span>' + esc(b.size_text || '') + '</span></div>' + folderHtml + '</div></div>';
    }).join('');

    // 点书进阅读器（事件委托）
    grid.querySelectorAll('.book').forEach(function (el) {
      el.addEventListener('click', function () {
        window.location.href = '/read?book=' + encodeURIComponent(el.getAttribute('data-id'));
      });
    });

    observeCovers();
  }

  /* ---------- 懒生成 PDF 封面 ---------- */
  function observeCovers() {
    if (!('IntersectionObserver' in window) || typeof pdfjsLib === 'undefined') return;
    if (coverObserver) coverObserver.disconnect();
    coverObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var el = en.target;
        coverObserver.unobserve(el);
        var id = el.getAttribute('data-id');
        if (id && !coverDone[id]) { coverQueue.push(id); pumpCoverQueue(); }
      });
    }, { rootMargin: '200px' });
    grid.querySelectorAll('.book[data-need-cover="1"]').forEach(function (el) {
      coverObserver.observe(el);
    });
  }

  function pumpCoverQueue() {
    if (coverWorking) return;
    var id = coverQueue.shift();
    if (!id) return;
    coverWorking = true;
    generateCover(id).finally(function () {
      coverWorking = false;
      pumpCoverQueue();
    });
  }

  function generateCover(id) {
    coverDone[id] = true;
    if (typeof pdfjsLib === 'undefined') return Promise.resolve();
    return pdfjsLib.getDocument('/api/file/' + encodeURIComponent(id)).promise
      .then(function (pdf) { return pdf.getPage(1); })
      .then(function (page) {
        var viewport = page.getViewport({ scale: 1 });
        var scale = 240 / viewport.width;
        var v2 = page.getViewport({ scale: scale });
        var canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.floor(v2.width));
        canvas.height = Math.max(1, Math.floor(v2.height));
        var ctx = canvas.getContext('2d');
        return page.render({ canvasContext: ctx, viewport: v2 }).promise.then(function () {
          return canvas.toDataURL('image/jpeg', 0.7);
        });
      })
      .then(function (dataUrl) {
        return fetch('/api/cover/' + encodeURIComponent(id), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image: dataUrl })
        });
      })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (res) {
        if (!res.ok && res.error) return; // 封面失败不打扰用户
        var tile = grid.querySelector('.book[data-id="' + CSS.escape(id) + '"] .cover');
        if (tile) {
          tile.innerHTML = '';
          var img = document.createElement('img');
          img.src = '/api/cover/' + encodeURIComponent(id);
          img.alt = '';
          tile.appendChild(img);
        }
        if (booksById[id]) booksById[id].cover_url = '/api/cover/' + encodeURIComponent(id);
      })
      .catch(function () { /* 封面生成失败：保留文字封面 */ });
  }

  /* ---------- 设置 / 重新扫描 ---------- */
  document.getElementById('save-folder-btn').addEventListener('click', function () {
    var folder = folderInput.value.trim();
    if (!folder) { setupStatus.textContent = '请先填写书库文件夹路径'; return; }
    setupStatus.textContent = '保存中…';
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_folder: folder })
    })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: '服务器返回异常' }; }); })
      .then(function (data) {
        if (data.ok) {
          setupStatus.textContent = data.readable ? ('已保存，共找到 ' + (data.count || 0) + ' 本书') : '已保存，但该路径不可读，请检查权限';
          showToast('已保存');
          loadBooks();
        } else {
          setupStatus.textContent = data.error || '保存失败';
        }
      })
      .catch(function () { setupStatus.textContent = '保存失败，请检查网络'; });
  });

  document.getElementById('settings-link').addEventListener('click', function (e) {
    e.preventDefault();
    setupCard.classList.remove('hidden');
    setupCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
    folderInput.focus();
  });

  document.getElementById('rescan-btn').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = '扫描中…';
    fetch('/api/rescan', { method: 'POST' })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: '服务器返回异常' }; }); })
      .then(function (data) {
        if (data.ok) {
          showToast('扫描完成，共 ' + (data.count || 0) + ' 本');
          hideError();
          loadBooks();
        } else {
          showError(data.error || '重新扫描失败');
        }
      })
      .catch(function () { showError('重新扫描失败，请检查网络'); })
      .finally(function () { btn.disabled = false; btn.textContent = '重新扫描'; });
  });

  searchInput.addEventListener('input', renderGrid);

  /* ---------- 启动 ---------- */
  loadSettings();
  loadBooks().then(function () { loadContinue(); });
})();
