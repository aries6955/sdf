"""租期操作:入住、退租、换表。归属的唯一实现(ADR-0012)。

表上没有租户字段,"现在归谁"只从开放租期推导;这三个动作是唯一能写这种归属的地方。
HTTP 路由与示例数据是这条 seam 的两个 adapter,因此示例数据不再自带一套平行实现。
"""

from .reading_index import last_of_meter, readings_on_line
from .readings import ReadingRejected, insert


def move_in(conn, *, household_id: int, start_date: str, seats) -> list[int]:
    """入住:每条座位(meter, parts, price, label)开一条计量线并记入户读数。返回新建的线号。"""
    for meter, parts, _price, _label in seats:
        if meter["level"] != "sub":
            raise ReadingRejected(f"「{meter['name']}」是总表,不能分配给租户")
        if meter["retired_on"]:
            raise ReadingRejected(f"「{meter['name']}」已换下,不能分配")
        if meter["line_id"]:
            raise ReadingRejected(f"「{meter['name']}」现在有人在用,请先给上一任退租")
        # 有分表挂在它下面的表是汇总口径,租户不能直接用
        if conn.execute("SELECT 1 FROM meters WHERE main_meter_id=?",
                        (meter["id"],)).fetchone():
            raise ReadingRejected(f"「{meter['name']}」下面挂着分表,是总表口径,不能分配给租户")
        last = last_of_meter(conn, meter["id"])
        if last and parts[0] < last["value"]:
            raise ReadingRejected(
                f"「{meter['name']}」的读数 {parts[0]} 小于该表上次读数 {last['value']},请核对")

    created = []
    for meter, parts, price, label in seats:
        line = conn.execute(
            "INSERT INTO lines(household_id,meter_type,label,move_in_date,move_in_value,note)"
            " VALUES (?,?,?,?,?,?)",
            (household_id, meter["meter_type"], label, start_date, parts[0], "")).lastrowid
        conn.execute("UPDATE meters SET line_id=? WHERE id=?", (line, meter["id"]))
        insert(conn, kind="move_in", line_id=line, meter_id=meter["id"],
               reading_date=start_date, parts=parts, price=price,
               note=f"{meter['name']} 入住读数")
        created.append(line)
    return created


def check_out(conn, *, out_date: str, outs) -> None:
    """退租:每条开放线记终点读数、当场结清该租期,然后关线并腾出表。

    outs: [(line, meter_id, parts, price)]。结算只要求算清差额,不要求尾款已收。
    """
    for line, meter_id, parts, _price in outs:
        rows = readings_on_line(conn, line["id"])
        if not rows:
            raise ReadingRejected("该计量线没有读数,无法退租")
        last = rows[-1]
        if out_date <= last["reading_date"]:
            raise ReadingRejected(f"退租日期需晚于上次读数日期 {last['reading_date']}")
        if parts[0] < last["value"]:
            raise ReadingRejected(f"退租读数 {parts[0]} 小于上次读数 {last['value']},请核对")

    for line, meter_id, parts, price in outs:
        insert(conn, kind="move_out", line_id=line["id"], meter_id=meter_id,
               reading_date=out_date, parts=parts, price=price, note="退租读数")
        conn.execute("UPDATE lines SET move_out_date=?, move_out_value=? WHERE id=?",
                     (out_date, parts[0], line["id"]))
        conn.execute("UPDATE meters SET line_id=NULL WHERE line_id=?", (line["id"],))


def swap_meter(conn, *, old_meter, new_meter, switch_date: str,
               old_parts: tuple, new_parts: tuple, note: str = "") -> None:
    """换表:旧表终读数与新表基数同一天录入,账期不切断(ADR-0014)。

    分表换表不切账期,新表接管该线;换总表时原接入的分表改接到新表,账期本来就不受影响。
    """
    if old_meter["level"] != new_meter["level"] or \
            old_meter["meter_type"] != new_meter["meter_type"]:
        raise ReadingRejected("新旧表必须是同类、同级")
    if old_meter["retired_on"]:
        raise ReadingRejected("旧表已经换下过")
    if new_meter["line_id"]:
        raise ReadingRejected("新表已在某条计量线上使用")

    if old_meter["level"] == "main":
        conn.execute("UPDATE meters SET main_meter_id=? WHERE main_meter_id=?",
                     (new_meter["id"], old_meter["id"]))
        conn.execute("UPDATE meters SET retired_on=? WHERE id=?", (switch_date, old_meter["id"]))
    else:
        line = conn.execute(
            "SELECT * FROM lines WHERE id=? AND move_out_date IS NULL",
            (old_meter["line_id"],)).fetchone() if old_meter["line_id"] else None
        if not line:
            raise ReadingRejected("旧表当前没有租户在用,直接把它换下即可不留账期")
        rows = readings_on_line(conn, line["id"])
        last = rows[-1] if rows else None
        if last:
            if switch_date <= last["reading_date"]:
                raise ReadingRejected(f"换表日期需晚于上次读数日期 {last['reading_date']}")
            if old_parts[0] < last["value"]:
                raise ReadingRejected(
                    f"旧表终读数 {old_parts[0]} 小于上次读数 {last['value']}")
        insert(conn, kind="switch_out", line_id=line["id"], meter_id=old_meter["id"],
               reading_date=switch_date, parts=old_parts,
               price=last["tenant_price"] if last else 0.0, note="旧表终读数")
        insert(conn, kind="switch_base", line_id=line["id"], meter_id=new_meter["id"],
               reading_date=switch_date, parts=new_parts,
               price=last["tenant_price"] if last else 0.0, note="新表基数")
        conn.execute("UPDATE meters SET line_id=NULL, retired_on=? WHERE id=?",
                     (switch_date, old_meter["id"]))
        conn.execute("UPDATE meters SET line_id=? WHERE id=?", (line["id"], new_meter["id"]))

    conn.execute(
        "INSERT INTO meter_switches(old_meter_id,new_meter_id,switch_date,old_final_value,"
        "new_base_value,note) VALUES (?,?,?,?,?,?)",
        (old_meter["id"], new_meter["id"], switch_date, old_parts[0], new_parts[0], note))


def meter(conn, meter_id: int):
    return conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
