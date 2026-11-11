/* 可乐阅读 — 书架逻辑：文件夹分组、每本进度、阅读历史 */
(function () {
  'use strict';

  // 注册 Service Worker（失败也不影响页面）
  try {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(function () {});
    }
  } catch (e) { /* 忽略 */ }

  var shelfEl = document.getElementById('shelf');
  var emptyEl = document.getElementById('empty');
  var errorBox = document.getElementById('error-box');
  var countEl = document.getElementById('book-count');
  var searchInput = document.getElementById('search-input');
  var sortSelect = document.getElementById('sort-select');
  var sortMode = 'title';   // title | size | date | progress
  var setupCard = document.getElementById('setup-card');
  var folderInput = document.getElementById('folder-input');
  var setupStatus = document.getElementById('setup-status');
  var accountCard = document.getElementById('account-card');
  var curPassEl = document.getElementById('cur-pass');
  var newUserEl = document.getElementById('new-user');
  var newPassEl = document.getElementById('new-pass');
  var accountStatus = document.getElementById('account-status');
  var continueStrip = document.getElementById('continue-strip');
  var historySection = document.getElementById('history-section');
  var historyList = document.getElementById('history-list');
  var toastEl = document.getElementById('toast');

  var books = [];
  var booksById = {};
  var progressById = {};   // bookId -> {loc, total, updated}
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

  // 会话过期（401）直接回登录页，登录后按 next 回来。
  function redirectToLogin() {
    window.location.href = '/login?next=' +
      encodeURIComponent(window.location.pathname + window.location.search);
  }

  // 带鉴权检查的 fetch：401 说明会话过期，跳登录页并中断本次调用。
  function apiFetch(url, opts) {
    return fetch(url, opts).then(function (r) {
      if (r.status === 401) {
        redirectToLogin();
        var err = new Error('unauthorized');
        err.unauthorized = true;
        throw err;
      }
      return r;
    });
  }

  function fmtBadge(fmt) { return (fmt || '').toUpperCase(); }

  /* ---------- 加载书库 / 进度 ---------- */
  function loadBooks() {
    return apiFetch('/api/books')
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: '服务器返回异常' }; }); })
      .then(function (data) {
        if (data.error) { showError(data.error); }
        books = Array.isArray(data.books) ? data.books : [];
        booksById = {};
        books.forEach(function (b) { booksById[b.id] = b; });
        renderGrid();
        renderHistory();
        return books;
      })
      .catch(function (e) {
        if (e && e.unauthorized) return [];
        showError('加载书库失败，请检查网络或稍后再试');
        return [];
      });
  }

  function loadProgress() {
    return apiFetch('/api/progress')
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (data) {
        progressById = data.progress || {};
        renderGrid();
        renderHistory();
        return progressById;
      })
      .catch(function (e) {
        if (e && e.unauthorized) return {};
        return {};
      });
  }

  function loadSettings() {
    return apiFetch('/api/settings')
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
      .catch(function (e) {
        if (e && e.unauthorized) return;
        setupCard.classList.remove('hidden');
      });
  }

  /* ---------- 进度口径（与阅读器一致） ---------- */
  // 返回 0..100 的百分比；算不出来的返回 null（不显示进度条）。
  // PDF/TXT：loc/total；EPUB/AZW3：loc 是 0..1 的滚动小数。
  // 旧版 EPUB 存的是 CFI 字符串，算不出百分比，返回 null。
  function progressPct(book, p) {
    if (!p) return null;
    var fmt = (book.format || '').toLowerCase();
    var loc = p.loc, total = p.total;
    if (fmt === 'pdf' || fmt === 'txt') {
      if (typeof loc !== 'number' || typeof total !== 'number' || total <= 0) return null;
      return Math.max(0, Math.min(100, Math.round(loc / total * 100)));
    }
    if (fmt === 'epub' || fmt === 'azw3' || fmt === 'mobi') {
      if (typeof loc !== 'number') return null;
      return Math.max(0, Math.min(100, Math.round(loc * 100)));
    }
    return null;
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
    if ((fmt === 'azw3' || fmt === 'mobi' || fmt === 'epub') && typeof loc === 'number') {
      return '已读 ' + Math.round(loc * 100) + '%';
    }
    if (fmt === 'epub') {
      return '上次读到的位置';
    }
    return '继续阅读';
  }

  // 供历史列表用的进度文字：能算百分比就给百分比，否则给位置描述
  function progressText(book, p) {
    var pct = progressPct(book, p);
    if (pct !== null) return '已读 ' + pct + '%';
    return formatPosition(p, book.format);
  }

  // updated 是 "YYYY-MM-DD HH:MM:SS"，友好显示：今天/昨天/月日
  function fmtTime(updated) {
    if (!updated) return '';
    var m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(updated);
    if (!m) return updated;
    var today = new Date();
    var y = today.getFullYear(), mo = today.getMonth() + 1, d = today.getDate();
    var uy = parseInt(m[1], 10), um = parseInt(m[2], 10), ud = parseInt(m[3], 10);
    var hm = m[4] + ':' + m[5];
    if (uy === y && um === mo && ud === d) return '今天 ' + hm;
    var yday = new Date(y, mo - 1, d - 1);
    if (uy === yday.getFullYear() && um === yday.getMonth() + 1 && ud === yday.getDate()) return '昨天 ' + hm;
    return m[2] + '-' + m[3] + ' ' + hm;
  }

  /* ---------- 继续阅读 ---------- */
  function loadContinue() {
    var ids = Object.keys(progressById);
    if (!ids.length) return;
    // 取最近更新的一条
    var best = null, bestTime = '';
    ids.forEach(function (id) {
      var p = progressById[id];
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
  }

  /* ---------- 阅读历史 ---------- */
  function renderHistory() {
    var entries = [];
    Object.keys(progressById).forEach(function (id) {
      var p = progressById[id];
      var book = booksById[id];
      if (!book || !p || !p.updated) return;
      entries.push({ book: book, p: p });
    });
    entries.sort(function (a, b) {
      return a.p.updated < b.p.updated ? 1 : (a.p.updated > b.p.updated ? -1 : 0);
    });
    var shown = entries.slice(0, 20);
    if (!shown.length) {
      historySection.classList.add('hidden');
      historyList.innerHTML = '';
      return;
    }
    historySection.classList.remove('hidden');
    document.getElementById('history-count').textContent =
      shown.length + ' 条';
    historyList.innerHTML = shown.map(function (e) {
      var b = e.book, p = e.p;
      return '<a class="history-item" href="/read?book=' + encodeURIComponent(b.id) + '">' +
        '<span class="history-title">' + esc(b.title || '未命名') + '</span>' +
        '<span class="history-pos">' + esc(progressText(b, p)) + '</span>' +
        '<span class="history-time">' + esc(fmtTime(p.updated)) + '</span></a>';
    }).join('');
  }

  /* ---------- 排序（文件夹内排序，对所有格式生效） ---------- */
  // title：书名升序；size：文件从大到小；date：修改时间从新到旧；
  // progress：阅读进度从高到低（无进度排最后）。
  function sortBooks(list) {
    var arr = list.slice();
    if (sortMode === 'size') {
      arr.sort(function (a, b) { return (b.size || 0) - (a.size || 0); });
    } else if (sortMode === 'date') {
      arr.sort(function (a, b) {
        return (b.mtime || '') < (a.mtime || '') ? -1 : ((b.mtime || '') > (a.mtime || '') ? 1 : 0);
      });
    } else if (sortMode === 'progress') {
      arr.sort(function (a, b) {
        var pa = progressPct(a, progressById[a.id]);
        var pb = progressPct(b, progressById[b.id]);
        if (pa === null) pa = -1;
        if (pb === null) pb = -1;
        return pb - pa;
      });
    } else {
      arr.sort(function (a, b) {
        return (a.title || '').localeCompare(b.title || '', 'zh');
      });
    }
    return arr;
  }

  /* ---------- 渲染书架（按文件夹分组） ---------- */
  function bookCardHtml(b) {
    var coverHtml;
    if (b.cover_url) {
      coverHtml = '<img src="' + esc(b.cover_url) + '" alt="" loading="lazy">';
    } else {
      var first = (b.title || '?').trim().charAt(0) || '?';
      coverHtml = '<div class="tile"><div class="first-char">' + esc(first) + '</div>' +
        '<div class="tile-title">' + esc(b.title || '') + '</div></div>';
    }
    var folderHtml = b.folder ? '<div class="folder">' + esc(b.folder) + '</div>' : '';
    var progHtml = '';
    var pct = progressPct(b, progressById[b.id]);
    if (pct !== null) {
      progHtml = '<div class="prog"><div class="prog-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="prog-text">已读 ' + pct + '%</div>';
    }
    return '<div class="book" data-id="' + esc(b.id) + '" data-need-cover="' +
      ((!b.cover_url && b.format === 'pdf') ? '1' : '0') + '">' +
      '<div class="cover">' + coverHtml + '</div>' +
      '<div class="meta"><div class="t">' + esc(b.title || '未命名') + '</div>' +
      '<div class="sub"><span class="badge ' + esc((b.format || '').toLowerCase()) + '">' + esc(fmtBadge(b.format)) + '</span>' +
      '<span>' + esc(b.size_text || '') + '</span></div>' + folderHtml + progHtml + '</div></div>';
  }

  function renderGrid() {
    var q = (searchInput.value || '').trim().toLowerCase();
    var list = books.filter(function (b) {
      return !q || (b.title || '').toLowerCase().indexOf(q) !== -1;
    });
    countEl.textContent = list.length ? ('共 ' + list.length + ' 本') : '';
    if (!books.length) {
      shelfEl.innerHTML = '';
      emptyEl.classList.remove('hidden');
      emptyEl.innerHTML = '这里还没有书<br><span style="font-size:13px">先在设置里填好书库文件夹，再点“重新扫描”</span>';
      return;
    }
    if (!list.length) {
      shelfEl.innerHTML = '';
      emptyEl.classList.remove('hidden');
      emptyEl.innerHTML = '没有找到匹配的书';
      return;
    }
    emptyEl.classList.add('hidden');
    // 按文件夹分组：文件夹按字母排，根目录（""）在最前；组内书按书名排
    // （后端已按 folder/title 排好，这里只分组不重排，保持稳定）。
    var groups = {};
    var order = [];
    list.forEach(function (b) {
      var f = b.folder || '';
      if (!groups[f]) { groups[f] = []; order.push(f); }
      groups[f].push(b);
    });
    order.sort(function (a, b) {
      if (a === '') return -1;
      if (b === '') return 1;
      return a < b ? -1 : (a > b ? 1 : 0);
    });
    shelfEl.innerHTML = order.map(function (f) {
      var items = sortBooks(groups[f]);
      var label = f === '' ? '根目录' : f;
      return '<details class="folder-group">' +
        '<summary><span class="folder-name">' + esc(label) + '</span>' +
        '<span class="folder-count">' + items.length + ' 本</span></summary>' +
        '<div class="grid">' + items.map(bookCardHtml).join('') + '</div></details>';
    }).join('');

    // 点书进阅读器
    shelfEl.querySelectorAll('.book').forEach(function (el) {
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
    shelfEl.querySelectorAll('.book[data-need-cover="1"]').forEach(function (el) {
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
        // 书 id 恒为 16 位十六进制（book_id_for），可直接拼进属性选择器，
        // 不用 CSS.escape（jsdom 没有 CSS 对象，调它会抛错）。
        var tile = shelfEl.querySelector('.book[data-id="' + id + '"] .cover');
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
    apiFetch('/api/settings', {
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
      .catch(function (e) {
        if (e && e.unauthorized) return;
        setupStatus.textContent = '保存失败，请检查网络';
      });
  });

  // 修改账号密码：只改 auth 段，书库与进度不动；改完立即生效。
  document.getElementById('save-account-btn').addEventListener('click', function () {
    var cur = curPassEl.value;
    var nu = newUserEl.value.trim();
    var np = newPassEl.value;
    if (!cur) { accountStatus.textContent = '请先输入当前密码'; return; }
    if (!np) { accountStatus.textContent = '新密码不能为空'; return; }
    accountStatus.textContent = '保存中…';
    apiFetch('/api/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: cur, new_username: nu, new_password: np })
    })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: '服务器返回异常' }; }); })
      .then(function (data) {
        if (data.ok) {
          accountStatus.textContent = '已保存' + (nu ? ('，账号已改为 ' + nu) : '，新密码已生效');
          curPassEl.value = '';
          newUserEl.value = '';
          newPassEl.value = '';
          showToast('账号密码已保存');
        } else {
          accountStatus.textContent = data.error || '保存失败';
        }
      })
      .catch(function (e) {
        if (e && e.unauthorized) return;
        accountStatus.textContent = '保存失败，请检查网络';
      });
  });

  document.getElementById('logout-btn').addEventListener('click', function () {
    fetch('/api/logout', { method: 'POST' })
      .catch(function () {})
      .finally(function () { window.location.href = '/login'; });
  });

  document.getElementById('settings-link').addEventListener('click', function (e) {
    e.preventDefault();
    setupCard.classList.remove('hidden');
    accountCard.classList.remove('hidden');
    setupCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
    folderInput.focus();
  });

  document.getElementById('rescan-btn').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = '扫描中…';
    apiFetch('/api/rescan', { method: 'POST' })
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
      .catch(function (e) {
        if (e && e.unauthorized) return;
        showError('重新扫描失败，请检查网络');
      })
      .finally(function () { btn.disabled = false; btn.textContent = '重新扫描'; });
  });

  searchInput.addEventListener('input', renderGrid);

  // 排序切换：对所有书（含 PDF）生效，文件夹内重排。
  if (sortSelect) {
    sortSelect.addEventListener('change', function () {
      sortMode = sortSelect.value || 'title';
      renderGrid();
    });
  }

  // 设置页“返回书架”：收起两张设置卡，回到顶部。
  document.querySelectorAll('.back-home-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      setupCard.classList.add('hidden');
      accountCard.classList.add('hidden');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });

  /* ---------- 启动 ---------- */
  loadSettings();
  loadBooks().then(loadProgress).then(loadContinue);
})();
