# 可乐阅读 — 前端说明（FRONTEND_NOTES）

## 写好的文件（都在 `~/workspace/manyue-reader/app/static/` 下）

| 文件 | 说明 |
|---|---|
| `index.html` | 书架页：顶栏（可乐阅读 / 设置 / 重新扫描）、继续阅读条、设置卡片、搜索框、书格 |
| `shelf.js` | 书架逻辑：拉书库、渲染、继续阅读、搜索、重新扫描、保存书库路径、懒生成 PDF 封面 |
| `reader.html` | 阅读器页骨架：顶栏（‹ 书架 / 标题 / 格式徽标）、viewer、底部工具栏 |
| `reader.js` | 阅读器逻辑：PDF / EPUB / TXT / AZW3 四种格式 + 进度恢复与保存 |
| `style.css` | 两页共享样式，移动优先，内容最大宽 720px，点按目标 ≥44px |
| `manifest.webmanifest` | PWA 清单（name/short_name 可乐阅读，standalone，主题色 #101828） |
| `sw.js` | Service Worker，缓存名 `kele-v1` |
| `pdf.min.js` / `pdf.worker.min.js` | pdfjs-dist 3.11.174 legacy UMD（已从 /tmp/vendor 拷入） |
| `epub.min.js` / `jszip.min.js` | epubjs 0.3.93 + JSZip（已从 /tmp/vendor 拷入） |
| `icons/icon-192.png` / `icons/icon-512.png` | PWA 图标：亮红底可乐杯（加冰，红白条纹吸管） |
| `FRONTEND_NOTES.md` | 本文件 |

## 依赖的 API 端点（按合同原样使用，未改动）

- `GET /`、`GET /read?book=<id>`、`GET /static/<file>`、`GET /manifest.json`、`GET /sw.js`、`GET /icons/icon-192.png`、`/icons/icon-512.png`
- `GET /api/books` — 书库列表（含 `cover_url`、`format`、`size_text`、`folder`）
- `GET /api/file/<id>` — 书籍字节（Range 支持；PDF.js / EPUB.js 直接取；TXT 分段取）
- `GET /api/cover/<id>` / `POST /api/cover/<id>`（`{"image":"data:image/jpeg;base64,..."}`）
- `GET /api/progress` / `POST /api/progress`（`{"book","loc","total"}`，2 秒节流 + pagehide/beforeunload 保存）
- `GET /api/settings` / `POST /api/settings`（`{"book_folder"}`）
- `POST /api/rescan`
- `GET /api/azw3/<id>/` 及 `/api/azw3/<id>/res/<name>`（iframe 同源加载）

进度 `loc` 语义按合同：PDF=页码（1 起）、EPUB=CFI、TXT=字符偏移、AZW3=滚动比例 0..1。

## 验证状态（2026-11-09 更新）

- **后端联调已验证**：`python3 tests/verify_e2e.py` 对真实服务器跑 43 项全过——书架、Range 流式（44MB PDF 超 20MB 段 206）、进度重启后仍在、AZW3 解包与图片、PWA 清单与 Service Worker、封面上传。`python3 -m unittest tests.test_server` 24 项全过。`scripts/verify_fpk.py` 对安装包全过。
- **前端 jsdom 验证**：`node tests/verify_browser_jsdom.js` **28/28 全过**——书架 6 本书、PDF 36 页渲染出非空白画面、TXT 分页、AZW3 章节文字、EPUB 封面页与第一章文字均已实测；EPUB 的 CFI 进度恢复也已实测（保存 `epubcfi(/6/4!/4/4/1:0)` 后重开，第一章正常显示）。jsdom 本身缺不少浏览器能力，测试脚本在 `beforeParse` 里逐项补齐（均为测试环境缺口，不是应用 bug）：`fetch`、`ReadableStream`、`setImmediate`（JSZip 内嵌 polyfill 在 jsdom 里不触发）、`URL.createObjectURL`（返回假 blob: 地址；章节内图片/CSS 在 jsdom 里加载失败，已在断言里过滤，真浏览器正常）、`Range#getBoundingClientRect`（epub.js 用它量章节文字，iframe 内外两个 realm 都要补）、`window.scrollTo`、`HTMLElement#getBoundingClientRect`/`clientWidth`/`clientHeight`（固定手机尺寸）、iframe 的 `srcdoc`（jsdom 从不解析它，删掉该属性让 epub.js 走 `document.write` 回退）。另：epub.js 的 continuous 管理器会把每个章节各放一个 iframe，断言要扫全部 iframe；jsdom 没有真实布局，滚动翻页（`next()`）无法推进章节，改用进度恢复验证第一章。
- **仍待实测（真机/真浏览器）**：
  1. EPUB scrolled-flow 在真机上的翻页手感与 `relocated` 的页码（jsdom 里因无布局只显示“第 1 / 1 页”，真机按内容高度分页）。
  2. TXT 的 Range 行为：假设首个响应带 `Content-Range: bytes a-b/<total>`；若后端对无 Range 的请求直接返回 200 全文，代码按“一次取完”处理；若后端返回的 total 与实际字节数不一致，分段循环可能多取或提前结束。
  3. TXT 编码：先 UTF-8 严格解码，失败回退 GBK；若文本是 UTF-8 但含个别非法字节，会整体按 GBK 解码而出现乱码。
  4. AZW3 iframe：假设 `/api/azw3/<id>/` 返回的 HTML 同源可访问 `contentDocument`；错误页检测只认正文含 `"ok":false`；若后端错误页是纯 HTML 或 iframe 加载超时 15 秒，会显示“打不开”提示。iframe 内滚动恢复依赖 `scrollingElement`，若提取页把滚动放在内层容器上，恢复/保存位置会失效。
  5. PDF.js 大文件：jsdom 已实测 44MB 文件按需取页、36 页正常渲染；封面生成（浏览器渲染第 1 页）同样已在 jsdom 实测通过。
  6. Service Worker：`cache.addAll` 里任何一个 URL 404 会导致整批安装缓存失败（已 catch，不阻塞页面，但离线壳可能不完整）。
  7. `/api/books` 的 `folder` 字段假设为子文件夹名（短名）；若后端返回完整路径，书格下会显示过长路径（CSS 已做单行省略）。
  8. 进度接口：`GET /api/progress` 返回的 `updated` 字段格式为 `YYYY-MM-DD HH:MM:SS`，继续阅读条按字符串比较取最新一条（e2e 已验证进度写入与读取）。
- **仅做过的验证**：`node --check`（shelf.js、reader.js）、`python3 -m json.tool`（manifest）、四个 vendored JS 存在且非空、两个图标 PNG 尺寸校验；另见上文的 e2e 与 jsdom 实测。未在真机上跑过页面，EPUB 真机渲染与手机排版仍待装机后确认。
