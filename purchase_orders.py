"""물류창고 발주(재고이관 창고<>캠프, warehouse_orders) 요청/승인/거절/입고완료."""

import streamlit as st
from sqlalchemy import text

from db import get_db_connection


@st.cache_data(ttl=15)
def list_warehouse_orders():
    """전체 발주 요청 이력을 최신순으로 반환. 발주 요청 탭이 열려있는 동안 매 rerun마다
    도는 쿼리라 짧게 캐시한다 (쓰기 직후에는 이 함수만 .clear()로 지워서 새로고침)."""
    conn = get_db_connection()
    return conn.query("select * from warehouse_orders order by requested_at desc", ttl=0)


def list_pending_warehouse_orders():
    """물류창고가 승인해야 할, 아직 처리 안 한 발주 요청 목록."""
    conn = get_db_connection()
    return conn.query(
        "select * from warehouse_orders where status = 'requested' order by requested_at desc",
        ttl=10,
    )


def list_incoming_warehouse_orders_for_camp(camp):
    """해당 캠프가 받는 쪽으로, 승인되어 입고/검수 확인이 필요한 발주 요청 목록."""
    conn = get_db_connection()
    return conn.query(
        "select * from warehouse_orders where to_camp = :camp and status = 'in_transit' "
        "order by approved_at desc",
        params={"camp": camp},
        ttl=10,
    )


def create_warehouse_order(item_code, item_name, to_camp, qty, weekly_avg_usage, reason, requested_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into warehouse_orders
                    (item_code, item_name, to_camp, qty, weekly_avg_usage, reason, requested_by)
                values (:item_code, :item_name, :to_camp, :qty, :weekly_avg_usage, :reason, :requested_by)
                """
            ),
            {
                "item_code": item_code,
                "item_name": item_name,
                "to_camp": to_camp,
                "qty": qty,
                "weekly_avg_usage": weekly_avg_usage,
                "reason": reason or None,
                "requested_by": requested_by,
            },
        )
        session.commit()


def approve_warehouse_order(order_id, approved_by):
    """발주를 승인 처리 (창고에서 발송 준비 단계로 전환). 박스히어로 실제 재고는 건드리지 않는다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update warehouse_orders
                set status = 'in_transit', approved_by = :approved_by, approved_at = now()
                where id = :id and status = 'requested'
                """
            ),
            {"id": order_id, "approved_by": approved_by},
        )
        session.commit()


def reject_warehouse_order(order_id, rejected_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update warehouse_orders
                set status = 'rejected', approved_by = :rejected_by, approved_at = now()
                where id = :id and status = 'requested'
                """
            ),
            {"id": order_id, "rejected_by": rejected_by},
        )
        session.commit()


def complete_warehouse_order(order_id, received_by, amt):
    """입고완료 처리. 이때만 실제로 캠프 재고 데이터에 반영된다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update warehouse_orders
                set status = 'completed', received_by = :received_by, received_at = now(), amt = :amt
                where id = :id and status = 'in_transit'
                """
            ),
            {"id": order_id, "received_by": received_by, "amt": amt},
        )
        session.commit()
