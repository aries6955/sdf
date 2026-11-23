"""表单输入折行:字符串进,值或 None 出。

写入口(读数、收款、缴费)共用这一处,所以「什么算合法日期」只有一份定义。
日期一律要求 ``YYYY-MM-DD`` 且真实存在 —— 账期与流水都按字符串比较大小,
放进来一个 ``2025-13-45`` 会让排序与区间筛选静默错位。
"""

from datetime import datetime


def str_or_none(value):
    """去掉首尾空白;空串当没填。"""
    text = str(value).strip() if value is not None else ""
    return text or None


def int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def float_or_none(value, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def date_or_none(value):
    """``YYYY-MM-DD`` 且真实存在才返回;其余一律 None。"""
    text = str_or_none(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None
