"""把一个页面渲染成可截图的本地 HTML —— 带登录态与内联样式,一次到位。

为什么需要它:``shot.py`` 只接受 URL,但它拿不到登录态;登录后也无法让 Edge 的
--screenshot 直接落在需要滚动的区域。于是每次截图都要手写一遍
"登录 → 渲染 → 把 <link> 换成内联 <style> → 落盘 → file:// 截图"。

用法:
    uv run python scripts/dump_page.py /readings            # 落盘到 .tmp/readings.html
    uv run python scripts/dump_page.py /ledger --shot       # 顺便截图到 .tmp/ledger.png
    uv run python scripts/dump_page.py /readings --click 'button[data-edit]'
    uv run python scripts/dump_page.py /readings --size 1500,1200

--click 会在页面脚本跑完后点一下该选择器,用来验证行内编辑、确认卡片这类
必须执行 JS 才看得见的状态。
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

TMP = ROOT / ".tmp"


def password() -> str:
    """与线上一致:只从环境变量读,不读文件。"""
    return os.environ.get("SDF_PASSWORD", "admin123")


def dump(path: str, click: str = "", screenshots: bool = False,
         size: str = "1500,1200", wait: float = 4.0) -> Path:
    """渲染 path 页面到 .tmp/<slug>.html,可选截图。返回落盘路径。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/login", data={"password": password()})
    resp = client.get(path)
    if resp.status_code != 200:
        raise SystemExit(f"{path} 返回 {resp.status_code},无法渲染")

    html = resp.text
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="/static/style.css">',
                        f"<style>{css}</style>")
    # file:// 下取不到 /static/*.js,内联进来才能跑
    for js in sorted((ROOT / "app/static").glob("*.js")):
        src = f'<script src="/static/{js.name}" defer></script>'
        html = html.replace(src, f"<script>{js.read_text(encoding='utf-8')}</script>")

    if click:
        html = html.replace("</body>", f"""<script>
setTimeout(function(){{
  var el=document.querySelector({click!r});
  if(el) el.click(); else document.title='click-miss';
}}, 400);
</script></body>""")

    slug = path.strip("/").replace("/", "-").replace("?", "-") or "home"
    out = TMP / f"{slug}.html"
    TMP.mkdir(exist_ok=True)
    # 同名旧产物先清掉:调试脚本常补 ../_probe.html 这类变体,不清会越积越多
    for stale in TMP.glob(f"{slug}.*"):
        if stale.suffix in (".html", ".png"):
            stale.unlink(missing_ok=True)
    out.write_text(html, encoding="utf-8")

    if screenshots:
        from shot import screenshot
        png = out.with_suffix(".png")
        if not screenshot(f"file:///{out.as_posix()}", png, size, wait):
            raise SystemExit(f"截图失败:{png}")
        print(f"shot  {png}")
    print(f"html  {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染页面到可截图的本地 HTML")
    ap.add_argument("path", help="例如 /readings")
    ap.add_argument("--shot", action="store_true", help="顺便截图")
    ap.add_argument("--click", default="", help="页面加载后点击的选择器")
    ap.add_argument("--size", default="1500,1200")
    a = ap.parse_args()
    dump(a.path, a.click, a.shot, a.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
