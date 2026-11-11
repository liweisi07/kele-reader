/* 可乐阅读 — Service Worker */
 // 缓存名必须跟版本号走（kele-v<版本>）：1.0.3 的教训——缓存名一直
 // 是 kele-v1，1.0.2 升 1.0.3 后浏览器仍用缓存的旧 reader.js（没有
 // 章节导航代码），新页面上的“上一章/下一章”按钮就成了摆设。发版
 // 改版本号时务必同步改这里；verify_fpk 会校验缓存名含当前版本。
var CACHE_NAME = 'kele-v1.0.6';
// 只预缓存公开静态资源；/ 与 /read 要登录，不预缓存（登录页
// 不能缓存到 / 下面），页面一律走网络。
var APP_SHELL = [
  '/static/style.css',
  '/static/shelf.js',
  '/static/reader.js',
  '/static/pdf.min.js',
  '/static/pdf.worker.min.js',
  '/static/epub.min.js',
  '/static/jszip.min.js',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

self.addEventListener('install', function (event) {
  // 立即接管：不等旧页面全部关闭，新版本一装好就激活并清旧缓存，
  // 否则升级后仍可能跑旧缓存的 JS。
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(APP_SHELL);
    }).catch(function () { /* 缓存失败不阻塞安装 */ })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    Promise.all([
      clients.claim(),
      caches.keys().then(function (keys) {
        return Promise.all(keys.map(function (key) {
          if (key !== CACHE_NAME) return caches.delete(key);
          return undefined;
        }));
      }),
    ]).catch(function () {})
  );
});

self.addEventListener('fetch', function (event) {
  var url;
  try { url = new URL(event.request.url); } catch (e) { return; }
  if (event.request.method !== 'GET') return;
  // 只处理同源请求
  if (url.origin !== self.location.origin) return;

  // /api/* 一律走网络，绝不缓存（包括 /api/file/* 书籍字节）
  if (url.pathname.indexOf('/api/') === 0) {
    event.respondWith(fetch(event.request));
    return;
  }

  var isStatic = url.pathname.indexOf('/static/') === 0 ||
    url.pathname.indexOf('/icons/') === 0 ||
    url.pathname === '/manifest.json';

  if (isStatic) {
    // 静态资源：缓存优先
    event.respondWith(
      caches.match(event.request).then(function (cached) {
        if (cached) return cached;
        return fetch(event.request).then(function (res) {
          if (res && res.ok) {
            var copy = res.clone();
            caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, copy); }).catch(function () {});
          }
          return res;
        });
      }).catch(function () { return fetch(event.request); })
    );
    return;
  }

  // 页面（/、/read、/login）：只走网络，不缓存。未登录时 / 会 302
  // 到 /login，跟随后的登录页绝不能缓存到 / 下面；已登录的书架壳
  // 缓存了也会在退出后被离线读到，干脆不缓存页面。
  if (url.pathname === '/' || url.pathname === '/read' ||
      url.pathname === '/login') {
    event.respondWith(fetch(event.request));
  }
});
