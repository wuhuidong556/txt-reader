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
  const startSeg = (b.progress && b.progress.seg) || 0;
  const from = Math.max(0, startSeg - 5);
  await loadSegs(from);
  S.cur = startSeg;
  highlight(true);
  api("/api/prefetch", { seg: startSeg }).catch(() => {});
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

  audio.src = "/api/audio?seg=" + i + "&token=" + encodeURIComponent(T);
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

  api("/api/prefetch", { seg: i + 1 }).catch(() => {});
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
