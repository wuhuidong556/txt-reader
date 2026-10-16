"""app.py —— TXT 朗读器主程序（窗口 + 托盘 + 单实例 + 桌面快捷方式）

跑法：
    python app.py              # 开窗口
    python app.py --no-window  # 只起服务（调试）
    python app.py --shortcut   # 在桌面创建快捷方式后退出

复用微信 AI 控制台那套已验证的套路：本地 HTTP + 随机 token + 单实例 + 托盘。
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import server as S                                    # noqa: E402
from library import ROOT as LIB_ROOT                  # noqa: E402

APP_NAME = "TXT朗读器"
ICON = ROOT / "assets" / "icon.ico"
PYW = sys.executable


# ─────────────────── 单实例 ───────────────────
def already_running(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ─────────────────── 图标 ───────────────────
def make_icon() -> pathlib.Path:
    """用 Pillow 画一个图标（书 + 声波），避免仓库里塞二进制资源之争。"""
    ICON.parent.mkdir(parents=True, exist_ok=True)
    if ICON.exists():
        return ICON
    try:
        from PIL import Image, ImageDraw
    except Exception:                                  # noqa: BLE001
        return ICON
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 16, 240, 240), radius=46, fill=(28, 32, 48, 255))
    # 书
    d.rounded_rectangle((52, 62, 148, 196), radius=10, fill=(76, 141, 255, 255))
    d.rounded_rectangle((64, 76, 136, 182), radius=6, fill=(232, 236, 248, 255))
    for y in range(92, 172, 16):
        d.rectangle((74, y, 126, y + 5), fill=(150, 160, 190, 255))
    # 声波
    for i, h in enumerate((16, 34, 54, 34, 16)):
        x = 168 + i * 15
        d.rounded_rectangle((x, 128 - h, x + 8, 128 + h), radius=4,
                            fill=(124, 92, 255, 255))
    img.save(ICON, sizes=[(256, 256), (64, 64), (32, 32), (16, 16)])
    return ICON


# ─────────────────── 桌面快捷方式 ───────────────────
def make_shortcut() -> str:
    """在桌面建快捷方式。

    **打包后会自动指向 exe 本身**（`sys.frozen`）—— 否则打包给别人的版本，
    快捷方式还指着 `python app.py`，对方机器上根本没有那些文件。
    """
    desktop = pathlib.Path(os.path.expanduser("~")) / "Desktop"
    lnk = desktop / f"{APP_NAME}.lnk"
    icon = make_icon()
    if getattr(sys, "frozen", False):                 # 打包运行中
        target, arguments, workdir = sys.executable, "", str(pathlib.Path(sys.executable).parent)
    else:                                             # 源码运行
        target, arguments, workdir = PYW, f'"{ROOT / "app.py"}"', str(ROOT)
    ps = (
        "$W=New-Object -ComObject WScript.Shell;"
        f"$s=$W.CreateShortcut('{lnk}');"
        f"$s.TargetPath='{target}';"
        f"$s.Arguments='{arguments}';"
        f"$s.WorkingDirectory='{workdir}';"
        f"$s.IconLocation='{icon}';"
        "$s.Description='TXT 朗读器 —— 导入 TXT 边合成边朗读';"
        "$s.Save()"
    )
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return str(lnk) if lnk.exists() else f"失败：{(r.stderr or '')[:150]}"


# ─────────────────── 托盘 ───────────────────
def start_tray(url: str, on_quit) -> None:
    try:
        import pystray
        from PIL import Image
    except Exception:                                  # noqa: BLE001
        print("[托盘] pystray/Pillow 不可用，跳过")
        return
    icon_path = make_icon()
    try:
        image = Image.open(icon_path)
    except Exception:                                  # noqa: BLE001
        image = Image.new("RGB", (64, 64), (76, 141, 255))

    def show(icon=None, item=None):
        webbrowser.open(url)

    def quit_app(icon, item):
        try:
            icon.stop()
        except Exception:                              # noqa: BLE001
            pass
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("打开界面", show, default=True),
        pystray.MenuItem("在浏览器中打开", show),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon(APP_NAME, image, f"{APP_NAME}（在跑）", menu)
    threading.Thread(target=icon.run, daemon=True, name="tray").start()


# ─────────────────── 主程序 ───────────────────
def _spawn_server() -> subprocess.Popen | None:
    """把 HTTP 服务放到**独立子进程**里跑。

    为什么不共用一个进程：pywebview 在部分 Windows/WebView2 环境下会陷入
    `window.native.AccessibilityObject.Bounds.Empty.Empty.Empty…` 的无限递归
    （stderr 可见 `maximum recursion depth exceeded`），GUI 线程被拖死时会
    **连带饿死同进程的 HTTP 线程** —— 端口还在监听，请求却全部超时。
    隔离之后，窗口怎么闹都不影响朗读。
    """
    exe_dir = ROOT
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--serve-only"]
    else:
        args = [PYW, str(ROOT / "app.py"), "--serve-only"]
    flags = 0 if "--serve-only-console" in sys.argv else getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(args, cwd=str(exe_dir), creationflags=flags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_port(port: int, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if already_running(port):
            return True
        time.sleep(0.4)
    return False


def main() -> None:
    args = sys.argv[1:]
    if "--shortcut" in args:
        print("桌面快捷方式：", make_shortcut())
        return

    # 子进程模式：只跑服务，不开窗口
    if "--serve-only" in args:
        S.save_token()
        httpd = S.serve()
        print(f"{APP_NAME} 服务已启动：http://127.0.0.1:{S.PORT}/")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    pid_file = LIB_ROOT / "data" / "_server.pid"

    if already_running(S.PORT):
        token = S.read_token(max_wait=3) or S.TOKEN
        url = f"http://127.0.0.1:{S.PORT}/?token={token}"
        print("已经在运行了，直接打开界面")
        webbrowser.open(url)
        return

    child = _spawn_server()
    if child is not None:
        try:
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(child.pid), encoding="utf-8")
        except Exception:                              # noqa: BLE001
            pass
    if not _wait_port(S.PORT):
        print("服务没能在 40 秒内起来 —— 改用同一个进程里起服务再试")
        child = None
        httpd = S.serve()
        S.save_token()
    token = S.read_token(max_wait=8) or S.TOKEN
    url = f"http://127.0.0.1:{S.PORT}/?token={token}"
    Api.token, Api.port = token, S.PORT      # 供 pick_and_import() 在 Python 侧直连后端
    print(f"{APP_NAME} 已启动：{url}")
    print("（数据目录：%s）" % LIB_ROOT)

    quit_flag = threading.Event()

    def kill_child() -> None:
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except Exception:                          # noqa: BLE001
                pass
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:                              # noqa: BLE001
            pass

    def on_quit() -> None:
        quit_flag.set()
        kill_child()
        os._exit(0)

    if "--no-window" in args:
        try:
            while not quit_flag.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            kill_child()
        return

    start_tray(url, on_quit)

    try:
        import webview
        api = Api()
        window = webview.create_window(
            APP_NAME, url, js_api=api, width=1180, height=760,
            min_size=(900, 600), background_color="#12131a",
        )
        api.window = window

        def on_closed() -> None:
            # 关窗口不退程序：留在托盘里，音频不会被掐断
            print("窗口已关闭，程序仍在托盘运行（要退出请用托盘菜单）")

        window.events.closed += on_closed
        webview.start(debug=False, private_mode=False)
    except Exception as exc:                           # noqa: BLE001
        print(f"[窗口] pywebview 起不来（{type(exc).__name__}: {exc}），改用浏览器打开")
        webbrowser.open(url)
        try:
            while not quit_flag.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            kill_child()


class Api:
    """暴露给前端的原生能力。

    ⚠️ **路径绝不经过 JS 桥。**
    实测教训：`pick_txt()` 把中文路径返回给前端、再 POST 回来时，
    服务端收到的字符串 `exists()` 为假（文件确实存在，逐字符码点比对也一致，
    说明是桥的传输层把字符串改了）。与其去猜桥改了什么，不如让路径**全程留在
    Python 一侧**：选完文件直接在这里调后端导入，只把一个 ASCII 的 book_id 交回前端。
    """

    window = None
    token = ""
    port = 8791

    # 兼容旧前端：只返回路径（有中文字符时不可靠，保留仅作兜底）
    def pick_txt(self):
        return self._dialog()

    def _dialog(self) -> str:
        try:
            import webview
            res = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("文本文件 (*.txt;*.md;*.log)", "所有文件 (*.*)"))
            if not res:
                return ""
            return res[0] if isinstance(res, (list, tuple)) else str(res)
        except Exception as exc:                       # noqa: BLE001
            print("[文件框] 失败：", exc)
            return ""

    def pick_and_import(self):
        """选文件 → 在 Python 侧直接导入 → 只回 ASCII 结果。"""
        path = self._dialog()
        if not path:
            return {"ok": False, "cancelled": True}
        # 把对话框返回的原始路径落盘，出问题时能查（诊断用）
        try:
            (LIB_ROOT / "data").mkdir(parents=True, exist_ok=True)
            with (LIB_ROOT / "data" / "_last_pick.txt").open("a", encoding="utf-8") as fh:
                fh.write(f"{path!r}\n")
        except Exception:                              # noqa: BLE001
            pass
        url = f"http://127.0.0.1:{self.port}/api/import"
        body = json.dumps({"path": path}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"X-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("ok"):
                return {"ok": True, "id": d["book"]["id"], "segs": d["book"]["seg_count"]}
            return {"ok": False, "code": "server", "status": 400}
        except urllib.error.HTTPError as e:
            return {"ok": False, "code": "http", "status": e.code}
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "code": "net", "status": 0,
                    "detail": type(exc).__name__}

    def ready(self):
        print("[界面] 前端已就绪")
        return True

    def echo(self, s):
        """诊断用：把前端传来的字符串原样回传。

        用途：验证 pywebview 的 JS 桥对非 ASCII 的处理。前端只需
        `await pywebview.api.echo("批林批孔（阅读版）")` 再比对返回即可 ——
        这正是「中文路径导入失败」的取证手段。
        """
        return s


if __name__ == "__main__":
    main()
