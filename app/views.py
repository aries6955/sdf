"""视图层:Jinja2 模板渲染。

业务路由只负责查询数据与组装 context,HTML 一律在 templates/ 中维护。
"""

from pathlib import Path
from urllib.parse import quote

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__

TEMPLATES = Path(__file__).resolve().parent / "templates"

# 侧栏:(标识, 路径, 文案, 图标名)。不再分栏,平铺成一列。
NAV = [
    ("home", "/", "首页概览", "home"),
    ("daily", "/readings", "日常管理", "pen"),
    ("ledger", "/ledger", "账务管理", "wallet"),
    ("statistics", "/statistics", "统计与查询", "chart"),
]

# 图标集:统一 24×24 线性图标,跟品牌色走,不再用 ⌂ ▤ 这类字形
ICONS = {
    "home": '<path d="M3 10.6 12 3.2l9 7.4"/><path d="M5.2 9.6V19a2 2 0 0 0 2 2h3.2v-5.6h3.2V21h3.2a2 2 0 0 0 2-2V9.6"/>',
    "pen": '<path d="M12.5 20.5H21"/><path d="M16.4 3.6a2.1 2.1 0 0 1 3 3L7.6 18.4l-4 1 1-4Z"/>',
    "wallet": '<path d="M19 7.5V5a1.5 1.5 0 0 0-1.5-1.5H5A2.5 2.5 0 0 0 2.5 6v12A2.5 2.5 0 0 0 5 20.5h13a1.5 1.5 0 0 0 1.5-1.5V18"/><path d="M21.5 9.5h-4.2a2.3 2.3 0 0 0 0 4.6h4.2a.9.9 0 0 0 .9-.9v-2.8a.9.9 0 0 0-.9-.9Z"/>',
    "chart": '<path d="M3.5 20.5h17"/><path d="M7.5 20.5v-6"/><path d="M12 20.5V5.5"/><path d="M16.5 20.5v-9"/>',
    "settings": '<path d="M4 6.5h16"/><path d="M4 12h16"/><path d="M4 17.5h16"/><circle cx="9" cy="6.5" r="2.1"/><circle cx="15" cy="12" r="2.1"/><circle cx="8" cy="17.5" r="2.1"/>',
    "power": '<path d="M12 3.5v8"/><path d="M18 6.8a8.5 8.5 0 1 1-12 0"/>',
    "coin": '<rect x="2.5" y="6" width="19" height="12" rx="2.4"/><circle cx="12" cy="12" r="2.6"/>',
}

LOGOMARK = (
    '<svg class="logomark" viewBox="0 0 32 32" role="img" aria-label="水电费管理">'
    '<defs><linearGradient id="sdf-logo" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#0ea5e9"/>'
    "</linearGradient></defs>"
    '<rect width="32" height="32" rx="9" fill="url(#sdf-logo)"/>'
    '<path d="M16 5.4c4.4 5.7 7 9.2 7 12.2a7 7 0 0 1-14 0c0-3 2.6-6.5 7-12.2Z" fill="#fff"/>'
    '<path d="M16.4 12.2 11.2 20h3.5l-1.1 4.4 5.4-8h-3.5Z" fill="#4f46e5"/>'
    "</svg>"
)

# 浏览器标签页图标:与侧栏同一枚水+电的标识,不再用 💧 emoji
FAVICON = "data:image/svg+xml," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#0ea5e9"/>'
    "</linearGradient></defs>"
    '<rect width="32" height="32" rx="9" fill="url(#g)"/>'
    '<path d="M16 5.4c4.4 5.7 7 9.2 7 12.2a7 7 0 0 1-14 0c0-3 2.6-6.5 7-12.2Z" fill="#fff"/>'
    '<path d="M16.4 12.2 11.2 20h3.5l-1.1 4.4 5.4-8h-3.5Z" fill="#4f46e5"/>'
    "</svg>"
)

TYPE_NAMES = {"water": "水", "electricity": "电"}
LEVEL_NAMES = {"main": "总表", "sub": "分表"}
KIND_NAMES = {"move_in": "入住", "periodic": "抄表", "move_out": "退租",
              "switch_out": "旧表终读数", "switch_base": "新表基数"}

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),
    auto_reload=True,   # 模板文件改动立即生效,不依赖进程重启
)
_env.globals.update(
    version=__version__,
    nav=NAV,
    icon=lambda name: (
        '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{ICONS.get(name, "")}</svg>'
    ),
    logomark=LOGOMARK,
    favicon=FAVICON,
    type_name=lambda k: TYPE_NAMES.get(k, ""),
    # 表类型标签:水/电各一套配色,样式在 DESIGN.md「表类型标签」
    type_tag=lambda k: f'<span class="tag {k}">{TYPE_NAMES.get(k, "")}</span>',
    level_name=lambda k: LEVEL_NAMES.get(k, ""),
    kind_name=lambda k: KIND_NAMES.get(k, ""),
    money=lambda v: f"{(v or 0):,.2f}",
)


def render(template: str, **context) -> HTMLResponse:
    """渲染模板并返回 HTMLResponse(带 no-store 由中间件统一处理)。"""
    return HTMLResponse(_env.get_template(template).render(**context))
