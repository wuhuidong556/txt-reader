"""translate.py —— 把中文正文改写成「高考难度」英文

思路：不逐字直译，而是**按分数段重写**——同一段中文，给 90 分以下的学生要短句简单词，
给 120–135 分的学生可以上从句和四级词。难度靠 ①提示词 ②真实词表样例 ③句长上限 三条控。

产出（由 server.py 调用后落盘）：
  · 中英对照 txt —— 一行英文、一行中文
  · 纯英文 txt
  · docx（对照版 / 纯英文版）

只用标准库 urllib 发请求，不引入 requests，保持开源包轻量。
"""
from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import pathlib
import re
import threading
import time
import urllib.error
import urllib.request

API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-flash"

# 难度档：句子长度上限、允许的词汇层级、给人看的说明
LEVELS: dict[int, dict] = {
    1: {"name": "90 分以下", "score": "<90", "max_words": 12,
        "vocab": "只用最基础的初中词汇（如 make, help, because, important, together），"
                 "避免四级以上词汇和复杂从句；多用简单句、并列句。",
        "band_file": "band1"},
    2: {"name": "90–105 分", "score": "90–105", "max_words": 16,
        "vocab": "用高考核心词汇（如 growth, remind, climate, pattern, valuable），"
                 "允许简单的定语从句、宾语从句。",
        "band_file": "band2"},
    3: {"name": "105–120 分", "score": "105–120", "max_words": 22,
        "vocab": "用高考词汇加部分四级词（如 significant, resource, emotional, release），"
                 "允许状语从句、非谓语结构。",
        "band_file": "band3"},
    4: {"name": "120–135 分", "score": "120–135", "max_words": 30,
        "vocab": "可用四级、六级词汇（如 environmental, participant, immune, undermine），"
                 "允许复杂从句、倒装、插入语，但仍须自然地道。",
        "band_file": "band4"},
}

VOCAB_DIR = pathlib.Path(r"D:\deepseek工作区\gaokao-vocab\vocab")


# ─────────────────── 密钥 ───────────────────
def load_key(data_dir: pathlib.Path) -> str:
    p = data_dir / "apikey.txt"
    if p.exists():
        k = p.read_text(encoding="utf-8").strip()
        if k:
            return k
    import os
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


# ─────────────────── 词表样例 ───────────────────
_sample_cache: dict[int, str] = {}


def vocab_sample(level: int, n: int = 40) -> str:
    """从真词表里抽 n 个词当「用词示范」，比空泛描述有效得多。"""
    if level in _sample_cache:
        return _sample_cache[level]
    f = VOCAB_DIR / f"{LEVELS[level]['band_file']}.json"
    out = ""
    if f.exists():
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
            step = max(1, len(rows) // n)
            words = [r["word"] for r in rows[::step][:n]]
            out = "、".join(words)
        except Exception:
            out = ""
    _sample_cache[level] = out
    return out


# ─────────────────── 单段改写 ───────────────────
SYSTEM_TMPL = """你是一位资深高中英语教师，正在把一篇中文文章改写成英文，\
供高考英语 {name}（约 {score} 分）水平的学生阅读。

【用词】
{vocab}
本档词表的常用词示例（**优先使用这一类词**）：{samples}

【句子】
- 单句不超过 {max_words} 个单词
- 避免生僻词、罕见习语、晦涩的长从句
- 必须自然地道，读起来像人写的，不要中式英语

【忠实】
- 忠实原意，不增、不删、不评论、不做文学改写
- 人名地名照原文音译成英文

【格式】
- 下面给你的是一段中文，你只输出对应的一段英文
- **只输出英文正文**：不要编号、不要标题、不要括号注释、不要 Markdown、不要任何解释"""


class TranslateError(RuntimeError):
    pass


def _post(payload: dict, key: str, timeout: float = 180.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise TranslateError(f"HTTP {e.code}：{detail}") from None
    except urllib.error.URLError as e:
        raise TranslateError(f"连不上 DeepSeek：{e.reason}") from None


def translate_one(text: str, level: int, key: str, timeout: float = 180.0,
                  retries: int = 3) -> str:
    """把一段中文改写成英文。失败重试；重试仍失败抛 TranslateError。"""
    lv = LEVELS[level]
    system = SYSTEM_TMPL.format(
        name=lv["name"], score=lv["score"], vocab=lv["vocab"],
        samples=vocab_sample(level) or "（无）", max_words=lv["max_words"])
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": text}],
        # 这个模型默认带思维链：一段 435 字的中文能烧掉 1.1 万 reasoning token，
        # 512/1024/2048 的 max_tokens 全被吃光 ⇒ content 为空、finish_reason=length。
        # 显式关掉思考：实测 15.9s → 2.3s，completion_tokens 3587 → 288。
        "max_tokens": 4096,           # 关掉思考后仍留足余量，防个别长段落截断
        "temperature": 0.3,
        "thinking": {"type": "disabled"},
    }
    last = None
    for attempt in range(retries):
        try:
            d = _post(payload, key, timeout)
            if "error" in d:
                raise TranslateError(str(d["error"])[:300])
            msg = d["choices"][0]["message"]
            out = (msg.get("content") or "").strip()
            if not out:
                raise TranslateError("模型返回空内容（可能 token 用尽）")
            return _clean(out)
        except TranslateError as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
        except Exception as e:                     # noqa: BLE001
            last = TranslateError(str(e)[:300])
            time.sleep(1.5 * (attempt + 1))
    raise last or TranslateError("未知错误")


_MD = re.compile(r"^(#{1,6}\s+|[-*]\s+|\d+[.、)]\s*|```)")


def _clean(text: str) -> str:
    """去掉模型偶尔多嘴加的 Markdown 标记、引号包裹、『译文：』前缀。"""
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            if lines and lines[-1]:
                lines.append("")
            continue
        ln = _MD.sub("", ln)
        lines.append(ln)
    out = "\n".join(lines).strip()
    out = re.sub(r"^(译文|英文|English)\s*[:：]\s*", "", out, flags=re.I)
    if len(out) >= 2 and out[0] in "“\"'" and out[-1] in "”\"'":
        out = out[1:-1].strip()
    return out


# ─────────────────── 整本 ───────────────────
def _seg_key(zh: str) -> str:
    return hashlib.sha1(zh.encode("utf-8")).hexdigest()[:20]


def translate_book(segs: list[dict], level: int, key: str,
                   workers: int = 4,
                   on_progress=None,
                   cancel: threading.Event | None = None,
                   cache_file: pathlib.Path | None = None) -> list[dict]:
    """把整本书逐段改写成英文。返回 [{i, zh, en}]，失败的段 en 为空字符串。

    cache_file 给了就启用「译文落盘缓存」（按段文本哈希存）：
    同一本书同一难度重跑时直接复用旧译文 —— 既省钱，也让下游的 TTS 音频缓存能全部命中
    （TTS 缓存 key 是文本，译文一变就全废）。
    """
    todo = [(i, s["text"].strip()) for i, s in enumerate(segs) if s.get("text", "").strip()]
    results: dict[int, str] = {}
    errors: dict[int, str] = {}
    done = 0
    lock = threading.Lock()

    # ── 读缓存 ──
    cached: dict[str, str] = {}
    if cache_file is not None:
        try:
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8")).get("entries") or {}
        except Exception:                            # noqa: BLE001
            cached = {}
    hits = sum(1 for _, zh in todo if _seg_key(zh) in cached)
    if hits:
        print(f"[translate] 缓存命中 {hits}/{len(todo)} 段")

    def flush_cache() -> None:
        if cache_file is None:
            return
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(
                {"level": level, "entries": cached}, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:                     # noqa: BLE001
            print(f"[translate] 写缓存失败：{exc}")

    def work(idx: int, zh: str):
        nonlocal done
        if cancel is not None and cancel.is_set():
            return
        k = _seg_key(zh)
        if k in cached:                              # 命中，不再花钱
            with lock:
                results[idx] = cached[k]
                done += 1
            if on_progress:
                on_progress(done, len(todo))
            return
        try:
            en = translate_one(zh, level, key)
        except TranslateError as e:
            with lock:
                errors[idx] = str(e)
                done += 1
            return
        with lock:
            results[idx] = en
            cached[k] = en
            done += 1
        if on_progress:
            on_progress(done, len(todo))

    with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(lambda t: work(*t), todo))
    flush_cache()

    out = []
    for i, zh in todo:
        out.append({"i": i, "zh": zh, "en": results.get(i, "")})
    if errors:
        first = next(iter(errors.values()))
        raise TranslateError(f"{len(errors)} 段失败，首条原因：{first}")
    return out


# ─────────────────── 落盘 ───────────────────
def pairs_text(book_title: str, pairs: list[dict], level: int) -> tuple[str, str]:
    """返回 (中英对照文本, 纯英文文本)。一行英文、一行中文。"""
    lv = LEVELS[level]
    head = f"# {book_title} · 高考英语难度{level}（{lv['name']}）"
    sub = f"# 原文逐段改写：英文在上，中文在下；共 {len(pairs)} 段"
    bi = [head, sub, ""]
    en_only = [head, f"# 纯英文版；共 {len(pairs)} 段", ""]
    for k, p in enumerate(pairs, 1):
        bi.append(p["en"])
        bi.append(p["zh"])
        bi.append("")
        en_only.append(f"{p['en']}")
        en_only.append("")
    return "\n".join(bi).rstrip() + "\n", "\n".join(en_only).rstrip() + "\n"


def write_docx(path: pathlib.Path, book_title: str, pairs: list[dict], level: int,
               bilingual: bool = True) -> pathlib.Path:
    """生成 docx。bilingual=True 出中英对照版，False 出纯英文版。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    def font(run, size=11, bold=False, color=None):
        run.font.name = "Times New Roman"
        run.font.size = Pt(size)
        run.font.bold = bold
        if color:
            run.font.color.rgb = color
        rPr = run._element.get_or_add_rPr()
        rf = rPr.find(qn("w:rFonts"))
        if rf is None:
            rf = rPr.makeelement(qn("w:rFonts"), {})
            rPr.append(rf)
        rf.set(qn("w:eastAsia"), "微软雅黑")

    lv = LEVELS[level]
    doc = Document()
    for s in doc.sections:
        s.top_margin, s.bottom_margin = Cm(2.2), Cm(2.0)
        s.left_margin, s.right_margin = Cm(2.2), Cm(2.2)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    font(p.add_run(book_title), size=18, bold=True)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    font(p.add_run(f"高考英语难度{level}（{lv['name']}）· "
                   f"{'中英对照版' if bilingual else '纯英文版'} · 共 {len(pairs)} 段"),
         size=9)

    for k, item in enumerate(pairs, 1):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.space_after = Pt(2)
        font(p.add_run(item["en"]), size=11)

        if bilingual:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(10)
            font(p.add_run(item["zh"]), size=10.5)

    doc.save(str(path))
    return path
