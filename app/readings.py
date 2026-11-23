"""读数录入:应用层写读数的唯一入口(ADR-0024)。

三相合成、日期先后、隔表逐相比较、已收款锁定都在这里做完,调用方只说"要记录哪一种读数"。
读数的种类决定校验档位与是否触发重算:

- ``move_in``     入户读数,是账期起点;要求不小于该表上次读数。
- ``periodic``    抄表,可以与前后邻接读数比较,因此允许补录到中间(ADR-0021)。
- ``move_out``    退租读数,要求租期仍在开放、日期晚于末读数。
- ``switch_out``  换表时旧表的终读数,不划账期、不触发重算。
- ``switch_base`` 换表时新表的基数,同上。

拒绝时抛 :class:`ReadingRejected`,消息即用户可见文案,由路由原样显示。
"""

from .billing import get_line, period_of, recompute_line
from .inputs import date_or_none, float_or_none, int_or_none
from .ledger import span_hit, touched_by_reading
from .reading_index import neighbours

# 表单字段名的唯一主人。调用方只给前缀,分相后缀由这里补。
FORM_PARTS = ("value", "value_1", "value_2", "value_3")
SWITCH_PARTS = ("switch_out", "switch_base")


def field_names(prefix: str) -> tuple[str, str, str, str]:
    """一个前缀在表单里对应的四个字段名:(合计/单相, A 相, B 相, C 相)。"""
    return (prefix, f"{prefix}_1", f"{prefix}_2", f"{prefix}_3")


class ReadingRejected(Exception):
    """读数不被接受。消息含用户可见的中文文案。"""


def _int(value):
    return int_or_none(value)


def _float(value, default=None):
    return float_or_none(value, default)


def _line(conn, line_id):
    line = get_line(conn, line_id)
    if line is None:
        raise ReadingRejected("计量线不存在")
    return line


def meter_on_line(conn, line_id, meter_id):
    meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
    if not meter or meter["line_id"] != line_id:
        raise ReadingRejected("这块表不在该计量线上,请刷新页面重选")
    return meter


def parts_of(form, meter, prefix: str = "value"):
    """把表单里的分相输入折成 (合计, r1, r2, r3)。三相表三相应齐全,单相表只有一个值。

    调用方只给字段前缀(``value`` / ``base_<meter_id>`` / ``out_<line_id>`` …),
    分相后缀与拒绝文案都在这里定,新入口不必再抄一遍。
    """
    names = field_names(prefix)
    phase = meter["phase"] if meter is not None else "single"
    blank = "三相电表需要分别填写三个读数" if phase == "three" else "请填写读数"
    if phase == "three":
        parts = [_int(form.get(n, "")) for n in names[1:]]
        if any(p is None or p < 0 for p in parts):
            raise ReadingRejected(blank)
        return sum(parts), parts[0], parts[1], parts[2]
    value = _int(form.get(names[0], ""))
    if value is None or value < 0:
        raise ReadingRejected(blank)
    return value, None, None, None


def _guard_lock(conn, line_id, reading_date, inserting, action):
    """已收款的账期不许动:插入会把账期一分为二,改撤会打穿已收的钱(ADR-0015/0021)。

    判据按**计量线**取:同一租户水电两路在同一天各有一期,只有收到钱的那一路该锁。
    """
    hit = touched_by_reading(conn, line_id, reading_date, inserting=inserting)
    if hit:
        tail = ("不能在其区间内补录。" if inserting
                else f"不能{action}。")
        raise ReadingRejected(f"{hit['start']} ~ {hit['end']} 这一期已收款,{tail}请先删除对应收款。")


def _guard_dates(reading_date, prev, nxt, *, kind):
    """插入位置必须夹在前后邻接读数之间。

    抄表按 ADR-0021 可以补录到中间,所以两侧都要比;退租与入驻是账期边界的一头,只比同侧。
    """
    if prev is not None and prev["reading_date"] >= reading_date:
        raise ReadingRejected(f"抄表日期需晚于前一条读数 {prev['reading_date']}")
    if nxt is not None and kind != "move_in" and nxt["reading_date"] <= reading_date:
        raise ReadingRejected(f"抄表日期需早于后一条读数 {nxt['reading_date']}")


def insert(conn, *, kind: str, line_id: int, meter_id: int, reading_date: str,
           parts: tuple, price=None, note: str = "") -> dict:
    """记一条读数,需要时当场重算账期。返回 ``{"id": 新读数 id, "period": 本期账期}``。

    抄表或退租的读数划出一个账期,所以账期随返回值一起给出 —— 调用方要报「本账期应收
    多少」时不必回头再查一遍表(ADR-0015:抄表即成账)。其余种类 period 为 None。
    """
    value, r1, r2, r3 = parts
    line = _line(conn, line_id)
    switch = kind in SWITCH_PARTS
    # 换表时新表尚未挂上这条线,换表读数因此不校验表的归属
    meter = (conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
             if switch else meter_on_line(conn, line_id, meter_id))
    if meter is None:
        raise ReadingRejected("表不存在")

    if not switch and line["move_out_date"]:
        raise ReadingRejected("该租期已结束,不能再抄表")

    prev, nxt = neighbours(conn, line_id, reading_date)
    if not switch:
        _guard_dates(reading_date, prev, nxt, kind=kind)
        if prev is not None and prev["meter_id"] == meter_id:
            if value < prev["value"]:
                raise ReadingRejected(
                    f"本次读数 {value} 小于前一条读数 {prev['value']},请核对")
            if (meter["phase"] == "three" and prev["r1"] is not None
                    and (r1 < prev["r1"] or r2 < prev["r2"] or r3 < prev["r3"])):
                raise ReadingRejected("某一相读数小于前一条,请核对")
        if nxt is not None and nxt["meter_id"] == meter_id and value > nxt["value"]:
            raise ReadingRejected(f"本次读数 {value} 大于后一条读数 {nxt['value']},请核对")
        if prev is None and kind == "periodic" and value < line["move_in_value"]:
            raise ReadingRejected(f"读数不得小于入住读数 {line['move_in_value']}")
        if kind == "periodic":
            _guard_lock(conn, line_id, reading_date, True, "补录")

    if price is None:
        base = nxt or prev
        price = base["tenant_price"] if base else 0.0
    cur = conn.execute(
        "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,tenant_price,"
        "kind,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (line_id, meter_id, reading_date, value, r1, r2, r3, price, kind, note))
    period = None
    if kind in ("periodic", "move_out"):
        recompute_line(conn, line_id)
        period = period_of(conn, line_id, reading_date)
    return {"id": cur.lastrowid, "period": period}


def change(conn, reading_id: int, form) -> None:
    """改一条已有读数。入户读数与换表读数不可在此改。"""
    row = conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
    if not row:
        raise ReadingRejected("找不到这条读数")
    if row["kind"] == "move_in":
        raise ReadingRejected("入户读数是租期起点,不能修改;要改请退租后重新入住")
    if row["kind"] in ("switch_out", "switch_base"):
        raise ReadingRejected("换表读数是换表登记的一部分,不能在此修改")
    line = _line(conn, row["line_id"])
    if line["move_out_date"]:
        raise ReadingRejected("该租期已结束,读数锁定")
    _guard_lock(conn, row["line_id"], row["reading_date"], False, "改读数")

    meter = conn.execute("SELECT * FROM meters WHERE id=?", (row["meter_id"],)).fetchone()
    # 日期必须是可比较的 YYYY-MM-DD:账期排序与区间筛选都靠字符串大小
    raw_date = str(form.get("reading_date", "")).strip()
    reading_date = row["reading_date"] if not raw_date else date_or_none(raw_date)
    if reading_date is None:
        raise ReadingRejected("请填写正确的抄表日期")
    value, r1, r2, r3 = parts_of(form, meter)
    price = _float(form.get("tenant_price", ""), row["tenant_price"])
    prev, nxt = neighbours(conn, row["line_id"], row["reading_date"], reading_id)
    if prev and prev["meter_id"] == row["meter_id"] and (
            value < prev["value"] or reading_date <= prev["reading_date"]):
        raise ReadingRejected(f"读数需不小于上一条 {prev['value']},"
                              f"日期需晚于 {prev['reading_date']}")
    if nxt and nxt["meter_id"] == row["meter_id"] and (
            value > nxt["value"] or reading_date >= nxt["reading_date"]):
        raise ReadingRejected(f"读数不能大于下一条 {nxt['value']},"
                              f"日期需早于 {nxt['reading_date']}")
    conn.execute(
        "UPDATE readings SET reading_date=?, value=?, tenant_price=?, r1=?, r2=?, r3=? "
        "WHERE id=?", (reading_date, value, price, r1, r2, r3, reading_id))
    recompute_line(conn, row["line_id"])


def retract(conn, reading_id: int) -> None:
    """撤回一条常规抄表。撤后前后两期重算,孤立的应收一并清掉。"""
    row = conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
    if not row:
        raise ReadingRejected("记录不存在")
    if row["kind"] != "periodic":
        raise ReadingRejected("只有常规抄表的读数可以撤回")
    line = _line(conn, row["line_id"])
    _guard_lock(conn, row["line_id"], row["reading_date"], False, "撤回")
    if line["move_out_date"]:
        raise ReadingRejected("该租期已结束,读数锁定")
    conn.execute("DELETE FROM readings WHERE id=?", (reading_id,))
    recompute_line(conn, row["line_id"])


def editable(conn, reading, spans=None) -> bool:
    """读数可改的条件:是常规抄表、租期仍在开放、此后没有已收到的钱。

    ``spans`` 是预取的一批已付款区间(页面逐行判定时传,免得一行查一遍)。
    页面与写入共用这一个判定,不再各自拼一遍。
    """
    if reading["kind"] != "periodic":
        return False
    line = get_line(conn, reading["line_id"])
    if line is None or line["move_out_date"]:
        return False
    if spans is not None:
        return not span_hit(spans, reading["line_id"], reading["reading_date"],
                            inserting=False)
    return not touched_by_reading(conn, reading["line_id"],
                                 reading["reading_date"], inserting=False)
