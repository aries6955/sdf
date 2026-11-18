"""视图层:Jinja2 模板渲染。

业务路由只负责查询数据与组装 context,HTML 一律在 templates/ 中维护。
"""

from pathlib import Path

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).resolve().parent / "templates"

# 侧栏分组:(标识, 路径, 文案, 图标)
NAV = [
    ("总览", [("home", "/", "首页概览", "⌂")]),
    ("日常记账", [("readings", "/readings", "抄表", "✎"),
                  ("payments", "/payments", "缴费", "¥")]),
    ("查询", [("statistics", "/statistics", "统计与查询", "▤")]),
    ("系统", [("households", "/households", "租户", "☰"),
              ("meters", "/meters", "表设置", "▣"),
              ("init", "/init", "设置", "⚙")]),
]

TYPE_NAMES = {"water": "水", "electricity": "电"}
LEVEL_NAMES = {"main": "总表", "sub": "分表"}

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),
    auto_reload=True,   # 模板文件改动立即生效,不依赖进程重启
)
_env.globals.update(
    nav=NAV,
    nav_map={label: items for label, items in NAV},
    type_name=lambda k: TYPE_NAMES.get(k, ""),
    level_name=lambda k: LEVEL_NAMES.get(k, ""),
    money=lambda v: f"{(v or 0):,.2f}",
)


def render(template: str, **context) -> HTMLResponse:
    """渲染模板并返回 HTMLResponse(带 no-store 由中间件统一处理)。"""
    return HTMLResponse(_env.get_template(template).render(**context))
