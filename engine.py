"""engine.py —— TXT 朗读器的语音引擎

两条嗓子：
  · **在线为主**：微软 Edge TTS（edge-tts 包，MIT）—— 神经音色，中文自然度接近真人
  · **离线兜底**：Windows SAPI5（本机已装 Microsoft Huihui Desktop）—— 断网时自动接班

设计要点（长文朗读的关键）：
  1. **按句切分**，再把短句合并成 ~180 字的「段」—— 段太大则首播等待久，太小则请求次数多
  2. **边合成边播 + 预取**：播放第 n 段时，后台已经在合成 n+1、n+2…… 长文才不卡
  3. **磁盘缓存**：每段音频按 (书, 音色, 语速, 段号) 落盘，重听与导出直接复用
  4. SAPI 产 WAV，统一用完整 ffmpeg 转成 mp3，前后端只处理一种格式
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

try:
    import edge_tts
except Exception:                                   # noqa: BLE001
    edge_tts = None

# ⚠️ ffmpeg 是**可选**的，不是依赖！
# 原因（开源约束）：imageio-ffmpeg 附带的是 gyan.dev essentials 构建 = GPL v3（含 x264/x265），
# 打进发行包会让整个包受 GPL 约束。而本应用其实不需要它：
#   · 在线：edge-tts 直接产出 mp3 → 浏览器原生能播
#   · 离线：SAPI 直接产出 wav   → 浏览器原生能播
#   · 导出：同码率 mp3 可以字节直接拼接（无需重编码）
# 所以：**有 ffmpeg 就用它做更规范的拼接/转码，没有也完全不影响使用**。
FFMPEG: str | None = None
try:
    _img = __import__("imageio_ffmpeg")
    FFMPEG = _img.get_ffmpeg_exe()
except Exception:                                   # noqa: BLE001
    import shutil as _sh
    FFMPEG = _sh.which("ffmpeg")                    # 系统 PATH 里有就用

# 本机实测可用的中文音色（edge-tts 7.3.1）
VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓（女·标准）", "gender": "女"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊（女·活泼）", "gender": "女"},
    {"id": "zh-CN-YunxiNeural", "name": "云希（男·年轻）", "gender": "男"},
    {"id": "zh-CN-YunjianNeural", "name": "云健（男·沉稳）", "gender": "男"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬（男·播报）", "gender": "男"},
    {"id": "zh-CN-YunxiaNeural", "name": "云夏（男·少年）", "gender": "男"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北（女·东北话）", "gender": "女"},
    {"id": "zh-CN-shaanxi-XiaoniNeural", "name": "晓妮（女·陕西话）", "gender": "女"},
    {"id": "sapi:Microsoft Huihui Desktop", "name": "慧慧（离线·系统）", "gender": "女"},
]

# 句子切分：中文句末标点 + 换行
SENT_END = re.compile(r"(?<=[。！？；…\.\!\?\;])")
SEG_TARGET = 180          # 每段目标字数


def split_segments(text: str, target: int = SEG_TARGET) -> list[dict[str, Any]]:
    """把正文切成段：先按句末标点断句，再把短句合并到 target 字左右。

    返回 [{"i":0, "text":"...", "start":字符偏移, "end":...}]，偏移用于「点击文字跳读」。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    segs: list[dict[str, Any]] = []
    buf: list[str] = []
    buf_len = 0
    buf_start = 0
    pos = 0

    def flush(end_pos: int) -> None:
        nonlocal buf, buf_len, buf_start
        s = "".join(buf).strip()
        if s:
            segs.append({"i": len(segs), "text": s, "start": buf_start, "end": end_pos})
        buf, buf_len = [], 0

    for para in text.split("\n"):
        if not para.strip():
            if buf:
                flush(pos)
            pos += len(para) + 1
            continue
        if not buf:
            buf_start = pos
        for piece in [p for p in SENT_END.split(para) if p]:
            if not buf:
                buf_start = pos
            buf.append(piece)
            buf_len += len(piece)
            pos += len(piece)
            if buf_len >= target:
                flush(pos)
        if buf:
            buf.append("\n")
            buf_len += 1
            pos += 1
    if buf:
        flush(pos)
    return segs


class TTSError(RuntimeError):
    pass


class TTS:
    """一段文本 → 一个音频文件。在线优先，离线兜底，全部落盘缓存。"""

    def __init__(self, cache_dir: pathlib.Path, voice: str = "zh-CN-XiaoxiaoNeural",
                 rate: str = "+0%", volume: str = "+0%", prefer_online: bool = True):
        self.cache_dir = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.prefer_online = prefer_online
        self._online_ok: bool | None = None
        self._lock = threading.Lock()
        self.stats = {"online": 0, "sapi": 0, "fail": 0}

    # ── 缓存键 ──
    def key_for(self, text: str) -> str:
        h = hashlib.sha1(f"{self.voice}|{self.rate}|{self.volume}|{text}".encode("utf-8"))
        return h.hexdigest()[:20]

    # ── 在线：Edge TTS ──
    async def _edge(self, text: str, out: pathlib.Path, voice: str, rate: str) -> None:
        c = edge_tts.Communicate(text, voice, rate=rate)
        await c.save(str(out))

    def _synth_edge(self, text: str, out: pathlib.Path, voice: str | None = None,
                    rate: str | None = None) -> None:
        if edge_tts is None:
            raise TTSError("edge-tts 未安装")
        asyncio.run(self._edge(text, out, voice or self.voice, rate or self.rate))

    # ── 离线：Windows SAPI（直接出 WAV，浏览器原生能播，无需 ffmpeg）──
    def _synth_sapi(self, text: str, out: pathlib.Path) -> pathlib.Path:
        wav = out.with_suffix(".wav")
        safe = text.replace("'", "''").replace("\n", " ")
        rate = 0
        m = re.match(r"([+-])(\d+)%", self.rate or "+0%")
        if m:
            rate = int(int(m.group(2)) / 10) * (1 if m.group(1) == "+" else -1)
        rate = max(-10, min(10, rate))
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$s.Rate={rate};"
            "$s.SetOutputToWaveFile('" + str(wav) + "');"
            f"$s.Speak('{safe}');$s.Dispose()"
        )
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not wav.exists() or wav.stat().st_size < 1024:
            raise TTSError(f"SAPI 失败：{(r.stderr or '').strip()[:120]}")
        return wav

    # ── 对外：合成一段（返回 mp3 或 wav 路径）──
    def synth(self, text: str, out_base: pathlib.Path | None = None) -> pathlib.Path:
        text = (text or "").strip()
        if not text:
            raise TTSError("空文本")
        key = self.key_for(text)
        base = out_base or (self.cache_dir / key)
        mp3, wav = base.with_suffix(".mp3"), base.with_suffix(".wav")
        for hit in (mp3, wav):                       # 命中缓存（两种格式都认）
            if hit.exists() and hit.stat().st_size > 1024:
                return hit

        if self.prefer_online and self._online_ok is not False:
            try:
                self._synth_edge(text, mp3)
                with self._lock:
                    self._online_ok = True
                    self.stats["online"] += 1
                return mp3
            except Exception as exc:                # noqa: BLE001
                with self._lock:
                    self._online_ok = False
                print(f"[TTS] 在线合成失败，转离线：{type(exc).__name__}: {str(exc)[:80]}")
        try:
            w = self._synth_sapi(text, base)
            with self._lock:
                self.stats["sapi"] += 1
            return w
        except Exception as exc:                    # noqa: BLE001
            with self._lock:
                self.stats["fail"] += 1
            raise TTSError(f"两条嗓子都失败：{exc}") from exc

    def probe(self) -> dict[str, Any]:
        """自检：在线通不通、离线在不在。"""
        res = {"online": False, "offline": False, "voices": len(VOICES)}
        if edge_tts is not None:
            try:
                self._synth_edge("测试", self.cache_dir / "_probe.mp3")
                res["online"] = True
                (self.cache_dir / "_probe.mp3").unlink(missing_ok=True)
            except Exception as exc:                # noqa: BLE001
                res["online_error"] = str(exc)[:120]
        try:
            self._synth_sapi("测试", self.cache_dir / "_probe_sapi.mp3")
            res["offline"] = True
            (self.cache_dir / "_probe_sapi.mp3").unlink(missing_ok=True)
        except Exception as exc:                    # noqa: BLE001
            res["offline_error"] = str(exc)[:120]
        return res


class Pipeline:
    """一本书的合成流水线：确保第 n 段可用 + 后台预取后几段。"""

    def __init__(self, segs: list[dict[str, Any]], tts: TTS, workers: int = 2,
                 prefetch: int = 3):
        self.segs = segs
        self.tts = tts
        self.prefetch_n = prefetch
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts")
        self._futures: dict[int, Any] = {}
        self._lock = threading.Lock()
        self.t0 = time.time()

    def path_of(self, i: int) -> pathlib.Path:
        """第 i 段的音频路径（可能尚未生成；优先 mp3，其次离线 wav）。"""
        base = self.tts.cache_dir / self.tts.key_for(self.segs[i]["text"])
        mp3, wav = base.with_suffix(".mp3"), base.with_suffix(".wav")
        if mp3.exists():
            return mp3
        if wav.exists():
            return wav
        return mp3                                  # 默认返回 mp3 路径（合成时若转离线会自动变 wav）

    def ready(self, i: int) -> bool:
        p = self.path_of(i)
        return p.exists() and p.stat().st_size > 1024

    def ensure(self, i: int, timeout: float = 120.0) -> pathlib.Path:
        """阻塞直到第 i 段音频就绪（已在缓存则立刻返回）。"""
        if i < 0 or i >= len(self.segs):
            raise TTSError(f"段号越界：{i}")
        if self.ready(i):
            return self.path_of(i)
        with self._lock:
            fut = self._futures.get(i)
            if fut is None:
                fut = self.pool.submit(self.tts.synth, self.segs[i]["text"])
                self._futures[i] = fut
        return fut.result(timeout=timeout)

    def prefetch(self, start: int) -> None:
        """从第 start 段起，往后预取 prefetch_n 段（不等结果）。"""
        for j in range(start, min(start + self.prefetch_n, len(self.segs))):
            if self.ready(j):
                continue
            with self._lock:
                if j in self._futures:
                    continue
                self._futures[j] = self.pool.submit(self.tts.synth, self.segs[j]["text"])

    def status(self, current: int) -> dict[str, Any]:
        ahead = sum(1 for j in range(current, min(current + self.prefetch_n, len(self.segs)))
                    if self.ready(j))
        done = sum(1 for j in range(len(self.segs)) if self.ready(j))
        return {"total": len(self.segs), "cached": done, "ahead_ready": ahead,
                "prefetch": self.prefetch_n, "stats": dict(self.tts.stats)}

    def close(self) -> None:
        """关掉合成线程池。

        `wait=True`：正在合成的那一段**等它落盘**再走 —— 否则它手里还攥着缓存文件，
        紧接着的「移除书籍」清理缓存就会撞上 `PermissionError`（Windows 尤其明显）。
        队列里没开始的用 `cancel_futures=True` 直接取消。
        """
        try:
            self.pool.shutdown(wait=True, cancel_futures=True)
        except Exception:                                # noqa: BLE001
            self.pool.shutdown(wait=False, cancel_futures=True)


def export_mp3(segs: list[dict[str, Any]], tts: TTS, dest: pathlib.Path,
               progress=None) -> pathlib.Path:
    """把整本书合成为一个音频文件。

    优先用 ffmpeg 做规范拼接；**没有 ffmpeg 时退化为 MP3 字节拼接** ——
    edge-tts 的输出是同码率（24kHz 单声道 48kbps）CBR MP3，帧边界可以字节直接相接，
    绝大多数播放器都能正常识别（这也是不打包 ffmpeg 的底气）。
    """
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts: list[pathlib.Path] = []
    for i, s in enumerate(segs):
        parts.append(tts.synth(s["text"]))
        if progress and (i % 5 == 0 or i == len(segs) - 1):
            progress(i + 1, len(segs))

    if FFMPEG:
        lst = dest.with_suffix(".concat.txt")
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-codec:a", "libmp3lame", "-b:a", "64k", str(dest)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        lst.unlink(missing_ok=True)
        if r.returncode == 0 and dest.exists():
            return dest

    # 兜底：字节拼接（要求全部是 mp3；含 wav 则无法拼接，直接报明原因）
    if all(p.suffix.lower() == ".mp3" for p in parts):
        with dest.open("wb") as fh:
            for p in parts:
                fh.write(p.read_bytes())
        return dest
    raise TTSError("离线音色产出的是 WAV，无法字节拼接；请装 ffmpeg 后再导出整本音频")


if __name__ == "__main__":
    # 自检：切分 + 两条嗓子
    demo = ("阶级斗争，一些阶级胜利了，一些阶级消灭了。这就是历史，这就是几千年的文明史。"
            "拿这个观点解释历史的就叫做历史的唯物主义，站在这个观点的反面的是历史的唯心主义。\n"
            "要抓意识形态领域里的阶级斗争。")
    segs = split_segments(demo, target=60)
    print(f"切分测试：{len(demo)} 字 → {len(segs)} 段")
    for s in segs:
        print("  [%d] %s" % (s["i"], s["text"][:40].replace("\n", " ")))
    tts = TTS(pathlib.Path(r"D:\deepseek工作区\txt-reader\_test\cache"))
    print("自检：", tts.probe())
