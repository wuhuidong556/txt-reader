/* TXT 朗读器 —— 前端逻辑
 * 播放方式：<audio> 直接播 /api/audio?seg=N（服务端保证该段已合成），
 * 播完自动下一段；点正文任意一段即跳读。 */
"use strict";

const T = window.__TOKEN__ || "";
const $ = (id) => document.getElementById(id);
const CHUNK = 60;                       // 每次拉取的段数

const S = {
  state: null,
  segs: [],          // [{i, text, start}]
  from: 0,
  total: 0,
  cur: -1,
  playing: false,
  loading: false,
  chapter: -1,
  lang: "zh",        // 中英对照书：这一段念中文(zh)还是英文(en)
};

/* ── 网络 ── */
async function api(path, body) {
  const opts = { headers: { "X-Token": T } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts).catch((e) => {
    // 连不上后端（服务没起 / 刚被关掉）—— 也要给个明白话，而不是只抛 Failed to fetch
    showOffline(true, "连不上后端服务（可能没在运行），请重新启动「TXT朗读器」");
    throw new Error("连不上后端：" + e.message);
  });
  let data = null;
  try { data = await r.json(); } catch (e) { data = { ok: false, error: "返回不是 JSON" }; }

  // 403 几乎只有一个原因：后端重启过 → token 换了，而本页还捏着旧的。
  // 不自动处理的话，用户看到的是满屏「未授权」，只能自己猜到要按 F5。
  if (r.status === 403) {
    const last = Number(sessionStorage.getItem("reloadedAt") || "0");
    if (Date.now() - last > 8000) {                     // 防刷新死循环
      sessionStorage.setItem("reloadedAt", String(Date.now()));
      showOffline(true, "服务已重新启动，正在自动刷新页面…");
      setTimeout(() => location.reload(), 700);
    }
    throw new Error("服务已重启，请刷新页面（F5）");
  }
  if (!r.ok || data.ok === false) {
    throw new Error((data && data.error) || ("HTTP " + r.status));
  }
  return data;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 2600);
}
function setStatus(m) { $("status").textContent = m; }
function showOffline(on, msg) {
  const el = $("offline");
  $("offline-msg").textContent = msg || "";
  el.classList.toggle("hidden", !on);
}

/* ── 渲染 ── */
function renderBooks() {
  const ul = $("book-list");
  ul.innerHTML = "";
  const books = (S.state && S.state.books) || [];
  $("book-count").textContent = books.length ? "(" + books.length + ")" : "";
  if (!books.length) {
    ul.innerHTML = '<li style="cursor:default;color:#9aa0b4">还没有书，点右上角「导入 TXT」</li>';
    return;
  }
  const curId = S.state.current && S.state.current.id;
  books.forEach((b) => {
    const li = document.createElement("li");
    li.className = b.id === curId ? "active" : "";
    const p = (b.progress && b.progress.seg) || 0;
    li.innerHTML = '<span class="t"></span><span class="m">' + p + "/" + b.seg_count + " 段</span>"
      + '<span class="del" title="移除">✕</span>';
    li.querySelector(".t").textContent = b.title;
    li.addEventListener("click", (ev) => {
      if (ev.target.className === "del") {
        if (confirm("移除《" + b.title + "》？（不影响原文件）")) {
          // 如果移除的正是当前在放的那本，先把播放停干净 ——
          // 否则音频请求会撞上"书已经没了"，界面留下一条莫名其妙的「播放失败」。
          if (S.state && S.state.current && S.state.current.id === b.id) {
            try {
              audio.pause();
              audio.removeAttribute("src");
              audio.load();
            } catch (e) { /* 忽略 */ }
            S.playing = false;
            S.cur = -1;
            S.segs = [];
            playToken++;              // 让在途的 play() 回调全部作废
            pendingSeg = -1;
            paintPlay();
            setStatus("已停止");
          }
          api("/api/remove", { id: b.id }).then((d) => {
            const r = d.removed || {};
            const skipped = (r.skipped || []).length;
            S.state = d;
            afterOpen();
            if (skipped) {
              toast("已移除《" + b.title + "》；有 " + skipped + " 项缓存正被占用，" +
                    "下次删除 cache/ 即可");
            } else {
              toast("已移除《" + b.title + "》（清了 " + (r.cache_cleared || 0) + " 项缓存）");
            }
          }).catch((e) => toast("移除失败：" + e.message));
        }
        return;
      }
      openBook(b.id);
    });
    ul.appendChild(li);
  });
}

/* ── docx 仓库（译文 docx 的集中存放处）── */
async function loadDocxs() {
  try {
    const d = await api("/api/docxs");
    renderDocxs(d.items || []);
  } catch (e) {
    // 仓库读不到不该影响听书，只把提示放清单里
    const ul = $("docx-list");
    if (ul) ul.innerHTML = '<li style="cursor:default;color:#9aa0b4">仓库读不到：' +
      (e && e.message ? e.message : "未知错误") + "</li>";
  }
}

function fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1024)) + " KB";
}

/* 仓库文件名形如「书名_难度3_中英对照版.docx」——侧栏窄，拆成
 * 「书名」+「难度3 · 191 KB」，两本同名不同难度的才分得清。 */
function docxLabel(name) {
  const base = name.replace(/\.docx$/i, "");
  const m = base.match(/^(.*)_难度(\d+)_中英对照版$/);
  return m ? { title: m[1], tag: "难度" + m[2] } : { title: base, tag: "" };
}

function renderDocxs(items) {
  const ul = $("docx-list");
  if (!ul) return;
  ul.innerHTML = "";
  $("docx-count").textContent = items.length ? "(" + items.length + ")" : "";
  if (!items.length) {
    ul.innerHTML = '<li style="cursor:default;color:#9aa0b4">还没有译文 docx —— 跑一次「译成英文」就会存到这儿</li>';
    return;
  }
  items.forEach((it) => {
    const li = document.createElement("li");
    li.innerHTML = '<span class="t"></span><span class="m"></span>'
      + '<span class="del" title="从仓库删除（不动导出目录）">✕</span>';
    const lb = docxLabel(it.name);
    const t = li.querySelector(".t");
    t.textContent = lb.title;
    t.title = it.name;                       // 悬停看完整文件名
    const tm = [lb.tag, fmtSize(it.size || 0)].filter(Boolean).join(" · ");
    li.querySelector(".m").textContent = tm;
    li.addEventListener("click", async (ev) => {
      if (ev.target.className === "del") {
        if (!confirm("从 docx 仓库删掉这个文件？\n" + it.name +
                     "\n（exports 里的原文件不受影响）")) return;
        try {
          const d = await api("/api/docx/remove", { name: it.name });
          renderDocxs(d.items || []);
          toast("已从仓库删除");
        } catch (e) { toast("删除失败：" + e.message); }
        return;
      }
      try {
        await api("/api/docx/open", { name: it.name });
        setStatus("已用 Word 打开：" + it.name);
      } catch (e) { toast("打开失败：" + e.message); }
    });
    ul.appendChild(li);
  });
}

function renderChapters() {
  const ul = $("chap-list");
  ul.innerHTML = "";
  const b = S.state && S.state.current;
  if (!b || !b.chapters || !b.chapters.length) {
    ul.innerHTML = '<li style="cursor:default;color:#9aa0b4">—</li>';
    return;
  }
  $("chap-book").textContent = b.title;
  b.chapters.forEach((c, idx) => {
    const li = document.createElement("li");
    li.textContent = c.title;
    li.className = idx === S.chapter ? "active" : "";
    li.addEventListener("click", async () => {
      // 精确跳章：段表在服务端是全的，问它要段号，别在前端已加载的一小段里瞎找
      try {
        const d = await api("/api/seg_at?id=" + encodeURIComponent(b.id) + "&char=" + c.start);
        await focusSeg(d.seg, true);
      } catch (e) {
        toast("跳转失败：" + e.message);
      }
    });
    ul.appendChild(li);
  });
  if (S.chapter >= 0 && ul.children[S.chapter]) {
    ul.children[S.chapter].scrollIntoView({ block: "nearest" });
  }
}

/* ── 正文窗口（懒加载）──
 * 长文不能一次全塞进 DOM（540 段的书会卡），但**也只加载过 60 段就再也不管**是错的 ——
 * 主人实测「全文过长时加载不完全」就是这么来的。
 * 现在：初始取 60 段；滚到底自动往后接、滚到顶自动往前接；目录跳转走
 * `/api/seg_at` 精确算段号（服务端段表是全的），不再受"只加载了一小段"的限制。
 * ⚠️ CHUNK 已在上方统一定义，这里**不要再声明一次** —— 重复 const 是语法错，
 *    整个 app.js 会被浏览器拒绝执行，页面看着像"JS 没跑"。
 */
let loadingMore = false;

function segNode(s) {
  const d = document.createElement("div");
  d.className = "seg" + (s.i === S.cur ? " cur" : "");
  d.dataset.i = s.i;
  d.textContent = s.text;
  d.addEventListener("click", () => jumpTo(s.i, true));
  return d;
}

async function loadWindow(from, count) {
  const d = await api("/api/segs?from=" + Math.max(0, from) + "&count=" + count);
  S.segs = d.segs;
  S.from = d.from;
  S.total = d.total;
  renderText();
}

async function appendMore() {
  if (loadingMore) return;
  const end = S.from + S.segs.length;
  if (S.total && end >= S.total) return;
  loadingMore = true;
  try {
    const d = await api("/api/segs?from=" + end + "&count=" + CHUNK);
    if (!d.segs || !d.segs.length) return;
    S.segs = S.segs.concat(d.segs);
    const box = $("text");
    const frag = document.createDocumentFragment();
    d.segs.forEach((s) => frag.appendChild(segNode(s)));
    box.appendChild(frag);
    S.total = d.total;
  } catch (e) {
    /* 追加失败不打扰用户，滚动时会再试 */
  } finally {
    loadingMore = false;
  }
}

async function prependMore() {
  if (loadingMore || S.from <= 0) return;
  loadingMore = true;
  const box = $("text");
  const before = box.scrollHeight;
  try {
    const from = Math.max(0, S.from - CHUNK);
    const cnt = S.from - from;
    if (cnt <= 0) return;
    const d = await api("/api/segs?from=" + from + "&count=" + cnt);
    if (!d.segs || !d.segs.length) return;
    S.segs = d.segs.concat(S.segs);
    S.from = from;
    renderText();
    box.scrollTop += (box.scrollHeight - before);   // 补上前面插入的高度，视觉位置不动
  } catch (e) {
    /* 同上 */
  } finally {
    loadingMore = false;
  }
}

/** 把某个段号摆到窗口中间（目录跳转、跨章跳读用）。 */
async function focusSeg(i, play) {
  if (i < 0) return;
  const from = Math.max(0, i - Math.floor(CHUNK / 2));
  const already = i >= S.from && i < S.from + S.segs.length;
  if (!already) {
    await loadWindow(from, CHUNK);
  } else if (!play) {
    highlight(true);
    return;
  }
  S.cur = i;
  highlight(true);
  if (play) playSeg(i);
}

function findSegByChar(ch) {
  for (let k = 0; k < S.segs.length; k++) if (S.segs[k].start >= ch) return S.segs[k].i;
  return -1;
}

function renderText() {
  const box = $("text");
  box.innerHTML = "";
  if (!S.segs.length) {
    box.innerHTML = '<div class="empty">双击左侧书库里的书名开始阅读</div>';
    return;
  }
  const frag = document.createDocumentFragment();
  S.segs.forEach((s) => frag.appendChild(segNode(s)));
  box.appendChild(frag);
  highlight();
}

function highlight(scroll) {
  const box = $("text");
  const cur = box.querySelector(".seg.cur");
  if (cur) cur.classList.remove("cur");
  const el = box.querySelector('.seg[data-i="' + S.cur + '"]');
  if (el) {
    el.classList.add("cur");
    if (scroll) el.scrollIntoView({ block: "center" });
  }
  const b = S.state && S.state.current;
  $("pos").textContent = (S.cur + 1) + " / " + (S.total || (b ? b.seg_count : 0));
  if (b && b.chapters) {
    const st = (S.segs.find((x) => x.i === S.cur) || {}).start || 0;
    let ci = 0;
    b.chapters.forEach((c, k) => { if (c.start <= st) ci = k; });
    if (ci !== S.chapter) { S.chapter = ci; renderChapters(); }
  }
}

/* ── 数据 ── */
async function refresh() {
  try {
    S.state = await api("/api/state");
    showOffline(false);
  } catch (e) {
    showOffline(true, "读不到后端状态：" + e.message);
    throw e;
  }

  // 版本戳：本页的脚本比服务端旧 ⇒ 立刻换新的（否则会一直跑旧逻辑）
  const sv = S.state.build, mine = window.__BUILD__;
  if (sv && mine && sv !== mine) {
    const last = Number(sessionStorage.getItem("buildReloadedAt") || "0");
    if (Date.now() - last > 8000) {
      sessionStorage.setItem("buildReloadedAt", String(Date.now()));
      showOffline(true, "程序已更新，正在刷新到新版本…");
      setTimeout(() => location.reload(), 600);
      return;
    }
  }

  const eng = S.state.engine || {};
  const badge = $("engine-badge");
  const online = eng.online;
  badge.textContent = online === true ? "● 在线音色（Edge TTS）"
    : online === false ? "● 离线音色（系统 SAPI）" : "● 引擎待检测";
  badge.className = "badge " + (online === true ? "ok" : online === false ? "bad" : "");
  $("cache-badge").textContent = "缓存 " + (S.state.cache_mb || 0) + " MB";
  fillVoices();
  renderBooks();
  loadDocxs();
  if (S.state.lang) S.lang = S.state.lang;
  syncLang();
  if (S.state.current) {
    $("book-title").textContent = S.state.current.title;
    $("book-meta").textContent = S.state.current.chars + " 字 · "
      + S.state.current.seg_count + " 段 · 编码 " + S.state.current.encoding
      + " · 导入于 " + S.state.current.added;
    $("sel-rate").value = S.state.rate || "+0%";
    S.chapter = -1;
    renderChapters();
  }
}

function fillVoices() {
  const sel = $("sel-voice");
  const want = (S.state && S.state.voice) || "";
  if (sel.dataset.filled === "1" && sel.value === want) return;
  sel.innerHTML = "";
  (S.state.voices || []).forEach((v) => {
    const o = document.createElement("option");
    o.value = v.id;
    o.textContent = v.name;
    sel.appendChild(o);
  });
  sel.value = want;
  sel.dataset.filled = "1";
}

async function loadSegs(from) {
  try {
    await loadWindow(from, CHUNK);
  } catch (e) {
    toast("加载正文失败：" + e.message);
  }
}

async function openBook(id) {
  try {
    setStatus("打开中…");
    const d = await api("/api/open", { id: id });
    S.state = d;
    S.segs = [];
    S.cur = -1;
    S.chapter = -1;
    await afterOpen();
    setStatus("就绪");
  } catch (e) {
    toast("打开失败：" + e.message);
    setStatus("出错");
  }
}

/* 把库里的编码名映射到下拉框选项 */
const ENC_ALIAS = {
  "utf_8": "utf-8", "utf8": "utf-8", "utf-8": "utf-8",
  "gb18030": "gb18030", "gbk": "gb18030", "gb2312": "gb18030",
  "utf_16": "utf-16", "utf-16": "utf-16", "utf16": "utf-16",
  "big5": "big5",
};
function syncEncSelect(book) {
  const sel = $("sel-enc");
  const raw = String((book && book.encoding) || "").toLowerCase().split("(")[0].trim();
  const want = ENC_ALIAS[raw] || "auto";
  if ([...sel.options].some((o) => o.value === want)) sel.value = want;
}

async function afterOpen() {
  renderBooks();
  const b = S.state && S.state.current;
  if (!b) { renderText(); return; }
  $("book-title").textContent = b.title;
  $("book-meta").textContent = b.chars + " 字 · " + b.seg_count + " 段 · 编码 "
    + b.encoding + " · 导入于 " + b.added;
  syncEncSelect(b);
  S.lang = (S.state && S.state.lang) || "zh";
  syncLang();
  const startSeg = (b.progress && b.progress.seg) || 0;
  const from = Math.max(0, startSeg - 5);
  await loadSegs(from);
  S.cur = startSeg;
  highlight(true);
  api("/api/prefetch", { seg: startSeg, lang: S.lang }).catch(() => {});
}

/* ── 播放 ──
 * 关键：`play()` 返回的是 Promise，而它**只有在还没有被新的 load 取代时才有效**。
 * 快速点两下（点段落 / 空格 / 方向键都走这里）就会让前一个 play() 以
 * `AbortError: The play() request was interrupted by a new load request` 拒绝 ——
 * 这**不是**播放失败，是老请求过期了。早先我把它当失败弹红字，属于误报。
 * 现在用 playToken 记账：过期的回调一律丢弃，AbortError 一律静默。
 */
const audio = $("audio");
let playToken = 0;          // 每次切段 +1，用来判断回调是否过期
let pendingSeg = -1;        // 正在加载（服务端还在合成）的段号

function isAbort(e) {
  return !!e && (e.name === "AbortError" || /interrupted by a new load request|interrupted by the user agent/i.test(e.message || ""));
}

/* ── 朗读语言：中英对照书可以选这一段念中文还是念英文 ── */
function syncLang() {
  const bi = !!(S.state && S.state.bilingual);
  $("lang-switch").classList.toggle("hidden", !bi);
  $("lang-zh").classList.toggle("on", S.lang !== "en");
  $("lang-en").classList.toggle("on", S.lang === "en");
}

function setLang(lang) {
  const next = lang === "en" ? "en" : "zh";
  if (next === S.lang) return;
  S.lang = next;
  syncLang();
  api("/api/lang", { lang: S.lang }).catch(() => {});
  if (S.cur >= 0) playSeg(S.cur);        // 立刻用另一种语言重念这一段
}

function playSeg(i) {
  if (i < 0 || (S.total && i >= S.total)) return;

  // 同一段还在加载中，就别再触发一次（否则自己把自己打断）
  if (i === pendingSeg && audio.readyState < 3 && S.playing) return;

  const token = ++playToken;
  S.cur = i;
  pendingSeg = i;
  S.playing = false;
  highlight(true);
  setStatus("合成第 " + (i + 1) + " 段…");
  paintPlay();

  audio.src = "/api/audio?seg=" + i + "&lang=" + encodeURIComponent(S.lang)
    + "&token=" + encodeURIComponent(T);
  audio.play().then(() => {
    if (token !== playToken) return;          // 已被后来的切段取代 → 丢弃
    pendingSeg = -1;
    S.playing = true;
    setStatus("朗读中");
    paintPlay();
  }).catch((e) => {
    if (token !== playToken) return;          // 过期请求的报错
    if (isAbort(e)) {                         // 新 load 打断旧 play → 正常现象
      setStatus("已切换");
      return;
    }
    pendingSeg = -1;
    S.playing = false;
    paintPlay();
    setStatus("播放失败");
    toast("播放失败：" + ((e && e.message) || e));
  });

  api("/api/prefetch", { seg: i + 1, lang: S.lang }).catch(() => {});
  saveProgress();
}

function togglePlay() {
  if (!S.state || !S.state.current) { toast("先导入并打开一本书"); return; }

  if (S.playing) {                            // 正在读 → 暂停
    audio.pause();
    S.playing = false;
    setStatus("已暂停");
  } else if (S.cur >= 0 && audio.src && audio.readyState >= 2) {
    // 有现成的一段 → 原地续播（不重设 src，避免触发 load 打断）
    const p = audio.paused ? audio.play() : null;
    S.playing = true;
    setStatus("朗读中");
    if (p) p.catch((e) => {
      if (isAbort(e)) return;
      S.playing = false;
      setStatus("播放失败");
      toast("播放失败：" + ((e && e.message) || e));
    });
  } else {
    playSeg(S.cur < 0 ? 0 : S.cur);           // 没开始过 / 还没缓冲好 → 走切段
  }
  paintPlay();
}

function paintPlay() {
  const b = $("btn-play");
  b.textContent = S.playing ? "⏸" : "▶";
  b.classList.toggle("on", S.playing);
}

function jumpTo(i, play) {
  if (play) playSeg(i);
  else { S.cur = i; highlight(true); }
}

function saveProgress() {
  const b = S.state && S.state.current;
  if (!b || S.cur < 0) return;
  api("/api/progress", { id: b.id, seg: S.cur }).catch(() => {});
}

audio.addEventListener("ended", () => {
  playSeg(S.cur + 1);
});
audio.addEventListener("error", () => {
  S.playing = false; paintPlay();
  setStatus("这一段播不了");
  toast("音频加载失败（后端可能还在合成，稍后再点播放）");
});

/* ── 导入 ──
 * 有原生窗口时走「Python 侧选文件 + 导入」，**路径不经过 JS 桥** ——
 * 实测把中文路径交给桥往返一圈后，服务端收到的字符串 exists() 为假。
 * 桥只回一个 ASCII 的 book_id，书名等中文一律由 /api/state 走 HTTP 取。 */
async function doImport() {
  try {
    setStatus("导入中…");
    if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_and_import) {
      const r = await window.pywebview.api.pick_and_import();
      if (r && r.cancelled) { setStatus("已取消"); return; }
      if (!r || !r.ok) {
        const code = r && r.code ? r.code : "?";
        const st = r && r.status ? (" HTTP " + r.status) : "";
        // 兜底：原生导入失败时，允许手输路径 —— 这条路直接走 HTTP、不经任何桥，
        // 是**已被验证可靠**的通道，保证用户永远有一条能用。
        const manual = prompt("原生导入失败（" + code + st + "）。\n可以直接把 TXT 的完整路径粘在这里导入：");
        if (!manual) { setStatus("导入失败"); toast("已取消"); return; }
        const d = await api("/api/import", { path: manual.trim() });
        S.state = d;
        S.segs = []; S.cur = -1; S.chapter = -1;
        await afterOpen();
        setStatus("导入完成");
        toast("已导入《" + d.book.title + "》（编码 " + d.book.encoding + "）");
        return;
      }
      await refresh();                       // 书名等中文走 HTTP，不走桥
      S.segs = []; S.cur = -1; S.chapter = -1;
      await afterOpen();
      setStatus("导入完成");
      toast("已导入，共 " + ((r && r.segs) || "?") + " 段");
      return;
    }
    // 浏览器兜底：手输路径，直接 POST（这条路径不经过任何桥，已实测可靠）
    const path = prompt("请输入 TXT 文件的完整路径：");
    if (!path) { setStatus("已取消"); return; }
    const d = await api("/api/import", { path: path });
    S.state = d;
    S.segs = []; S.cur = -1; S.chapter = -1;
    await afterOpen();
    setStatus("导入完成");
    toast("已导入《" + d.book.title + "》（编码 " + d.book.encoding + "）");
  } catch (e) {
    toast("导入失败：" + e.message);
    setStatus("导入失败");
  }
}

/* ── 导出 ── */
async function doExport() {
  if (!S.state || !S.state.current) { toast("先打开一本书"); return; }
  try {
    await api("/api/export", { id: S.state.current.id });
    toast("已开始导出，慢工出细活，进度看状态栏");
    pollExport();
  } catch (e) {
    toast("导出启动失败：" + e.message);
  }
}

async function pollExport() {
  try {
    const d = await api("/api/export/status");
    if (d.running) {
      setStatus("导出中 " + d.done + "/" + d.total);
      setTimeout(pollExport, 1500);
    } else if (d.path) {
      setStatus("导出完成");
      toast("已导出到：" + d.path);
    } else if (d.error) {
      setStatus("导出失败");
      toast("导出失败：" + d.error);
    }
  } catch (e) { /* 忽略轮询错误 */ }
}

/* ── 音量：直接调 <audio>.volume，无需重新合成，实时生效 ── */
function initVolume() {
  const saved = Number(localStorage.getItem("vol") || "100");
  const rng = $("rng-vol");
  rng.value = String(saved);
  audio.volume = saved / 100;
  $("vol-num").textContent = saved + "%";
  rng.addEventListener("input", () => {
    const v = Number(rng.value);
    audio.volume = v / 100;
    $("vol-num").textContent = v + "%";
    localStorage.setItem("vol", String(v));
  });
}

/* ── 编码：自动识别不准时手动指定重读 ── */
async function doRecode() {
  const b = S.state && S.state.current;
  if (!b) { toast("先打开一本书"); return; }
  const enc = $("sel-enc").value;
  try {
    setStatus("按 " + enc + " 重读中…");
    const d = await api("/api/recode", { id: b.id, encoding: enc });
    S.state = d;
    S.segs = []; S.cur = -1; S.chapter = -1;
    await afterOpen();
    const got = d.book ? d.book.encoding : enc;
    const bad = (d.book && d.book.bad_chars) || 0;
    setStatus("已按 " + got + " 重读");
    if (bad > 0) {
      toast("⚠ 按 " + got + " 读出 " + bad + " 个无法解码的字符（正文里有乱码 = 这个编码不对）");
    } else {
      toast("已按 " + got + " 重新读取，正文与目录已更新");
    }
  } catch (e) {
    toast("重读失败：" + e.message);
    setStatus("重读失败");
  }
}

/* ── 事件绑定 ── */
$("btn-import").addEventListener("click", doImport);
$("btn-play").addEventListener("click", togglePlay);
$("btn-prev").addEventListener("click", () => playSeg(Math.max(0, S.cur - 1)));
$("btn-next").addEventListener("click", () => playSeg(S.cur + 1));
$("btn-export").addEventListener("click", doExport);
$("sel-enc").addEventListener("change", doRecode);

// 正文滚动到底/顶 → 继续加载（长文不再"只加载一小段就不管了"）
(() => {
  const box = $("text");
  let last = 0;
  box.addEventListener("scroll", () => {
    const now = Date.now();
    if (now - last < 200) return;          // 节流，别把鼠标滚轮变成请求风暴
    last = now;
    if (box.scrollTop + box.clientHeight > box.scrollHeight - 500) appendMore();
    if (box.scrollTop < 400) prependMore();
  });
})();
$("sel-voice").addEventListener("change", (e) => {
  const b = S.state && S.state.current;
  if (!b) return;
  api("/api/settings", { id: b.id, voice: e.target.value })
    .then((d) => { S.state = d; toast("音色已切换"); })
    .catch((err) => toast("切换失败：" + err.message));
});
$("sel-rate").addEventListener("change", (e) => {
  const b = S.state && S.state.current;
  if (!b) return;
  api("/api/settings", { id: b.id, rate: e.target.value })
    .then((d) => { S.state = d; toast("语速已调整"); })
    .catch((err) => toast("调整失败：" + err.message));
});
document.addEventListener("keydown", (ev) => {
  if (ev.target.tagName === "SELECT" || ev.target.tagName === "INPUT") return;
  if (ev.code === "Space") { ev.preventDefault(); togglePlay(); }
  if (ev.code === "ArrowRight") playSeg(S.cur + 1);
  if (ev.code === "ArrowLeft") playSeg(Math.max(0, S.cur - 1));
});
window.addEventListener("beforeunload", saveProgress);
setInterval(() => { if (S.playing) saveProgress(); }, 10000);

// 每 30 秒对一次版本戳：页面就算一直开着，程序更新后也会自己换到新版。
// （防的就是「重启了服务、页面还跑着旧代码」这类事。）
setInterval(async () => {
  try {
    const d = await api("/api/state");
    if (d && d.build && window.__BUILD__ && d.build !== window.__BUILD__) {
      showOffline(true, "程序已更新，正在刷新到新版本…");
      setTimeout(() => location.reload(), 500);
      return;
    }
    S.state = d;
  } catch (e) { /* 轮询失败不打扰用户 */ }
}, 30000);

/* ── 启动 ── */
(async function boot() {
  try {
    initVolume();
    await refresh();
    if (S.state && S.state.current) await afterOpen();
    else renderText();
  } catch (e) {
    showOffline(true, "启动失败：" + e.message);
  }
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.ready();
  }
})();

/* ═══════════════ 译成英文（DeepSeek · 高考难度） ═══════════════ */
const TR = { cfg: null, timer: null, running: false, files: [] };

function trEl(tag, props, kids) {
  const n = document.createElement(tag);
  if (props) for (const k in props) {
    if (k === "class") n.className = props[k];
    else if (k === "text") n.textContent = props[k];
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), props[k]);
    else n.setAttribute(k, props[k]);
  }
  (kids || []).forEach((c) => n.appendChild(c));
  return n;
}

function trOpen() {
  $("tr-modal").classList.remove("hidden");
  trRestoreOutputs();
  trRestoreAutoImport();
  trUpdateOutHint();
  trLoadCfg();
}

function trClose() {
  $("tr-modal").classList.add("hidden");
}

/* —— 产物勾选：中英对照 / 纯英文 / 英文音频 / 中文音频，各勾各的 —— */
const TR_OUT_KEYS = ["bilingual", "en_doc", "mp3_en", "mp3_zh"];
const TR_OUT_EL = {
  bilingual: "tr-out-bilingual", en_doc: "tr-out-endoc",
  mp3_en: "tr-out-mp3en", mp3_zh: "tr-out-mp3zh",
};

function trGetOutputs() {
  const o = {};
  TR_OUT_KEYS.forEach((k) => { o[k] = !!$(TR_OUT_EL[k]).checked; });
  return o;
}

function trSetOutputs(o) {
  TR_OUT_KEYS.forEach((k) => { if (k in o) $(TR_OUT_EL[k]).checked = !!o[k]; });
  trUpdateOutHint();
}

function trSaveOutputs() {
  try { localStorage.setItem("tr_outputs", JSON.stringify(trGetOutputs())); } catch (e) {}
}

function trRestoreOutputs() {
  try {
    const s = localStorage.getItem("tr_outputs");
    if (s) {
      const o = JSON.parse(s);
      TR_OUT_KEYS.forEach((k) => { if (k in o) $(TR_OUT_EL[k]).checked = !!o[k]; });
    }
  } catch (e) {}
}

function trNeedEn() {
  const o = trGetOutputs();
  return o.bilingual || o.en_doc || o.mp3_en;
}

/* —— 收尾：译完把 txt 直接收进书库，省得再手动导入一次 —— */
function trAutoImport() { return !!$("tr-auto-import").checked; }

function trSaveAutoImport() {
  try { localStorage.setItem("tr_auto_import", trAutoImport() ? "1" : "0"); } catch (e) {}
}

function trRestoreAutoImport() {
  try {
    const s = localStorage.getItem("tr_auto_import");
    if (s === "0") $("tr-auto-import").checked = false;
    else if (s === "1") $("tr-auto-import").checked = true;
  } catch (e) {}
}

function trUpdateOutHint() {
  const o = trGetOutputs();
  const any = TR_OUT_KEYS.some((k) => o[k]);
  const needEn = trNeedEn();
  const hint = $("tr-out-hint");
  if (!any) {
    hint.textContent = "⚠ 至少要勾一个产物";
    hint.style.color = "#e66";
  } else if (!needEn) {
    hint.textContent = "只出中文音频：不调用 DeepSeek、不翻译，直接合成原文朗读";
    hint.style.color = "";
  } else {
    hint.textContent = "";
    hint.style.color = "";
  }
  $("tr-level").disabled = !needEn;      // 不翻译时难度没意义
  $("tr-start").textContent = needEn ? "开始翻译" : "开始导出";
  $("tr-start").disabled = !any;
  const ih = $("tr-import-hint");
  if (ih) {
    ih.textContent = !(o.bilingual || o.en_doc) ? "（没勾文本产物，无需导入）"
      : (trAutoImport() ? "生成后直接进书库，导入完就能听" : "只导出文件，不进书库");
  }
}

async function trLoadCfg() {
  try {
    const d = await api("/api/translate/status");
    TR.cfg = d.cfg || null;
    if (d.cfg) {
      $("tr-enabled").checked = !!d.cfg.enabled;
      trRenderCfg(d.cfg);
    }
    if (!d.has_key) {
      $("tr-status").textContent = "⚠ 没有 API key：请把 DeepSeek key 写进 data/apikey.txt 再重启";
    }
    if (d.running) trStartPoll();
  } catch (e) { /* 忽略 */ }
}

function trRenderCfg(cfg) {
  const box = $("tr-cfg-list");
  box.innerHTML = "";
  const names = { 1: "90 分以下", 2: "90–105", 3: "105–120", 4: "120–135" };
  Object.keys(cfg.levels || {}).sort().forEach((k) => {
    const lv = cfg.levels[k];
    const num = trEl("input", { type: "number", min: "6", max: "60", value: String(lv.max_words) });
    num.dataset.lv = k;
    num.className = "cfg-words";
    const ta = trEl("textarea", {});
    ta.value = lv.vocab || "";
    ta.dataset.lv = k;
    ta.className = "cfg-vocab";
    box.appendChild(trEl("div", { class: "cfg-item" }, [
      trEl("div", { class: "cfg-head" }, [
        trEl("b", { text: `难度${k} · ${names[k]}` }),
        trEl("span", { class: "tiny", text: "单句词数" }), num,
      ]),
      ta,
    ]));
  });
  trUpdateWordsHint();
}

function trUpdateWordsHint() {
  const lv = $("tr-level").value;
  const c = TR.cfg && TR.cfg.levels && TR.cfg.levels[lv];
  $("tr-words").textContent = c ? `单句 ≤ ${c.max_words} 词` : "";
}

async function trSaveCfg() {
  const levels = {};
  document.querySelectorAll("#tr-cfg-list .cfg-words").forEach((n) => {
    const k = n.dataset.lv;
    levels[k] = levels[k] || {};
    levels[k].max_words = Number(n.value);
  });
  document.querySelectorAll("#tr-cfg-list .cfg-vocab").forEach((n) => {
    const k = n.dataset.lv;
    levels[k] = levels[k] || {};
    levels[k].vocab = n.value;
  });
  try {
    const d = await api("/api/translate/cfg", { enabled: $("tr-enabled").checked, levels });
    TR.cfg = d.cfg;
    trRenderCfg(d.cfg);
    toast("难度参数已保存");
  } catch (e) { toast("保存失败：" + e.message); }
}

async function trStart() {
  const st = S.state || {};
  const bid = st.current && st.current.id;
  if (!bid) { toast("先打开一本书"); return; }
  const outputs = trGetOutputs();
  if (!TR_OUT_KEYS.some((k) => outputs[k])) { toast("至少要勾一个产物"); return; }
  const body = { id: bid, level: Number($("tr-level").value), outputs,
                 auto_import: trAutoImport() };
  try {
    await api("/api/translate", body);
    trSaveOutputs();
    trSaveAutoImport();
    $("tr-files").innerHTML = "";
    $("tr-status").textContent = "已开始…";
    trStartPoll();
  } catch (e) {
    $("tr-status").textContent = "启动失败：" + e.message;
  }
}

function trStartPoll() {
  if (TR.timer) return;
  TR.running = true;
  $("tr-cancel").classList.remove("hidden");
  $("tr-start").disabled = true;
  TR.timer = setInterval(trPoll, 1000);
  trPoll();
}

async function trPoll() {
  let d;
  try { d = await api("/api/translate/status"); } catch (e) { return; }
  const pct = d.total ? Math.min(100, Math.round((d.done / d.total) * 100)) : 0;
  $("tr-bar-fill").style.width = pct + "%";
  let line = d.error ? `❌ ${d.error}` : (d.stage || "就绪");
  if (d.total) line += `　${d.done}/${d.total}（${pct}%）`;
  $("tr-status").textContent = line;

  if (!d.running) {
    clearInterval(TR.timer);
    TR.timer = null;
    TR.running = false;
    $("tr-cancel").classList.add("hidden");
    trUpdateOutHint();
    if (d.files && d.files.length) trRenderFiles(d.files);
    if (d.imported && d.imported.length) trRenderImported(d.imported);
    loadDocxs();                     // 新译出的 docx 进仓库，列表立刻刷新
  }
}

/* 翻译结束后：把「已导入书库」的结果补在文件清单下面 */
function trRenderImported(list) {
  const box = $("tr-files");
  let ok = false;
  list.forEach((it) => {
    const txt = it.error ? "⚠ 导入失败：" + it.error
      : (it.reused ? "📚 已刷新书库：" : "📚 已导入书库：") + it.title;
    if (!it.error) ok = true;
    box.appendChild(trEl("div", { class: "tiny", style: "opacity:.85" },
      [trEl("span", { text: txt })]));
  });
  if (ok) refresh().catch(() => {});      // 让左侧书库立刻出现新书
}

function trRenderFiles(files) {
  const box = $("tr-files");
  box.innerHTML = "";
  files.forEach((p) => {
    const name = p.split(/[\/]/).pop();
    box.appendChild(trEl("div", { class: "tiny" }, [
      trEl("a", { href: "#", text: "📄 " + name, onclick: (ev) => {
        ev.preventDefault();
        api("/api/open_file", { path: p }).catch((e) => toast("打不开：" + e.message));
      } }),
    ]));
  });
  const dir = files[0].replace(/[\/][^\/]+$/, "");
  box.appendChild(trEl("div", { class: "tiny", style: "opacity:.6;margin-top:4px" },
    [trEl("span", { text: "目录：" + dir })]));
}

async function trCancel() {
  try {
    await api("/api/translate/cancel", {});
    $("tr-status").textContent = "正在取消…";
  } catch (e) { toast("取消失败：" + e.message); }
}

$("btn-translate").addEventListener("click", trOpen);
$("tr-close").addEventListener("click", trClose);
$("tr-start").addEventListener("click", trStart);
$("tr-cancel").addEventListener("click", trCancel);
$("tr-cfg-save").addEventListener("click", trSaveCfg);
$("tr-level").addEventListener("change", trUpdateWordsHint);

/* docx 仓库 */
$("btn-docx-refresh").addEventListener("click", () => { loadDocxs(); toast("docx 仓库已刷新"); });
$("btn-docx-dir").addEventListener("click", () => {
  api("/api/docx/reveal", {}).catch((e) => toast("打不开目录：" + e.message));
});

/* 产物勾选：变了就存 + 刷新提示 */
TR_OUT_KEYS.forEach((k) => {
  $(TR_OUT_EL[k]).addEventListener("change", () => { trSaveOutputs(); trUpdateOutHint(); });
});
/* 一键：只要英文 / 只要中文 */
$("tr-only-en").addEventListener("click", () => {
  trSetOutputs({ bilingual: false, en_doc: true, mp3_en: true, mp3_zh: false });
  trSaveOutputs();
});
$("tr-only-zh").addEventListener("click", () => {
  trSetOutputs({ bilingual: false, en_doc: false, mp3_en: false, mp3_zh: true });
  trSaveOutputs();
});
/* 自动导入书库：勾选变化就存 + 刷新提示 */
$("tr-auto-import").addEventListener("change", () => {
  trSaveAutoImport();
  trUpdateOutHint();
});
/* 朗读语言切换 */
$("lang-zh").addEventListener("click", () => setLang("zh"));
$("lang-en").addEventListener("click", () => setLang("en"));

$("tr-modal").addEventListener("click", (ev) => { if (ev.target.id === "tr-modal") trClose(); });
