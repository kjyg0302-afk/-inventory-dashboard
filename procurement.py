"""외부 구매발주(내자/외자) — 거래처에 발주 넣고 입고예정/입고까지 추적.
재고이관(창고<>캠프, purchase_orders.py)과 달리 회사 바깥 거래처에서 실제로 물건을
사오는 과정을 다룬다. 상태 전이는 vendor_order_items 라인 단위로 일어난다:
ordered(발주) -> expected(입고예정일 입력됨) -> received(입고완료) / 언제든 cancelled."""

import streamlit as st
from sqlalchemy import text

from db import get_db_connection


@st.cache_data(ttl=15)
def list_vendor_order_items(statuses=None):
    """구매발주 라인을 거래처/발주 정보와 함께 조회. statuses를 주면 그 상태만 필터링."""
    conn = get_db_connection()
    sql = """
        select i.*, o.vendor_id, o.ordered_by, o.ordered_at, o.note as order_note,
               v.name as vendor_name, v.vendor_type
        from vendor_order_items i
        join vendor_orders o on o.id = i.vendor_order_id
        join vendors v on v.id = o.vendor_id
    """
    params = {}
    if statuses:
        sql += " where i.status = any(:statuses)"
        params["statuses"] = list(statuses)
    sql += " order by o.ordered_at desc, i.id"
    return conn.query(sql, params=params, ttl=0)


def incoming_qty_by_item():
    """아직 입고 안 된(ordered/expected) 라인을 item_code로 묶은 입고예정수량."""
    conn = get_db_connection()
    return conn.query(
        """
        select item_code, item_name, sum(qty) as incoming_qty,
               min(expected_arrival_date) as earliest_expected
        from vendor_order_items
        where status in ('ordered', 'expected')
        group by item_code, item_name
        order by item_name
        """,
        ttl=15,
    )


def create_vendor_order(vendor_id, ordered_by, note, line_items):
    """헤더 1개 + 라인 여러 개를 한 번에 등록. line_items: [{item_code, item_name, qty,
    unit_price, currency, amt}, ...]"""
    conn = get_db_connection()
    with conn.session as session:
        result = session.execute(
            text(
                """
                insert into vendor_orders (vendor_id, ordered_by, note)
                values (:vendor_id, :ordered_by, :note)
                returning id
                """
            ),
            {"vendor_id": vendor_id, "ordered_by": ordered_by, "note": note or None},
        )
        order_id = result.scalar_one()
        for line in line_items:
            session.execute(
                text(
                    """
                    insert into vendor_order_items
                        (vendor_order_id, item_code, item_name, qty, unit_price, currency, amt)
                    values (:order_id, :item_code, :item_name, :qty, :unit_price, :currency, :amt)
                    """
                ),
                {
                    "order_id": order_id,
                    "item_code": line["item_code"],
                    "item_name": line["item_name"],
                    "qty": line["qty"],
                    "unit_price": line.get("unit_price"),
                    "currency": line.get("currency") or "KRW",
                    "amt": line.get("amt"),
                },
            )
        session.commit()
    return order_id


def set_expected_arrival(item_id, expected_date):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update vendor_order_items
                set status = 'expected', expected_arrival_date = :expected_date, updated_at = now()
                where id = :id and status = 'ordered'
                """
            ),
            {"id": item_id, "expected_date": expected_date},
        )
        session.commit()


def receive_item(item_id, received_qty, received_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update vendor_order_items
                set status = 'received', received_qty = :received_qty, received_by = :received_by,
                    received_at = now(), updated_at = now()
                where id = :id and status = 'expected'
                """
            ),
            {"id": item_id, "received_qty": received_qty, "received_by": received_by},
        )
        session.commit()


def cancel_item(item_id):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text("update vendor_order_items set status = 'cancelled', updated_at = now() where id = :id"),
            {"id": item_id},
        )
        session.commit()
