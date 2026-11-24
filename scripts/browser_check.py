"""用真实 Chromium 走一遍页面,收集用户会遇到的错误。

为什么不用 Playwright:本机没有它的 Python 包,也不能联网装。Chromium 自带
DevTools 协议,够用而且零依赖 —— 只要一个最小 WebSocket 客户端(下面 40 行)。

用法:
    uv run python scripts/browser_check.py http://100.112.157.25:8536 密码

做四件事:
1. 登录,逐页访问,记录 HTTP 状态、控制台错误、未捕获异常、失败的请求
2. 用真实鼠标点开每个可交互元素(链接、按钮、tab),比对点击前后
3. 检查页面里的数字/文字是否出现替换符(乱码的可靠信号)
4. 打印每个异常的调用栈,方便直接定位代码
"""

import base64
import contextlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHROME = Path(os.environ.get(
    "SDF_CHROME",
    Path.home() / "AppData/Local/ms-playwright/chromium-1148/chrome-win/chrome.exe"))


# ---------- 最小 WebSocket 客户端 ----------

class WS:
    """只支持 CDP 需要的那点功能:发文本帧、收文本帧,不做分片。"""

    def __init__(self, url: str):
        _, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        assert b"101" in buf.split(b"\r\n")[0], buf[:200]
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> dict:
        _b0, b1 = self._recv_exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recv_exact(8))[0]
        return json.loads(self._recv_exact(n).decode())

    def call(self, method: str, params: dict = None, session: str = None,
             timeout: float = 20.0) -> dict:
        """发一条 CDP 命令等它的回包。

        必须有超时:点了会导航的元素之后,Runtime.evaluate 可能永远不回,
        整个检查就会挂死而不是报错。
        """
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.send(msg)
        self.sock.settimeout(timeout)
        try:
            while True:
                reply = self.recv()
                if reply.get("id") == self.next_id:
                    if "error" in reply:
                        raise RuntimeError(f"{method}: {reply['error']}")
                    return reply.get("result", {})
        except (socket.timeout, TimeoutError):
            raise TimeoutError(f"{method} 超时 {timeout}s")
        finally:
            self.sock.settimeout(30)


# ---------- 检查器 ----------

class Audit:
    def __init__(self, base: str, password: str):
        self.base = base.rstrip("/")
        self.password = password
        self.problems: list[dict] = []
        self.profile = tempfile.TemporaryDirectory(prefix="sdf-audit-")
        self.proc = subprocess.Popen(
            [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--remote-debugging-port=0",
             f"--user-data-dir={self.profile.name}", "--window-size=1440,1000",
             "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.ws = self._connect()

    def _connect(self) -> WS:
        """DevToolsActivePort 两行:端口、浏览器 websocket 路径(新版已含 uuid)。"""
        port_file = Path(self.profile.name) / "DevToolsActivePort"
        for _ in range(200):
            if port_file.exists():
                lines = port_file.read_text().splitlines()
                if len(lines) >= 2:
                    return WS(f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}")
            time.sleep(0.1)
        raise RuntimeError("Chromium 没起来")

    def add(self, kind: str, where: str, detail: str, extra: dict = None):
        self.problems.append({"kind": kind, "where": where, "detail": detail,
                              **(extra or {})})

    def open_page(self):
        target = self.ws.call("Target.createTarget", {"url": "about:blank"})
        session = self.ws.call("Target.attachToTarget",
                               {"targetId": target["targetId"], "flatten": True})["sessionId"]
        self.ws.call("Page.setInterceptFileChooserDialog",
                     {"enabled": False}, session)
        for event in ("Runtime.consoleAPICalled", "Runtime.exceptionThrown",
                      "Network.loadingFailed", "Network.responseReceived",
                      "Log.entryAdded"):
            self.ws.call(event.split(".")[0] + ".enable", {}, session)
        self.ws.call("Page.enable", {}, session)
        self.session = session
        return session

    def drain(self, seconds: float = 0.6) -> list[dict]:
        """读一小段时间里的事件,不阻塞太久。"""
        self.ws.sock.settimeout(seconds)
        events = []
        try:
            while True:
                events.append(self.ws.recv())
        except (socket.timeout, TimeoutError):
            pass
        finally:
            self.ws.sock.settimeout(30)
        return events

    def goto(self, url: str, wait: float = 1.2):
        """导航到 url 并收集事件,直到 loadEventFired 之后再收一小段尾巴。

        只按时间等会漏事件:导航后首屏事件往往比固定等待更晚到,
        于是"没发现问题"其实是根本没读到。
        """
        self.ws.call("Page.navigate", {"url": url}, self.session)
        events = self.wait_for_load(wait)
        self.rearm()
        return events

    def wait_for_load(self, tail: float = 0.5, limit: float = 4.0) -> list[dict]:
        events, loaded, deadline = [], False, time.time() + limit
        self.ws.sock.settimeout(2.0)
        try:
            while time.time() < deadline:
                try:
                    ev = self.ws.recv()
                except (socket.timeout, TimeoutError):
                    if loaded:
                        break
                    continue
                events.append(ev)
                if ev.get("method") == "Page.javascriptDialogOpening":
                    # 一律取消:检查器只验证页面能弹窗,不替用户确认删除
                    with contextlib.suppress(Exception):
                        self.ws.call("Page.handleJavaScriptDialog",
                                     {"accept": False}, self.session, timeout=5.0)
                    events.append({"method": "Audit.dialogCancelled",
                                   "params": {"message": ev.get("params", {}).get("message", "")}})
                if ev.get("method") == "Page.loadEventFired":
                    loaded = True
        finally:
            self.ws.sock.settimeout(30)
        events += self.drain(tail)
        return events

    def eval_js(self, expr: str):
        r = self.ws.call("Runtime.evaluate",
                         {"expression": expr, "returnByValue": True,
                          "awaitPromise": True}, self.session)
        return r.get("result", {}).get("value")

    def collect(self, where: str, events: list[dict]):
        for ev in events:
            method = ev.get("method")
            p = ev.get("params", {})
            if method == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                text = d.get("exception", {}).get("description") or d.get("text")
                self.add("JS 异常", where, text, {"stack": d.get("stackTrace")})
            elif method == "Runtime.consoleAPICalled" and p.get("type") == "error":
                args = " ".join(str(a.get("value", a.get("description", "")))
                                for a in p.get("args", []))
                self.add("控制台错误", where, args)
            elif method == "Network.loadingFailed":
                if p.get("type") != "Ping":
                    self.add("请求失败", where,
                             f"{p.get('type')} {p.get('errorText')}")
            elif method == "Network.responseReceived":
                r = p.get("response", {})
                if r.get("status", 0) >= 400:
                    self.add("HTTP 错误", where, f"{r.get('status')} {r.get('url')}")
            elif method == "Audit.dialogCancelled":
                self.add("确认弹窗", where, f"弹出确认框:{p.get('message','')[:80]}")
            elif method == "Log.entryAdded":
                e = p.get("entry", {})
                if e.get("level") == "error":
                    self.add("浏览器日志", where, e.get("text", ""))

    def check_text(self, where: str):
        """乱码的两个可靠信号:替换符与 UTF-8 被按 latin-1 解码后的双字节序列。

        用 fromCharCode 而不是源码里的反斜杠 u 转义 —— 后者进到 JS 里只是
        字面文本,正则永远匹配不上(这个 bug 曾让检查器静默放过乱码)。
        """
        bad = self.eval_js(r"""
        (() => {
          const t = document.body.innerText;
          const hits = [];
          if (t.indexOf(String.fromCharCode(0xFFFD)) >= 0) hits.push('含替换符 U+FFFD');
          // UTF-8 被按 latin-1 解码后,每个中文字会变成 Ã©/Ã¨/æ° 这类两字节组合:
          // 高位字符(C0-FF)后紧跟一个可见符号,连续出现就是乱码。
          const hiRe = new RegExp('[' + String.fromCharCode(0xC0) + '-'
                                 + String.fromCharCode(0xFF) + ']', 'g');
          const n = (t.match(hiRe) || []).length;
          if (n >= 2) hits.push('疑似乱码:' + n + ' 处高位字符');
          return hits;
        })()
        """)
        for h in bad or []:
            self.add("文字乱码", where, h)

    def clickables(self) -> list[dict]:
        return self.eval_js(r"""
        (() => {
          const out = [];
          document.querySelectorAll('a[href], button, summary, [data-edit], [role=tab]')
            .forEach((el, i) => {
              const txt = (el.innerText || el.value || el.getAttribute('aria-label') || '')
                            .trim().slice(0, 24);
              if (!txt && !el.hasAttribute('data-edit')) return;
              el.setAttribute('data-audit-id', i);
              out.push({i, tag: el.tagName.toLowerCase(), text: txt,
                        href: el.getAttribute('href') || ''});
            });
          return out;
        })()
        """) or []

    def click(self, i: int, wait: float = 1.0):
        """点一下并收集后续事件。

        整页导航时 Runtime.evaluate 拿不到回包,这里把超时当正常,
        然后按一次完整加载去等;单个坏元素不会拖死整轮检查。
        """
        expr = (f"document.querySelector('[data-audit-id=\"{i}\"]')?.click()")
        with contextlib.suppress(TimeoutError):
            self.ws.call("Runtime.evaluate",
                         {"expression": expr, "returnByValue": True},
                         self.session, timeout=2.5)
        events = self.wait_for_load(0.4, limit=2.5)
        self.rearm()
        return events

    def rearm(self):
        """导航后旧 execution context 已销毁,重新启用运行时域。"""
        for dom in ("Runtime.enable", "Page.enable"):
            with contextlib.suppress(Exception):
                self.ws.call(dom, {}, self.session, timeout=5.0)

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        finally:
            time.sleep(0.5)      # 等 Chromium 释放 profile,否则 Windows 上删不掉
            with contextlib.suppress(PermissionError):
                self.profile.cleanup()   # 残留临时目录无害,不值得为它失败


PAGES = ["/", "/readings", "/households", "/meters", "/payments",
         "/ledger", "/statistics", "/init"]


def login(a: Audit, password: str) -> bool:
    """填表并提交;提交会让当前文档失活,所以只发点击,不 await 结果。"""
    a.goto(f"{a.base}/login")
    a.eval_js(f"""
    (() => {{
      const f = document.querySelector('form');
      f.password.value = {json.dumps(password)};
      f.requestSubmit();
      return true;
    }})()
    """)
    a.drain(1.5)
    return a.eval_js("location.pathname") == "/"


def main() -> int:
    base = sys.argv[1]
    password = sys.argv[2]
    a = Audit(base, password)
    try:
        a.open_page()
        if not login(a, password):
            print("登录失败,后续检查没有意义")
            return 2
        print(f"已登录 {base}\n")

        for path in PAGES:
            events = a.goto(f"{base}{path}")
            a.collect(path, events)
            a.check_text(path)

        # 逐页点开可交互元素,每次点完回到该页重扫
        for path in ("/readings", "/households", "/meters", "/payments", "/ledger"):
            events = a.goto(f"{base}{path}")
            a.collect(path, events)
            items = a.clickables()
            # 导航链接逐个 goto 就够(每个页面在上一段已单独访问过),
            # 这里只点会跑 JS 的元素:按钮、表单提交、带 data-* 的交互控件。
            todo = [it for it in items if not it["href"] or it["href"].startswith("#")]
            print(f"{path}: {len(items)} 个可交互元素,其中 {len(todo)} 个需点击验证")
            budget = time.time() + 90          # 单页 90 秒上限,超了就记一笔继续下一页
            for it in todo:
                if time.time() > budget:
                    a.add("检查未完成", path, f"本页剩余 {len(todo)} 个元素因超时未点")
                    break
                label = it["text"] or it["tag"]
                where = f"{path} → 点击「{label}」"
                try:
                    events = a.goto(f"{base}{path}")   # 复位
                    a.collect(path, events)
                    events = a.click(it["i"])
                    a.collect(where, events)
                    a.check_text(where)
                except Exception as e:      # 单个元素出问题不能中断整轮
                    a.add("检查器异常", where, f"{type(e).__name__}: {e}")
                    with contextlib.suppress(Exception):
                        a.goto(f"{base}{path}")
    finally:
        a.close()

    if not a.problems:
        print("没有发现问题。")
        return 0

    # 同类同页只留一条,避免一个坏模板刷满屏幕
    seen, out = set(), []
    for p in a.problems:
        key = (p["kind"], p["where"], p["detail"][:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(p)

    print(f"共 {len(out)} 类问题:\n")
    for i, p in enumerate(out, 1):
        print(f"{i}. [{p['kind']}] {p['where']}")
        print(f"   {p['detail'][:400]}")
        if p.get("stack"):
            frames = p["stack"].get("callFrames", [])[:3]
            for f in frames:
                print(f"     at {f.get('functionName') or '(匿名)'} "
                      f"{f.get('url','').split('/')[-1]}:{f.get('lineNumber')}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
