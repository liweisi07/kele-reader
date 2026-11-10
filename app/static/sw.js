/* 满月阅读 — Service Worker */
var CACHE_NAME = 'manyue-v1';
var APP_SHELL = [
  '/',
 '/read',
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
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(APP_SHELL);
    }).catch(function () { /* 缓存失败不阻塞安装 */ })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        if (key !== CACHE_NAME) return caches.delete(key);
        return undefined;
      }));
    }).catch(function () {})
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

  // 页面（/ 与 /read）：网络优先，离线时回退缓存
  if (url.pathname === '/' || url.pathname === '/read') {
    event.respondWith(
      fetch(event.request).then(function (res) {
        if (res && res.ok) {
          var copy = res.clone();
          caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, copy); }).catch(function () {});
        }
        return res;
      }).catch(function () {
        return caches.match(event.request);
      })
    );
  }
});
