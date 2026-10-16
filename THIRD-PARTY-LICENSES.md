# 第三方依赖许可清单

> **本清单是实测出来的，不是凭印象写的。** 每一行都来自本机已安装包的元数据
> （`importlib.metadata`）与包内 `LICENSE` 文件原文，命令见文末。

## 汇总

| 依赖 | 版本（实测） | 许可证 | 是否传染（copyleft） | 说明 |
|---|---|---|---|---|
| **edge-tts** | 7.3.1 | **LGPL-3.0**（仅 `src/edge_tts/srt_composer.py` 为 MIT） | ⚠️ 弱传染 | 见下方「LGPL 说明」 |
| **charset-normalizer** | 3.5.2 | MIT | 否 | |
| **pywebview** | 6.2.1 | BSD-3-Clause | 否 | © 2014-2017 Roman Sirokov |
| **pystray** | 0.19.5 | **LGPL-3.0** | ⚠️ 弱传染 | 见下方「LGPL 说明」 |
| **Pillow** | 12.3.0 | MIT-CMU（HPND 系） | 否 | © 1997-2011 Secret Labs AB / Fredrik Lundh |
| **PyInstaller**（可选） | — | GPL-2.0 **+ 打包例外条款** | 否（例外条款覆盖） | 用 PyInstaller 打包**不**使产物受 GPL 约束 |
| **ffmpeg**（可选，本仓库不分发） | — | ⚠️ 视构建而定 | ⚠️ 可能强传染 | 见下方「ffmpeg 说明」 |

**结论：本仓库源码采用 MIT 发布是可行的** —— 没有任何 GPL 强传染项被**静态合并**进来。

---

## LGPL 说明（edge-tts、pystray）

LGPLv3 允许「以库的形式被任意许可证的程序使用」。本项目正是这种情形：

- 通过 `import edge_tts` / `import pystray` **作为独立库调用**，没有修改其源码，也没有把它的代码并入本项目的源文件；
- 使用者可以自由地用 `pip install` 替换成自己版本的 edge-tts / pystray（LGPL 要求的「可替换性」成立）；
- 本项目源码完全公开，任何人可自行重建等价环境。

因此 **本项目以 MIT 发布，同时依赖 LGPL 库**，是标准且合规的组合。

**如果你要把它打包成单文件 exe 分发，请务必：**
1. 随包附上 LGPL-3.0 全文（`edge_tts-*.dist-info/licenses/LICENSE` 与 pystray 的许可证）；
2. 在「关于」或 README 中声明用到了 edge-tts 与 pystray 及其许可证；
3. **不要**修改这两个库的源码后闭源分发。

## ffmpeg 说明（重要，决定「能不能干净地开源」）

ffmpeg 本身是 **LGPL-2.1+**，但社区常见构建会把 GPL-only 的组件编进去：

| 构建 | 许可 | 含 x264/x265 |
|---|---|---|
| gyan.dev **essentials** / **full** | **GPL-3.0** | ✅ 含 |
| BtbN `ffmpeg-master-latest-win64-gpl` | GPL-3.0 | ✅ 含 |
| BtbN `ffmpeg-master-latest-win64-lgpl` | LGPL-3.0 | ❌ 不含 |
| 发行版仓库（apt 等）的 `ffmpeg` | 视发行版配置而定 | 通常含 |

⚠️ Python 生态里的 **`imageio-ffmpeg` 附带的是 gyan.dev essentials 构建 = GPL-3.0**。
**把它打进你的发行包里，会让整个包受 GPL 约束。**

**本项目的处理方式：把 ffmpeg 做成可选，且不随仓库分发。**
理由是本应用根本不需要它：

- 在线音色：edge-tts 直接产出 **mp3** → 浏览器原生可播；
- 离线音色：Windows SAPI 直接产出 **wav** → 浏览器原生可播；
- 导出整本：edge-tts 输出是同码率 **CBR mp3**，帧边界可以**字节直接拼接**；
- 只有当「有 ffmpeg」时才用它做更规范的 concat / 转码。

所以 **发行版里可以完全不含 ffmpeg 二进制，许可证干净**。

如果你自己要装 ffmpeg 图方便：**优先选 LGPL 构建**；若用了 GPL 构建，
请只把它当**系统级工具**用（不随包分发），或接受整个发行包按 GPL-3.0 发布。

---

## 复现这份清单

```python
from importlib.metadata import metadata, version, distribution
for p in ("edge-tts", "charset-normalizer", "pywebview", "pystray", "Pillow", "pyinstaller"):
    m = metadata(p)
    print(p, version(p), m.get("License"), [c for c in m.get_all("Classifier") or [] if c.startswith("License")])

# 直接读包内 LICENSE 原文
d = distribution("edge-tts")
for f in d.files:
    if "LICENSE" in str(f).upper():
        print(d.locate_file(f).read_text(encoding="utf-8")[:300])
```

## 附：本仓库不含第三方二进制

`git ls-files` 里的内容只有本项目源码（Python + HTML/CSS/JS）。
`books/`、`cache/`、`data/`、`exports/` 已在 `.gitignore` 中排除，
不会把你的书或生成的音频误传上仓库。
