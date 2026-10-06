"""캠프 간 재고이관(transfer_requests) 요청/승인/거절/입고완료."""

import streamlit as st
from sqlalchemy import text

from db import get_db_connection


@st.cache_data(ttl=15)
def list_transfer_requests(item_code):
    """특정 품목의 이관 요청 이력을 최신순으로 반환.
    재분배 도우미 탭이 열려있으면(품목 선택 상태가 세션에 남아있으면) 다른 탭에서 뭘 눌러도
    매 rerun마다 이 쿼리가 도는 구조라 짧게라도 캐시한다 (쓰기 직후에는 이 함수만 .clear()로
    지워서 새로고침 — 박스히어로 캐시처럼 관련 없는 캐시까지 같이 지우지 않기 위해)."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where item_code = :item_code order by requested_at desc",
        params={"item_code": item_code},
        ttl=0,
    )


@st.cache_data(ttl=15)
def list_all_transfer_requests():
    """전체 캠프 간 이관 요청 이력을 품목 구분 없이 최신순으로 반환. 관리자가 품목 검색 없이
    전체 진행 현황을 보는 용도 (list_warehouse_orders와 동일한 패턴)."""
    conn = get_db_connection()
    return conn.query("select * from transfer_requests order by requested_at desc", ttl=0)


def list_pending_transfer_requests_for_camp(camp):
    """해당 캠프가 보내는 쪽으로 승인 대기 중인(=아직 처리 안 한) 이관 요청 목록."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where from_camp = :camp and status = 'requested' "
        "order by requested_at desc",
        params={"camp": camp},
        ttl=10,
    )


def list_incoming_transit_requests_for_camp(camp):
    """해당 캠프가 받는 쪽으로, 승인되어 이동중이라 입고 확인이 필요한 이관 요청 목록."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where to_camp = :camp and status = 'in_transit' "
        "order by approved_at desc",
        params={"camp": camp},
        ttl=10,
    )


def list_my_requested_awaiting_approval(camp):
    """해당 캠프가 받는 쪽으로 요청했고, 아직 상대 캠프의 승인을 기다리는 중인 목록 (정보성, 액션 없음)."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where to_camp = :camp and status = 'requested' "
        "order by requested_at desc",
        params={"camp": camp},
        ttl=10,
    )


def create_transfer_request(item_code, item_name, from_camp, to_camp, qty, requested_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into transfer_requests (item_code, item_name, from_camp, to_camp, qty, requested_by)
                values (:item_code, :item_name, :from_camp, :to_camp, :qty, :requested_by)
                """
            ),
            {
                "item_code": item_code,
                "item_name": item_name,
                "from_camp": from_camp,
                "to_camp": to_camp,
                "qty": qty,
                "requested_by": requested_by,
            },
        )
        session.commit()


def approve_transfer_request(request_id, approved_by):
    """요청을 승인하고 이동중 상태로 전환. 실제 재고 차감은 입고완료 시점에 이루어진다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'in_transit', approved_by = :approved_by, approved_at = now()
                where id = :id and status = 'requested'
                """
            ),
            {"id": request_id, "approved_by": approved_by},
        )
        session.commit()


def reject_transfer_request(request_id, rejected_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'rejected', approved_by = :rejected_by, approved_at = now()
                where id = :id and status = 'requested'
                """
            ),
            {"id": request_id, "rejected_by": rejected_by},
        )
        session.commit()


def complete_transfer_request(request_id, received_by, amt):
    """입고완료 처리. 실제 이동한 금액(amt)을 함께 기록한다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'completed', received_by = :received_by, received_at = now(), amt = :amt
                where id = :id and status = 'in_transit'
                """
            ),
            {"id": request_id, "received_by": received_by, "amt": amt},
        )
        session.commit()
