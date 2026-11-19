"""页面模块:分页控件、提示条整理、带提示跳回。

这些是界面约定,不是业务事实;三条动作收在一处,路由只写业务判定。
"""

from urllib.parse import quote

from fastapi.responses import RedirectResponse

PAGE_SIZE = 20


def pager(total: int, page: int, base_qs: str) -> tuple[int, int, str]:
    """返回 (当前页, 总页数, 分页控件 HTML)。base_qs 是已拼好的查询串(不含 page)。"""
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    if pages <= 1:
        return page, pages, ""

    def link(n: int, label: str) -> str:
        sep = "&" if base_qs else ""
        return f'<a class="btn ghost small" href="?{base_qs}{sep}page={n}">{label}</a>'

    parts = []
    if page > 1:
        parts.append(link(1, "« 首页"))
        parts.append(link(page - 1, "‹ 上一页"))
    parts.append(f'<span class="pageno">{page} / {pages}</span>')
    if page < pages:
        parts.append(link(page + 1, "下一页 ›"))
        parts.append(link(pages, "末页 »"))
    return page, pages, f'<div class="pager">{"".join(parts)}</div>'


def page_slice(rows, total: int, page: int, base_qs: str):
    """内存里已有全部行时的一页:返回 (本页行, 当前页, 总页数, 分页控件 HTML)。"""
    page, pages, html = pager(total, page, base_qs)
    return rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], page, pages, html


def flash(msg: str = "", ok: str = "") -> tuple[str, str] | None:
    if msg:
        return ("err", msg)
    if ok:
        return ("ok", ok)
    return None


def go(path: str, msg: str = "", ok: str = "") -> RedirectResponse:
    """带一条提示跳回:msg 走 msg 参数,ok 走 ok 参数,都是 303。"""
    key = "msg" if msg else "ok"
    return RedirectResponse(f"{path}?{key}={quote(msg or ok)}", status_code=303)
