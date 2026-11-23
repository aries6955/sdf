"""测试用的 HTML 结构查询:按标题或 id 框出一块,再在其中断言。

为什么不用字符串切片:``html.index("<h2>X</h2>")`` 在 X 不存在时抛 ``ValueError``
—— 那是崩,不是有意义的断言失败;切片右边界写错还会静默取到错误的区域。
这里给出三个稳的入口,找不到就报一条读得懂的错误。

用法::

    from tests.htmlq import block, block_of, order

    card = block(html, "账期合计")     # 标题含「账期合计」的卡片 HTML
    assert "filterbar" in card
    order(html, "下一步", "常用操作")   # 断言标题出现顺序

本项目的卡片一律是 ``<section class="card">…</section>`` 且互不嵌套,
因此按 section 配对即可精确定位,不需要完整的 HTML 解析器。
"""

import re

SECTION_OPEN = re.compile("<section", re.I)
SECTION_CLOSE = re.compile("</section", re.I)
# 标题标签:用字符类代替反斜杠转义,避免在多层 heredoc/脚本里被吃掉
HEADING = re.compile("<h([123])[^>]*>([^<]*)</h[123]>", re.I)
TAG = re.compile("<[^>]+>")


def _match_section(html: str, start: int) -> str:
    """从某个 <section 起点切出与之配对的整段(含首尾标签)。"""
    depth = 0
    pos = start
    while pos < len(html):
        nxt_open = SECTION_OPEN.search(html, pos)
        nxt_close = SECTION_CLOSE.search(html, pos)
        if nxt_close is None:
            raise AssertionError("section 没有闭合标签")
        if nxt_open is not None and nxt_open.start() < nxt_close.start():
            depth += 1
            pos = nxt_open.end()
            continue
        depth -= 1
        pos = nxt_close.end()
        if depth == 0:
            return html[start:pos]
    raise AssertionError("section 没有闭合标签")


def _heading(html: str, heading: str):
    """返回 (标题起点, 标题文字)。标题按「包含」匹配。

    模板常把标题写成「抄表记录 · 第 1 页(共 1 页)」,所以不做全等比较。
    """
    for m in HEADING.finditer(html):
        label = TAG.sub("", m.group(2))
        if heading in label:
            return m.start(), label
    return -1, ""


def block(html: str, heading: str) -> str:
    """切出标题文字含 ``heading`` 的那张卡片。"""
    i, _ = _heading(html, heading)
    if i < 0:
        raise AssertionError(f"页面里找不到标题含「{heading}」的区块")
    before = html.rfind("<section", 0, i)
    if before < 0:
        raise AssertionError(f"「{heading}」不在任何 section 里")
    return _match_section(html, before)


def block_of(html: str, marker: str) -> str:
    """切出含 ``marker``(通常是 ``id="xxx"``)的那个 section,退而求其次是 table。"""
    i = html.find(marker)
    if i < 0:
        raise AssertionError(f"页面里找不到「{marker}」")
    before = html.rfind("<section", 0, i)
    closed = html.rfind("</section>", 0, i)
    if before >= 0 and before > closed:
        return _match_section(html, before)
    t = html.rfind("<table", 0, i)
    te = html.find("</table>", i)
    if t >= 0 and te >= 0:
        return html[t:te + len("</table>")]
    return html[i:]


def heading_pos(html: str, heading: str) -> int:
    """标题元素出现的起点。按标题找,不按正文找 ——

    提示语里常复述卡片名(「筛选在下方「抄表记录」里」),按原文找会命错位置。
    """
    i, _ = _heading(html, heading)
    if i < 0:
        raise AssertionError(f"页面里找不到标题含「{heading}」的元素")
    return i


def order(html: str, *headings: str) -> None:
    """断言这些标题在页面上按给定顺序出现。"""
    positions = [heading_pos(html, h) for h in headings]
    if positions != sorted(positions):
        pairs = ", ".join(f"{h}@{p}" for h, p in zip(headings, positions))
        raise AssertionError(f"顺序不对:{pairs}")
