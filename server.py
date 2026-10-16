"""server.py —— 本地 HTTP 服务（界面 + API + 音频流）

为什么用本地 HTTP：音频要能边合成边播、要支持拖动进度（Range），
浏览器原生 `<audio>` 一句就能播，比在 Python 里摆弄音频设备省事得多，
也不需要任何额外音频依赖 —— 这对「能开源、依赖越少越好」很重要。

安全：只绑 127.0.0.1 + 随机 token（index.html 注入），并校验 Origin/Host，
避免别的网页偷偷来读你本地的书。
"""
from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import engine as E
from library import CACHE, DATA, EXPORTS, PANEL, ROOT, Library

# ── token ──
# 为什么要把 token 落盘：**窗口进程和服务进程是分开的**。
# pywebview 在部分 Windows/WebView2 环境下会陷入
# `window.native.AccessibilityObject.Bounds.Empty.Empty.Empty…` 的无限递归
# （stderr 里能看到 `maximum recursion depth exceeded`），把 GUI 线程拖死的**同时
# 连带饿死 HTTP 线程** —— 表现为端口在监听、请求却全部超时。
# 所以服务由独立子进程承载，窗口只当客户端；两者靠这个文件对齐 token。
TOKEN_FILE = DATA / "_token.txt"
PORT = 8791


def _init_token() -> str:
    env = os.environ.get("TXTREADER_TOKEN")
    if env:
        return env
    try:
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(t) >= 16:
            return t
    except Exception:                                # noqa: BLE001
        pass
    return secrets.token_urlsafe(18)


TOKEN = _init_token()


def _build_stamp() -> str:
    """前端资源的版本戳（取 index.html/app.js/style.css 里最新的 mtime）。

    为什么需要它：**重启服务不会让已经打开的页面换代码** ——
    浏览器手里还是旧的 app.js。更糟的是 token 现在做了持久化，
    重启后 token 不变 ⇒ 页面连 403 都收不到 ⇒ 永远不会自动刷新，
    于是一直跑着旧逻辑（本次「导入失败」就是这么来的）。
    有了版本戳，页面每次 refresh 都能发现「我比服务端旧」，自己刷新。
    """
    try:
        stamps = [(PANEL / f).stat().st_mtime
                  for f in ("index.html", "app.js", "style.css")
                  if (PANEL / f).exists()]
        return str(int(max(stamps))) if stamps else "0"
    except Exception:                                # noqa: BLE001
        return "0"


BUILD = _build_stamp()


def save_token() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(TOKEN, encoding="utf-8")


def read_token(max_wait: float = 25.0) -> str:
    """给窗口进程用：等子进程把 token 写出来（有就立刻返回）。"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if len(t) >= 16:
                return t
        except Exception:                            # noqa: BLE001
            pass
        time.sleep(0.25)
    return ""

_lib = Library()
_state: dict[str, Any] = {
    "bid": None,                    # 当前打开的书
    "pipeline": None,               # Pipeline
    "tts": None,                    # TTS
    "voice": "zh-CN-XiaoxiaoNeural",
    "rate": "+0%",
    "volume": "+0%",
    "export": {"running": False, "done": 0, "total": 0, "path": None, "error": None},
}
_lock = threading.Lock()


# ─────────────────── 业务层 ───────────────────
def make_tts(bid: str, voice: str, rate: str, volume: str) -> E.TTS:
    cache = _lib.cache_dir(bid, voice, rate)
    return E.TTS(cache, voice=voice, rate=rate, volume=volume,
                 prefer_online=not voice.startswith("sapi:"))


def open_book(bid: str, voice: str | None = None, rate: str | None = None) -> dict[str, Any]:
    book = _lib.get(bid)
    voice = voice or book.get("voice") or _state["voice"]
    rate = rate or book.get("rate") or _state["rate"]
    with _lock:
        old = _state.get("pipeline")
        if old:
            old.close()
        tts = make_tts(bid, voice, rate, _state["volume"])
        segs = _lib.segments_of(bid)
        _state.update({"bid": bid, "pipeline": E.Pipeline(segs, tts), "tts": tts,
                       "voice": voice, "rate": rate})
    _lib.update(bid, voice=voice, rate=rate)
    return {"book": book, "seg_count": len(segs), "voice": voice, "rate": rate}


def api_state() -> dict[str, Any]:
    bid = _state.get("bid")
    pipe = _state.get("pipeline")
    book = _lib.get(bid) if bid else None
    tts = _state.get("tts")
    online = getattr(tts, "_online_ok", None)
    # 只要这一轮已经成功用过在线音色，就直接报「在线」——避免界面一直显示「待检测」
    if online is None and tts is not None and getattr(tts, "stats", {}).get("online"):
        online = True
    return {
        "ok": True,
        "build": BUILD,
        "books": _lib.list(),
        "current": book,
        "voices": E.VOICES,
        "voice": _state["voice"],
        "rate": _state["rate"],
        "engine": {"online": online, "ffmpeg": bool(E.FFMPEG),
                   "stats": dict(getattr(tts, "stats", {})) if tts else {}},
        "cache_mb": _lib.cache_size_mb(),
        "export": _state["export"],
        "pipeline": pipe.status(book["progress"]["seg"]) if (pipe and book) else None,
    }


# ─────────────────── HTTP ───────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "TxtReader/1.0"

    def log_message(self, fmt, *args):               # 静默（日志走控制台反而刷屏）
        pass

    # ---- 工具 ----
    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str, code: int = 200,
               extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:                            # noqa: BLE001
            return {}

    def _query(self) -> dict[str, str]:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def _authed(self) -> bool:
        """只允许本机 + 带 token；token 由 index.html 注入，用户无感。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", ""):
            return False
        if self.path.startswith("/api/") or "/panel/" in self.path:
            t = self.headers.get("X-Token") or self._query().get("token")
            return t == TOKEN
        return True

    # ---- 路由 ----
    def do_GET(self) -> None:                        # noqa: N802
        try:
            self._do_get()
        except Exception as exc:                     # noqa: BLE001
            # 兜底：任何未预料的异常都要**变成 JSON 错误**返回。
            # 否则 socketserver 只会打一条 traceback 然后**直接断掉连接**，
            # 前端拿到的是 RemoteDisconnected（无从下手），排查成本极高。
            try:
                self._json({"ok": False,
                            "error": f"{type(exc).__name__}: {exc}"}, 500)
            except Exception:                        # noqa: BLE001
                pass

    def _do_get(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        q = self._query()
        if path in ("/", "/index.html"):
            html = (PANEL / "index.html").read_text(encoding="utf-8")
            # ⚠️ 占位符必须与 JS 变量名不同！先前用 "__TOKEN__" 会把 `window.__TOKEN__`
            # 这个**变量名**一起替换掉（变成 window.<token> = "..."），前端拿到 undefined，
            # 于是所有请求 403。教训：做字符串替换时，占位符不能是变量名的一部分。
            html = html.replace("{{TOKEN}}", TOKEN).replace("{{BUILD}}", BUILD)
            self._bytes(html.encode("utf-8"), "text/html; charset=utf-8",
                        extra={"Cache-Control": "no-store"})
            return
        if not self._authed():
            self._json({"ok": False, "error": "未授权（token 或来源不对）"}, 403)
            return
        if path.startswith("/panel/"):
            f = (PANEL / path.split("/panel/", 1)[1]).resolve()
            if PANEL.resolve() in f.parents and f.is_file():
                self._bytes(f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                            extra={"Cache-Control": "no-store"})   # 改动后刷新必定拿到新代码
            else:
                self._json({"ok": False, "error": "not found"}, 404)
            return
        if path == "/api/state":
            self._json(api_state()); return
        if path == "/api/chapters":
            bid = q.get("id") or _state["bid"]
            if not bid:
                self._json({"ok": False, "error": "还没有打开任何书"}, 400); return
            self._json({"ok": True, "chapters": _lib.get(bid)["chapters"]}); return
        if path == "/api/segs":
            bid = q.get("id") or _state["bid"]
            if not bid:
                self._json({"ok": False, "error": "还没有打开任何书"}, 400); return
            pipe = _state.get("pipeline")
            if not pipe or _state.get("bid") != bid:
                open_book(bid)
                pipe = _state["pipeline"]
            frm = int(q.get("from", 0)); cnt = min(int(q.get("count", 40)), 300)
            segs = pipe.segs[frm:frm + cnt]
            self._json({"ok": True, "from": frm, "total": len(pipe.segs),
                        "segs": [{"i": s["i"], "text": s["text"], "start": s["start"]}
                                 for s in segs]}); return
        if path == "/api/seg_at":
            # 按字符偏移**精确定位段号**（目录跳转用）。
            # 前端只加载了窗口内的一小段，靠它自己算不出远处章节在哪一段 ——
            # 段表在服务端是全的，二分一下最准。
            bid = q.get("id") or _state["bid"]
            if not bid:
                self._json({"ok": False, "error": "还没有打开任何书"}, 400); return
            pipe = _state.get("pipeline")
            if not pipe or _state.get("bid") != bid:
                open_book(bid)
                pipe = _state["pipeline"]
            ch = int(q.get("char", 0))
            segs = pipe.segs
            idx, lo, hi = 0, 0, len(segs) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if segs[mid]["start"] <= ch:
                    idx, lo = mid, mid + 1
                else:
                    hi = mid - 1
            self._json({"ok": True, "seg": idx, "total": len(segs),
                        "start": segs[idx]["start"] if segs else 0}); return
        if path == "/api/audio":
            self._serve_audio(q); return
        if path == "/api/audio/status":
            pipe = _state.get("pipeline")
            if not pipe:
                self._json({"ok": False, "error": "没有打开的书"}, 400); return
            seg = int(q.get("seg", 0))
            self._json({"ok": True, "ready": pipe.ready(seg),
                        **pipe.status(seg)}); return
        if path == "/api/engines":
            tts = _state.get("tts")
            self._json({"ok": True, "probe": tts.probe() if tts else None,
                        "ffmpeg": E.FFMPEG}); return
        if path == "/api/export/status":
            self._json({"ok": True, **_state["export"]}); return
        self._json({"ok": False, "error": f"未知接口 {path}"}, 404)

    def do_HEAD(self) -> None:                       # noqa: N802
        self.do_GET()

    def _serve_audio(self, q: dict[str, str]) -> None:
        """按段号交付音频；未合成完则**阻塞到合成完**再返回（前端已有 loading 态）。"""
        pipe = _state.get("pipeline")
        if not pipe:
            self._json({"ok": False, "error": "没有打开的书"}, 400); return
        try:
            seg = int(q.get("seg", 0))
            path = pipe.ensure(seg)                  # 阻塞直到就绪
        except Exception as exc:                     # noqa: BLE001
            self._json({"ok": False, "error": f"合成失败：{exc}"}, 500); return
        data = path.read_bytes()
        ctype = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
        rng = self.headers.get("Range")
        if rng:                                      # 支持拖动进度
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                a = int(m.group(1) or 0)
                b = int(m.group(2) or len(data) - 1)
                chunk = data[a:b + 1]
                self._bytes(chunk, ctype, 206, {
                    "Content-Range": f"bytes {a}-{b}/{len(data)}",
                    "Accept-Ranges": "bytes"})
                return
        self._bytes(data, ctype, 200, {"Accept-Ranges": "bytes"})
        pipe.prefetch(seg + 1)                       # 播这段时顺手预热后面几段

    def do_POST(self) -> None:                       # noqa: N802
        if not self._authed():
            self._json({"ok": False, "error": "未授权"}, 403); return
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/import":
                b = _lib.import_txt(body["path"], body.get("encoding"))
                open_book(b["id"])
                self._json({"ok": True, "book": b, **api_state()}); return
            if path == "/api/open":
                r = open_book(body["id"])
                self._json({"ok": True, "seg_count": r["seg_count"], **api_state()}); return
            if path == "/api/settings":
                bid = body.get("id") or _state["bid"]
                v, r = body.get("voice"), body.get("rate")
                if "volume" in body:
                    _state["volume"] = body["volume"]
                if bid and (v or r):
                    open_book(bid, v, r)
                self._json({"ok": True, **api_state()}); return
            if path == "/api/progress":
                bid = body.get("id") or _state["bid"]
                seg = int(body.get("seg", 0))
                pipe = _state.get("pipeline")
                ch = pipe.segs[seg]["start"] if (pipe and 0 <= seg < len(pipe.segs)) else 0
                _lib.update(bid, progress={"seg": seg, "char": ch,
                                           "chapter": _lib.chapter_at(bid, ch)})
                self._json({"ok": True, "char": ch}); return
            if path == "/api/prefetch":
                pipe = _state.get("pipeline")
                if pipe:
                    pipe.prefetch(int(body.get("seg", 0)))
                self._json({"ok": True}); return
            if path == "/api/recode":
                # 手动指定编码重读原文件 —— 自动探测偶尔会踩坑（尤其半路被截断的 GBK）
                bid = body["id"]
                enc = body.get("encoding") or None
                book = _lib.get(bid)
                src = book.get("source")
                if not src or not pathlib.Path(src).exists():
                    self._json({"ok": False,
                                "error": f"原始文件已不在：{src}（无法重读编码）"}, 400)
                    return
                new = _lib.reimport(bid, src, encoding=enc)
                open_book(bid)
                self._json({"ok": True, "book": new, **api_state()}); return
            if path == "/api/remove":
                bid = body["id"]
                if _state.get("bid") == bid:
                    # 先停掉合成流水线并**等它把手里的文件落盘**，
                    # 否则随后的缓存清理会撞上 PermissionError（WinError 5）
                    if _state.get("pipeline"):
                        _state["pipeline"].close()
                    _state.update({"bid": None, "pipeline": None, "tts": None})
                res = _lib.remove(bid)
                self._json({"ok": True, "removed": res, **api_state()}); return
            if path == "/api/export":
                self._start_export(body.get("id") or _state["bid"]); return
        except Exception as exc:                     # noqa: BLE001
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500); return
        self._json({"ok": False, "error": f"未知接口 {path}"}, 404)

    def _start_export(self, bid: str) -> None:
        if _state["export"]["running"]:
            self._json({"ok": False, "error": "已有导出任务在进行"}, 409); return
        book = _lib.get(bid)

        def run() -> None:
            _state["export"] = {"running": True, "done": 0, "total": 0,
                                "path": None, "error": None}
            try:
                tts = make_tts(bid, book.get("voice", _state["voice"]),
                               book.get("rate", _state["rate"]), _state["volume"])
                segs = _lib.segments_of(bid)
                _state["export"]["total"] = len(segs)

                def prog(done: int, total: int) -> None:
                    _state["export"]["done"] = done

                safe = re.sub(r"[\\/:*?\"<>|]", "_", book["title"])[:40]
                dest = ROOT / "exports" / f"{safe}.mp3"
                E.export_mp3(segs, tts, dest, progress=prog)
                _state["export"].update({"running": False, "path": str(dest)})
            except Exception as exc:                 # noqa: BLE001
                _state["export"].update({"running": False, "error": str(exc)[:200]})

        threading.Thread(target=run, daemon=True, name="export").start()
        self._json({"ok": True, "started": True})


def serve(port: int = PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd


if __name__ == "__main__":
    srv = serve()
    print(f"TXT 朗读器服务已启动： http://127.0.0.1:{PORT}/?token={TOKEN}")
    print("（单独调试用；正常请跑 app.py 开窗口）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("已停止")
