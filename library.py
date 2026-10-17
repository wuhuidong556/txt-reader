"""library.py —— TXT 书库：导入、编码识别、章节切分、进度落盘

中文 TXT 的三大坑，这里全堵上：
  1. **编码**：UTF-8 / GBK / UTF-16 三分天下，读错就是一屏问号 —— 用 charset-normalizer
     自动探测，并保留「手动指定编码重读」的入口
  2. **章节切分**：中文章节标题花样多（第一章 / 第 1 章 / 卷一 / 序 / 后记 / Chapter 1），
     用一组正则兜住，切不出来的就按段落等分兜底
  3. **进度**：按「段号 + 字符偏移」双记录 —— 段号用于朗读续读，字符偏移用于点击跳读
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
import time
import uuid
from typing import Any

from charset_normalizer import from_path

try:
    from engine import SEG_TARGET, split_segments
except Exception:                                    # noqa: BLE001
    split_segments = None                            # type: ignore[assignment]
    SEG_TARGET = 180                                 # type: ignore[assignment]

# 数据放哪：源码运行时放项目目录；**打包成 exe 后放 exe 旁边**
# —— 否则数据会落在 `_internal/` 里，用户既看不到也不好备份。
if getattr(sys, "frozen", False):
    ROOT = pathlib.Path(sys.executable).resolve().parent
    PANEL = pathlib.Path(getattr(sys, "_MEIPASS", ROOT)) / "panel"   # 前端资源在解包目录
else:
    ROOT = pathlib.Path(__file__).resolve().parent
    PANEL = ROOT / "panel"

BOOKS = ROOT / "books"
DATA = ROOT / "data"
CACHE = ROOT / "cache"
EXPORTS = ROOT / "exports"
DOCXS = ROOT / "docx"          # 译文的 docx 仓库（中英对照版存这儿，便于集中取用）
LIB_FILE = DATA / "library.json"

# 章节标题：第一章 / 第 1 章 / 第一百二十三章 / 卷三 / 序章 / 楔子 / 后记 / Chapter 1
CHAP_PATTERNS = [
    re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百千零〇两]+\s*[章回节篇卷部]\b.{0,40}$"),
    re.compile(r"^\s*(序章|序言|序|楔子|引子|前言|后记|尾声|终章|番外)\s*.{0,40}$"),
    re.compile(r"^\s*(Chapter|CHAPTER|Part|PART)\s*[0-9IVXLC]+.{0,40}$"),
    re.compile(r"^\s*[0-9]{1,4}\s*[、.．]\s*\S.{0,40}$"),
]


def _is_latin(s: str) -> bool:
    letters = sum(1 for c in s if c < "\x80" and c.isalpha())
    han = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return letters >= 3 and letters > han * 2


def _is_han(s: str) -> bool:
    letters = sum(1 for c in s if c < "\x80" and c.isalpha())
    han = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return han >= 3 and han >= letters


def _pair_layout(text: str) -> bool:
    """判断是不是「英文一行 + 中文一行」的中英对照稿。

    为什么要判断：对照稿必须**按整段**切。按 180 字硬切会把一组的
    英文和中文拆进不同的段，读者看到的就是「有的句子没翻译」——
    其实是它的英文被切到上一段去了。

    只看开头 8000 字、最多 20 个空行块：块内既有拉丁为主的行走、也有汉字为主的
    行走，且这种块占比够高，就认定是对照稿。
    """
    blocks = [b for b in re.split(r"\n\s*\n", text[:8000]) if b.strip()][:20]
    if len(blocks) < 3:
        return False
    hit = 0
    for b in blocks:
        lines = [ln.strip() for ln in b.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        if any(_is_latin(ln) for ln in lines) and any(_is_han(ln) for ln in lines):
            hit += 1
    return hit >= max(3, int(len(blocks) * 0.6))


def _split_for(text: str) -> list[dict[str, Any]]:
    """按正文形态选切段方式：对照稿按整段切，普通书按 180 字左右切。"""
    if split_segments is None:
        return []
    return split_segments(text, 10 ** 9 if _pair_layout(text) else SEG_TARGET)


def detect_encoding(path: pathlib.Path) -> tuple[str, float]:
    """返回 (编码, 置信度)。探测不出来时按 UTF-8 处理。"""
    try:
        best = from_path(str(path)).best()
        if best:
            return best.encoding or "utf-8", 1.0
    except Exception:                                # noqa: BLE001
        pass
    return "utf-8", 0.0


def read_text(path: pathlib.Path, encoding: str | None = None,
              force: bool = False) -> tuple[str, str, int]:
    """读文本。返回 **(正文, 实际用的编码, 替换字符数)**。

    · `encoding` 为 None/""/"auto" → 自动探测，且允许回退（探测偶尔会踩坑，回退能救回来）
    · `encoding` 指定且 `force=True` → **严格按它读，绝不回退**
      （用户手动选编码时必须尊重他的选择，哪怕结果是一屏乱码 —— 那才是他要看的真相。
       先前"指定了却偷偷回退"的写法会让界面显示「已按 X 重读」而实际按 Y 读，属误导。）
    """
    path = pathlib.Path(path)
    if encoding in (None, "", "auto"):
        enc, _ = detect_encoding(path)
        force = False
    else:
        enc = encoding

    if force:
        text = path.read_bytes().decode(enc, errors="replace")
        return text, enc, text.count("\ufffd")

    for try_enc in [enc, "utf-8", "gb18030", "utf-16", "big5"]:
        try:
            return path.read_text(encoding=try_enc), try_enc, 0
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="ignore"), "utf-8(ignore)", 0


def split_chapters(text: str, max_lines: int = 3) -> list[dict[str, Any]]:
    """按章节标题切分；标题太少的（<2 个）就整本当一章。"""
    lines = text.split("\n")
    marks: list[tuple[int, str, int]] = []           # (行号, 标题, 字符偏移)
    pos = 0
    for ln, line in enumerate(lines):
        s = line.strip()
        if 0 < len(s) <= 48 and any(p.match(line) for p in CHAP_PATTERNS):
            marks.append((ln, s, pos))
        pos += len(line) + 1

    if len(marks) < 2:
        return [{"title": "全文", "start": 0, "end": len(text),
                 "lines": [0, len(lines)]}]

    # 首个标题之前的正文（前言/序）单独成章
    chapters: list[dict[str, Any]] = []
    if marks[0][2] > 0:
        chapters.append({"title": "（卷首）", "start": 0, "end": marks[0][2],
                         "lines": [0, marks[0][0]]})
    for i, (ln, title, start) in enumerate(marks):
        end_line = marks[i + 1][0] if i + 1 < len(marks) else len(lines)
        end = marks[i + 1][2] if i + 1 < len(marks) else len(text)
        chapters.append({"title": title, "start": start, "end": end,
                         "lines": [ln, end_line]})
    return chapters


class Library:
    """书库与阅读进度的落盘（原子写，坏不了）。"""

    def __init__(self) -> None:
        for d in (BOOKS, DATA, CACHE, EXPORTS, DOCXS):
            d.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {"books": {}, "order": []}
        self._seg_cache: dict[str, tuple[int, list[dict[str, Any]]]] = {}
        self._load()

    # ── 落盘 ──
    def _load(self) -> None:
        try:
            self.data = json.loads(LIB_FILE.read_text(encoding="utf-8"))
        except Exception:                            # noqa: BLE001
            self.data = {"books": {}, "order": []}
        self.data.setdefault("books", {})
        self.data.setdefault("order", [])

    def save(self) -> None:
        tmp = LIB_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(LIB_FILE)

    # ── 导入 ──
    def import_txt(self, src: str | pathlib.Path, encoding: str | None = None,
                   title: str | None = None) -> dict[str, Any]:
        src = pathlib.Path(src)
        if not src.exists():
            raise FileNotFoundError(f"找不到文件：{src}")
        text, enc = read_text(src, encoding)[:2]
        if not text.strip():
            raise ValueError("文件是空的（或编码识别失败导致读不出内容）")
        bid = "b_" + uuid.uuid4().hex[:10]
        (BOOKS / f"{bid}.txt").write_text(text, encoding="utf-8")
        chapters = split_chapters(text)
        segs = _split_for(text)
        book = {
            "id": bid,
            "title": title or src.stem,
            "source": str(src),
            "encoding": enc,
            "chars": len(text),
            "lines": text.count("\n") + 1,
            "chapters": [{"title": c["title"], "start": c["start"], "end": c["end"]}
                         for c in chapters],
            "seg_count": len(segs),
            "added": time.strftime("%Y-%m-%d %H:%M"),
            "progress": {"seg": 0, "char": 0, "chapter": 0,
                         "updated": time.strftime("%Y-%m-%d %H:%M")},
            "voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
        }
        self.data["books"][bid] = book
        self.data["order"].insert(0, bid)
        self.save()
        return book

    def reimport(self, bid: str, src: str | pathlib.Path,
                 encoding: str | None = None) -> dict[str, Any]:
        """用指定编码重读同一个文件，原地更新这本书（保留阅读进度）。

        手动指定编码时 **force=True**：严格按用户选的编码读，不回退 ——
        读出一屏乱码也是他要知道的结果，比"悄悄换成别的编码"诚实。
        返回值里额外带 `requested` / `bad_chars`，供界面如实提示。
        """
        src = pathlib.Path(src)
        if not src.exists():
            raise FileNotFoundError(f"找不到原文件：{src}")
        auto = encoding in (None, "", "auto")
        text, enc, bad = read_text(src, encoding, force=not auto)
        if not text.strip():
            raise ValueError("按这个编码读出来是空的 —— 换一个编码再试")
        book = self.get(bid)
        (BOOKS / f"{bid}.txt").write_text(text, encoding="utf-8")
        chapters = split_chapters(text)
        segs = _split_for(text)
        book.update({
            "encoding": enc,
            "requested": "auto" if auto else encoding,
            "bad_chars": bad,
            "chars": len(text),
            "lines": text.count("\n") + 1,
            "chapters": [{"title": c["title"], "start": c["start"], "end": c["end"]}
                         for c in chapters],
            "seg_count": len(segs),
        })
        # 换编码后正文变了，旧音频缓存（按文本哈希命名）自然失效，无需清理
        self.save()
        return book

    # ── 查询 ──
    def list(self) -> list[dict[str, Any]]:
        out = []
        for bid in self.data["order"]:
            b = self.data["books"].get(bid)
            if b:
                out.append({k: b[k] for k in ("id", "title", "chars", "seg_count",
                                              "encoding", "added", "progress")})
        return out

    def get(self, bid: str) -> dict[str, Any]:
        b = self.data["books"].get(bid)
        if not b:
            raise KeyError(f"没有这本书：{bid}")
        return b

    def text_of(self, bid: str) -> str:
        return (BOOKS / f"{bid}.txt").read_text(encoding="utf-8")

    def segments_of(self, bid: str) -> list[dict[str, Any]]:
        """切段结果按正文 mtime 缓存。

        为什么要缓存：`/api/state` 被前端每几秒轮询一次，而界面要显示「共 N 段」。
        每次轮询都把几十万字重新切一遍太浪费（实测对照稿 27 万字要 200ms+）。
        """
        p = BOOKS / f"{bid}.txt"
        try:
            mt = p.stat().st_mtime_ns
        except OSError:
            return []
        hit = self._seg_cache.get(bid)
        if hit and hit[0] == mt:
            return list(hit[1])
        segs = _split_for(p.read_text(encoding="utf-8"))
        self._seg_cache[bid] = (mt, segs)
        return list(segs)

    def chapter_at(self, bid: str, char_pos: int) -> int:
        for i, c in enumerate(self.get(bid)["chapters"]):
            if c["start"] <= char_pos < c["end"]:
                return i
        return 0

    # ── 更新 ──
    def update(self, bid: str, **fields: Any) -> dict[str, Any]:
        b = self.get(bid)
        if "progress" in fields and isinstance(fields["progress"], dict):
            b["progress"].update(fields.pop("progress"))
            b["progress"]["updated"] = time.strftime("%Y-%m-%d %H:%M")
        b.update(fields)
        self.save()
        return b

    def remove(self, bid: str) -> dict[str, Any]:
        """移除一本书。

        **先落盘、再清缓存** —— 缓存清理失败（Windows 上文件被占用很常见）绝不能让
        「移除」这件事本身失效。早先顺序反了：`CACHE.glob()` 匹配到的是缓存**目录**，
        对目录调 `unlink()` 直接抛 `PermissionError: [WinError 5] 拒绝访问`，
        而异常发生在 `save()` 之前 ⇒ 书还在库里，用户以为按钮坏了。
        """
        self.data["books"].pop(bid, None)
        if bid in self.data["order"]:
            self.data["order"].remove(bid)
        self.save()                                   # ← 先把移除落盘，这一步必须成功

        result = {"removed": bid, "cache_cleared": 0, "skipped": []}
        (BOOKS / f"{bid}.txt").unlink(missing_ok=True)

        for p in CACHE.glob(f"{bid}*"):
            try:
                if p.is_dir():
                    shutil.rmtree(p)                  # 缓存目录形如 <bid>_<音色>_<语速>/
                else:
                    p.unlink(missing_ok=True)
                result["cache_cleared"] += 1
            except Exception as exc:                  # noqa: BLE001
                # 被占用的（还在合成/播放）就留着 —— 下次启动或手动删 cache/ 都能清掉
                result["skipped"].append(f"{p.name}: {type(exc).__name__}")
        if result["skipped"]:
            print(f"[书库] 《{bid}》已移除，但有 {len(result['skipped'])} 项缓存被占用未清：",
                  result["skipped"][:3])
        return result

    def cache_dir(self, bid: str, voice: str, rate: str) -> pathlib.Path:
        safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{bid}_{voice}_{rate}")
        d = CACHE / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cache_size_mb(self) -> float:
        total = sum(p.stat().st_size for p in CACHE.rglob("*.mp3"))
        return round(total / 1048576, 1)


if __name__ == "__main__":
    lib = Library()
    print("书库：", json.dumps(lib.list(), ensure_ascii=False, indent=2)[:600])
    print("缓存占用：%.1f MB" % lib.cache_size_mb())
