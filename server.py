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
import shutil
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import engine as E
import translate as TR
from library import CACHE, DATA, DOCXS, EXPORTS, PANEL, ROOT, Library

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
    "pipeline": None,               # Pipeline（中文/原文）
    "pipeline_en": None,            # Pipeline（中英对照书的英文那一半）
    "bilingual": False,             # 当前书是不是「英文行 + 中文行」的对照书
    "en_voice": "en-US-AvaNeural",
    "lang": "zh",                   # 朗读语言：zh / en
    "tts": None,                    # TTS
    "voice": "zh-CN-XiaoxiaoNeural",
    "rate": "+0%",
    "volume": "+0%",
    "export": {"running": False, "done": 0, "total": 0, "path": None, "error": None},
    "translate": {"running": False, "stage": "", "done": 0, "total": 0,
                  "level": 0, "files": [], "imported": [], "error": None},
}
TR_CFG_FILE = DATA / "translate_cfg.json"
TR_CANCEL = threading.Event()
# 四样产物可各勾各的：中英对照 / 纯英文 / 英文音频 / 中文音频。
# 前三样需要英文（要调 DeepSeek 翻译），中文音频不需要。
TR_OUT_KEYS = ("bilingual", "en_doc", "mp3_en", "mp3_zh")
_lock = threading.Lock()


def _norm_outputs(raw: Any) -> dict[str, bool]:
    """产物勾选。没传（老前端 / 老的 make_mp3 调用）= 四样全出；
    传了字典就按字典来——空字典意味着「一个都没勾」，由调用方报错。"""
    if not isinstance(raw, dict):
        return {k: True for k in TR_OUT_KEYS}
    return {k: bool(raw.get(k, False)) for k in TR_OUT_KEYS}


def _need_en(o: dict[str, bool]) -> bool:
    return bool(o["bilingual"] or o["en_doc"] or o["mp3_en"])


_LATIN = re.compile(r"[A-Za-z]")
_HAN = re.compile(r"[\u4e00-\u9fff]")


def _split_lang(text: str) -> tuple[str, str]:
    """把「英文行 + 中文行」的一段拆成 (中文, 英文)。

    中英对照书里每段都是英文在上、中文在下，按行看主体是拉丁字母还是汉字就能分。
    纯中文书的英文侧会是空的 —— 调用方据此判断「这本书能不能切英文朗读」。
    """
    zh: list[str] = []
    en: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        lat = len(_LATIN.findall(s))
        han = len(_HAN.findall(s))
        if lat >= 3 and lat > han * 2:
            en.append(s)
        elif han >= lat:
            zh.append(s)
        else:
            en.append(s)
    return "\n".join(zh), "\n".join(en)


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
        for key in ("pipeline", "pipeline_en"):
            old = _state.get(key)
            if old:
                old.close()
        tts = make_tts(bid, voice, rate, _state["volume"])
        segs = _lib.segments_of(bid)
        # 按行拆出「中文侧 / 英文侧」：中英对照书两样都有，普通中文书英文侧是空的。
        parts = [_split_lang(s["text"]) for s in segs]
        have_en = sum(1 for p in parts if p[1].strip())
        bilingual = bool(segs) and have_en >= max(1, len(segs) // 2)
        # 对照书：中文模式只念中文那一半（别把英文也拿中文音色念一遍）。
        zh_segs = segs if not bilingual else [
            {"i": s["i"], "text": (p[0] or s["text"]), "start": s["start"],
             "end": s["end"]} for s, p in zip(segs, parts)]
        _state.update({"bid": bid, "pipeline": E.Pipeline(zh_segs, tts), "tts": tts,
                       "voice": voice, "rate": rate,
                       "pipeline_en": None, "bilingual": bilingual, "lang": "zh"})
        if bilingual:
            # 英文侧留空表示「这段没英文」，播放时由 _serve_audio 退回原文。
            en_segs = [{"i": s["i"], "text": p[1], "start": s["start"],
                        "end": s["end"]} for s, p in zip(segs, parts)]
            tts_en = make_tts(bid, _state["en_voice"], rate, _state["volume"])
            _state["pipeline_en"] = E.Pipeline(en_segs, tts_en)
    _lib.update(bid, voice=voice, rate=rate)
    return {"book": book, "seg_count": len(segs), "voice": voice, "rate": rate,
            "bilingual": _state["bilingual"]}


def api_state() -> dict[str, Any]:
    bid = _state.get("bid")
    pipe = _state.get("pipeline")
    book = _lib.get(bid) if bid else None
    # 段数按**当前切法**重算：书里存的是导入时的旧数字，对照稿改成整段切以后会不符。
    # `segments_of` 有 mtime 缓存，轮询不心疼。
    def live_count(b: dict[str, Any]) -> int:
        try:
            return len(_lib.segments_of(b["id"]))
        except Exception:                              # noqa: BLE001
            return int(b.get("seg_count") or 0)

    books = [{**b, "seg_count": live_count(b)} for b in _lib.list()]
    if book:
        book = {**book, "seg_count": live_count(book)}
    tts = _state.get("tts")
    online = getattr(tts, "_online_ok", None)
    # 只要这一轮已经成功用过在线音色，就直接报「在线」——避免界面一直显示「待检测」
    if online is None and tts is not None and getattr(tts, "stats", {}).get("online"):
        online = True
    return {
        "ok": True,
        "build": BUILD,
        "books": books,
        "current": book,
        "voices": E.VOICES,
        "voice": _state["voice"],
        "rate": _state["rate"],
        "engine": {"online": online, "ffmpeg": bool(E.FFMPEG),
                   "stats": dict(getattr(tts, "stats", {})) if tts else {}},
        "cache_mb": _lib.cache_size_mb(),
        "bilingual": bool(book and _state.get("bilingual")),
        "lang": _state.get("lang", "zh"),
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
            if _state.get("bid") != bid:
                open_book(bid)
            # 显示用书库里的**完整**正文（中英对照书要留着英文行）；
            # 合成流水线里可能是「只中文/只英文」的拆分版，不能拿来显示。
            segs_all = _lib.segments_of(bid)
            frm = int(q.get("from", 0)); cnt = min(int(q.get("count", 40)), 300)
            segs = segs_all[frm:frm + cnt]
            self._json({"ok": True, "from": frm, "total": len(segs_all),
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
        if path == "/api/translate/status":
            self._json({"ok": True, **_state["translate"],
                        "cfg": _tr_cfg(), "has_key": bool(TR.load_key(DATA))}); return
        if path == "/api/docxs":
            self._json({"ok": True, "dir": str(DOCXS), "items": _docx_files()}); return
        if path == "/api/docx":
            fp = _docx_path(q.get("name") or "")
            if not fp:
                self._json({"ok": False, "error": "文件不存在"}, 404); return
            self._bytes(fp.read_bytes(),
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document",
                        extra={"Content-Disposition":
                               f"attachment; filename*=UTF-8''{urllib.parse.quote(fp.name)}"})
            return
        self._json({"ok": False, "error": f"未知接口 {path}"}, 404)

    def do_HEAD(self) -> None:                       # noqa: N802
        self.do_GET()

    def _serve_audio(self, q: dict[str, str]) -> None:
        """按段号交付音频；未合成完则**阻塞到合成完**再返回（前端已有 loading 态）。

        `lang=en` 时走中英对照书的英文流水线（英文音色）；那段没有英文行就退回原文。
        """
        lang = (q.get("lang") or _state.get("lang") or "zh").lower()
        pipe = _state.get("pipeline")
        en_pipe = _state.get("pipeline_en")
        if lang == "en" and en_pipe:
            pipe = en_pipe
        if not pipe:
            self._json({"ok": False, "error": "没有打开的书"}, 400); return
        try:
            seg = int(q.get("seg", 0))
            if pipe is en_pipe and 0 <= seg < len(pipe.segs) \
                    and not pipe.segs[seg]["text"].strip():
                pipe = _state["pipeline"]            # 这段没有英文行 ⇒ 退回原文
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
                pipe = _state.get("pipeline_en") \
                    if (body.get("lang") == "en" and _state.get("pipeline_en")) \
                    else _state.get("pipeline")
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
            if path == "/api/translate":
                self._start_translate(body.get("id") or _state["bid"],
                                      int(body.get("level", 2)),
                                      body.get("outputs"),
                                      body.get("auto_import")); return
            if path == "/api/translate/cancel":
                TR_CANCEL.set()
                self._json({"ok": True, "cancelling": True}); return
            if path == "/api/lang":
                lang = str(body.get("lang") or "zh").lower()
                _state["lang"] = "en" if lang == "en" else "zh"
                self._json({"ok": True, "lang": _state["lang"],
                            "bilingual": bool(_state.get("bilingual"))}); return
            if path == "/api/open_file":
                fp = pathlib.Path(str(body.get("path") or ""))
                if not fp.exists():
                    self._json({"ok": False, "error": f"文件不存在：{fp}"}, 400); return
                os.startfile(str(fp))                 # Windows：用默认程序打开
                self._json({"ok": True}); return
            if path == "/api/translate/cfg":
                _save_tr_cfg(body.get("cfg") or {})
                self._json({"ok": True, "cfg": _tr_cfg()}); return
            if path == "/api/docx/open":
                fp = _docx_path(str(body.get("name") or ""))
                if not fp:
                    self._json({"ok": False, "error": "文件不存在"}, 404); return
                os.startfile(str(fp))                 # 用 Word / WPS 打开
                self._json({"ok": True, "name": fp.name}); return
            if path == "/api/docx/remove":
                fp = _docx_path(str(body.get("name") or ""))
                if not fp:
                    self._json({"ok": False, "error": "文件不存在"}, 404); return
                fp.unlink()                           # 只删仓库副本，exports 里的原文件不动
                self._json({"ok": True, "items": _docx_files()}); return
            if path == "/api/docx/reveal":
                DOCXS.mkdir(parents=True, exist_ok=True)
                os.startfile(str(DOCXS))              # 在资源管理器里打开仓库目录
                self._json({"ok": True, "dir": str(DOCXS)}); return
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


# ─────────────────── 翻译成英文（DeepSeek） ───────────────────
def _tr_cfg() -> dict[str, Any]:
    """难度配置：默认取 translate.LEVELS，允许用户改「单句词数」和「用词说明」。"""
    cfg: dict[str, Any] = {"enabled": True, "auto_import": True, "levels": {},
                           "outputs": {k: True for k in TR_OUT_KEYS}}
    for lv, d in TR.LEVELS.items():
        cfg["levels"][str(lv)] = {"name": d["name"], "max_words": d["max_words"],
                                  "vocab": d["vocab"]}
    if TR_CFG_FILE.exists():
        try:
            saved = json.loads(TR_CFG_FILE.read_text(encoding="utf-8"))
            cfg["enabled"] = bool(saved.get("enabled", True))
            cfg["auto_import"] = bool(saved.get("auto_import", True))
            if isinstance(saved.get("outputs"), dict):
                for k in TR_OUT_KEYS:
                    if k in saved["outputs"]:
                        cfg["outputs"][k] = bool(saved["outputs"][k])
            for k, v in (saved.get("levels") or {}).items():
                if k in cfg["levels"] and isinstance(v, dict):
                    for kk in ("max_words", "vocab"):
                        if kk in v:
                            cfg["levels"][k][kk] = v[kk]
        except Exception:                             # noqa: BLE001
            pass
    return cfg


def _save_tr_cfg(cfg: dict[str, Any]) -> None:
    cur = _tr_cfg()
    if "enabled" in cfg:
        cur["enabled"] = bool(cfg["enabled"])
    if "auto_import" in cfg:
        cur["auto_import"] = bool(cfg["auto_import"])
    if isinstance(cfg.get("outputs"), dict):
        for k in TR_OUT_KEYS:
            if k in cfg["outputs"]:
                cur["outputs"][k] = bool(cfg["outputs"][k])
    for k, v in (cfg.get("levels") or {}).items():
        k = str(k)
        if k in cur["levels"] and isinstance(v, dict):
            if "max_words" in v:
                try:
                    cur["levels"][k]["max_words"] = max(6, min(60, int(v["max_words"])))
                except Exception:                     # noqa: BLE001
                    pass
            if "vocab" in v and str(v["vocab"]).strip():
                cur["levels"][k]["vocab"] = str(v["vocab"]).strip()[:400]
    DATA.mkdir(parents=True, exist_ok=True)
    TR_CFG_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    # 立刻生效，不用重启
    for k, v in cur["levels"].items():
        TR.LEVELS[int(k)]["max_words"] = int(v["max_words"])
        TR.LEVELS[int(k)]["vocab"] = str(v["vocab"])


def _start_translate(self, bid: str, level: int, outputs_raw: Any,
                     auto_import: Any = None) -> None:
    if _state["translate"]["running"]:
        self._json({"ok": False, "error": "已有翻译任务在进行"}, 409); return
    if not bid:
        self._json({"ok": False, "error": "没有打开的书"}, 400); return
    if not (1 <= level <= 4):
        self._json({"ok": False, "error": "难度只能是 1–4"}, 400); return
    outputs = _norm_outputs(outputs_raw)
    need_en = _need_en(outputs)
    if not any(outputs.values()):
        self._json({"ok": False, "error": "至少要勾一个产物"}, 400); return
    # 只出中文音频时既不用翻译、也不用 key / 功能开关。
    if need_en and not _tr_cfg()["enabled"]:
        self._json({"ok": False, "error": "翻译功能已在设置里关闭"}, 400); return
    if need_en and not TR.load_key(DATA):
        self._json({"ok": False, "error": "没有 DeepSeek API key：请写入 data/apikey.txt"}, 400); return
    if auto_import is None:                       # 没传就按设置里的默认
        auto_import = _tr_cfg()["auto_import"]
    auto_import = bool(auto_import) and (outputs["bilingual"] or outputs["en_doc"])
    threading.Thread(target=self._run_translate,
                     args=(bid, level, outputs, auto_import),
                     daemon=True, name="translate").start()
    self._json({"ok": True, "started": True, "outputs": outputs,
                "auto_import": auto_import})


def _import_into_lib(st: dict[str, Any], path: pathlib.Path, title: str,
                     voice: str | None) -> None:
    """把刚导出的 txt 收进书库：同一路径已导入过就原地刷新，不产生重复书。"""
    st["stage"] = "导入书库"
    try:
        target = path.resolve()
        for bid, b in list(_lib.data["books"].items()):
            src = b.get("source")
            if not src:
                continue
            try:
                if pathlib.Path(src).resolve() != target:
                    continue
            except OSError:
                continue
            book = _lib.reimport(bid, path)
            if voice:
                _lib.update(bid, voice=voice)
            st["imported"].append({"id": bid, "title": book["title"], "reused": True})
            return
        book = _lib.import_txt(path, title=title)
        if voice:
            _lib.update(book["id"], voice=voice)
        st["imported"].append({"id": book["id"], "title": book["title"],
                               "reused": False})
    except Exception as exc:                          # noqa: BLE001
        st["imported"].append({"error": f"{type(exc).__name__}: {exc}"[:200]})


# ── docx 仓库 ───────────────────────────────────────────────
# 为什么要单独一个仓库：译文 docx 本来就散在 `exports\{书名}_难度{n}\` 里，
# 想找「所有中英对照版」得逐层翻目录。这里集中复制一份到 `docx\`，
# 界面左侧能直接看到、点开就是 Word。
# 用**原文件名**当键：同书同难度重跑就覆盖，不堆副本；
# 仓库里的副本跟 exports 里的原文件互不影响（删仓库不动导出结果）。
def _store_docx(src: pathlib.Path) -> pathlib.Path:
    DOCXS.mkdir(parents=True, exist_ok=True)
    dest = DOCXS / src.name
    try:
        dst = dest.stat(); stt = src.stat()
        if dst.st_size == stt.st_size and dst.st_mtime >= stt.st_mtime:
            return dest                               # 已经是最新的，不重复拷
    except OSError:
        pass
    shutil.copy2(src, dest)
    return dest


def _docx_files() -> list[dict[str, Any]]:
    DOCXS.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for f in DOCXS.glob("*.docx"):
        # Word 打开文档时会在同目录建 `~$xxx.docx` 锁文件，那不是仓库内容；
        # 隐藏文件同样跳过。
        if f.name.startswith("~$") or f.name.startswith("."):
            continue
        try:
            stt = f.stat()
        except OSError:
            continue
        items.append({"name": f.name, "size": stt.st_size, "mtime": stt.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def _docx_path(name: str) -> pathlib.Path | None:
    """把前端给的文件名解析到仓库内的 .docx；越界/不存在一律 None。"""
    name = pathlib.Path(name or "").name              # 只取 basename，挡掉 ../ 之类
    if not name.lower().endswith(".docx"):
        return None
    try:
        p = (DOCXS / name).resolve()
        p.relative_to(DOCXS.resolve())                # 必须还在仓库里
    except (OSError, ValueError):
        return None
    return p if p.is_file() else None


def _backfill_docxs() -> None:
    """启动时把 exports 里已有的「中英对照版.docx」补进仓库 —— 老产物也能看到。"""
    try:
        for f in EXPORTS.glob("*/*_中英对照版.docx"):
            if f.name.startswith("~$"):
                continue                              # Word 锁文件，别当译文
            try:
                _store_docx(f)
            except OSError:
                continue
    except OSError:
        pass


def _run_translate(self, bid: str, level: int, outputs: dict[str, bool],
                   auto_import: bool = False) -> None:
    st = _state["translate"]
    TR_CANCEL.clear()
    st.update({"running": True, "stage": "准备", "done": 0, "total": 0,
               "level": level, "files": [], "imported": [], "error": None,
               "auto_import": auto_import})
    try:
        book = _lib.get(bid)
        need_en = _need_en(outputs)
        pairs: list[dict[str, Any]] = []

        # 1) 只有勾了英文产物才翻译；只出中文音频时完全跳过（不花钱、不耗时）。
        if need_en:
            segs = _lib.segments_of(bid)
            st["total"] = len(segs)
            st["stage"] = f"翻译中（难度{level}）"
            key = TR.load_key(DATA)

            def prog(done: int, total: int) -> None:
                st["done"] = done

            pairs = TR.translate_book(segs, level, key, on_progress=prog, cancel=TR_CANCEL,
                                      cache_file=DATA / "tr_cache" / f"{bid}_{level}.json")
            if TR_CANCEL.is_set():
                st.update({"running": False, "stage": "已取消"}); return

        safe = re.sub(r'[\/:*?"<>|]', "_", book["title"])[:40]
        # 中文音频跟难度无关：只出中文时不带「难度」，目录和文件名都干净些。
        base = f"{safe}_难度{level}" if need_en else safe
        outdir = EXPORTS / (f"{safe}_难度{level}" if need_en else f"{safe}_中文音频")
        outdir.mkdir(parents=True, exist_ok=True)
        files: list[str] = []

        # 2) 文本：中英对照 / 纯英文 各勾各的
        if outputs["bilingual"] or outputs["en_doc"]:
            st["stage"] = "写文本"
            bi, en_only = TR.pairs_text(book["title"], pairs, level)
            if outputs["bilingual"]:
                f1 = outdir / f"{base}_中英对照.txt"
                f1.write_text(bi, encoding="utf-8"); files.append(str(f1))
                if auto_import:
                    _import_into_lib(st, f1, f"{book['title']}（中英对照）", None)
            if outputs["en_doc"]:
                f2 = outdir / f"{base}_纯英文.txt"
                f2.write_text(en_only, encoding="utf-8"); files.append(str(f2))
                if auto_import:
                    _import_into_lib(st, f2, f"{book['title']}（英文）",
                                     "en-US-AvaNeural")

        # 3) docx：同上，各勾各的
        for want, bilingual, name in ((outputs["bilingual"], True, "中英对照版"),
                                      (outputs["en_doc"], False, "纯英文版")):
            if not want:
                continue
            st["stage"] = f"写 {name}"
            fp = outdir / f"{base}_{name}.docx"
            TR.write_docx(fp, book["title"], pairs, level, bilingual=bilingual)
            files.append(str(fp))
            if bilingual:                     # 英汉互译的 docx 另存一份进「docx 仓库」
                try:
                    _store_docx(fp)
                except OSError as exc:
                    st.setdefault("warn", []).append(f"存 docx 仓库失败：{exc}")

        # 4) 英文音频
        if outputs["mp3_en"]:
            st["stage"] = "合成英文音频"
            en_text = chr(10).join(p["en"] for p in pairs if p["en"])
            en_segs = E.split_segments(en_text, target=360)
            st["total"], st["done"] = len(en_segs), 0

            def prog_en(done: int, total: int) -> None:
                st["done"] = done

            tts_en = E.TTS(_lib.cache_dir(bid, "en-US-AvaNeural", "+0%"),
                           voice="en-US-AvaNeural", rate="+0%", volume=_state["volume"])
            fp = outdir / f"{base}_英文.mp3"
            E.export_mp3(en_segs, tts_en, fp, progress=prog_en)
            files.append(str(fp))

        # 5) 中文音频
        if outputs["mp3_zh"]:
            st["stage"] = "合成中文音频"
            zh_segs = _lib.segments_of(bid)
            st["total"], st["done"] = len(zh_segs), 0

            def prog_zh(done: int, total: int) -> None:
                st["done"] = done

            tts_zh = make_tts(bid, book.get("voice", _state["voice"]),
                              book.get("rate", _state["rate"]), _state["volume"])
            fp = outdir / f"{base}_中文.mp3"
            E.export_mp3(zh_segs, tts_zh, fp, progress=prog_zh)
            files.append(str(fp))

        st.update({"running": False, "stage": "完成", "files": files,
                   "done": st["total"]})
    except TR.TranslateError as exc:
        st.update({"running": False, "stage": "失败", "error": str(exc)[:300]})
    except Exception as exc:                          # noqa: BLE001
        st.update({"running": False, "stage": "失败",
                   "error": f"{type(exc).__name__}: {exc}"[:300]})


Handler._start_translate = _start_translate
Handler._run_translate = _run_translate


def serve(port: int = PORT) -> ThreadingHTTPServer:
    _backfill_docxs()                                 # 老产物补进 docx 仓库
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
