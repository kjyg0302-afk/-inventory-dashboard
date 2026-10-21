"""캠프 간 재고이관(transfer_requests) 요청/승인/거절/입고완료."""

import streamlit as st
from sqlalchemy import text

from db import get_db_connection
from chat_notify import send_camp_chat_notification
from formatting import fmt_transfer_qty


def _get_transfer_request(request_id):
    conn = get_db_connection()
    df = conn.query("select * from transfer_requests where id = :id", params={"id": request_id}, ttl=0)
    return df.iloc[0] if not df.empty else None


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
        ttl=0,
    )


def list_outgoing_transit_requests_for_camp(camp):
    """해당 캠프가 보내는 쪽으로, 승인되어 발송해야 하는(이동중) 이관 요청 목록."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where from_camp = :camp and status = 'in_transit' "
        "order by approved_at desc",
        params={"camp": camp},
        ttl=0,
    )


def list_incoming_transit_requests_for_camp(camp):
    """해당 캠프가 받는 쪽으로, 승인되어 이동중이라 입고 확인이 필요한 이관 요청 목록."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where to_camp = :camp and status = 'in_transit' "
        "order by approved_at desc",
        params={"camp": camp},
        ttl=0,
    )


def list_my_requested_awaiting_approval(camp):
    """해당 캠프가 받는 쪽으로 요청했고, 아직 상대 캠프의 승인을 기다리는 중인 목록 (정보성, 액션 없음)."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where to_camp = :camp and status = 'requested' "
        "order by requested_at desc",
        params={"camp": camp},
        ttl=0,
    )


def list_transfer_requests_for_camp(camp, days=30):
    """품목 검색 없이, 해당 캠프가 보내거나 받는 이관 요청 전체 — 진행중(요청중/이동중)은 기간과
    상관없이 전부, 완료/거절은 최근 days일 것만."""
    conn = get_db_connection()
    return conn.query(
        """
        select * from transfer_requests
        where (from_camp = :camp or to_camp = :camp)
          and (status in ('requested', 'in_transit')
               or coalesce(received_at, approved_at, requested_at) >= now() - make_interval(days => :days))
        order by requested_at desc
        """,
        params={"camp": camp, "days": days},
        ttl=0,
    )


def list_my_recently_rejected(camp, days=7):
    """해당 캠프가 요청했다가 최근 거절된 목록 (사유 확인용, 정보성)."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where to_camp = :camp and status = 'rejected' "
        "and approved_at >= now() - make_interval(days => :days) order by approved_at desc",
        params={"camp": camp, "days": days},
        ttl=0,
    )


def create_transfer_request(item_code, item_name, from_camp, to_camp, qty, requested_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into transfer_requests
                    (item_code, item_name, from_camp, to_camp, qty, requested_qty, requested_by)
                values (:item_code, :item_name, :from_camp, :to_camp, :qty, :qty, :requested_by)
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
    send_camp_chat_notification(
        from_camp,
        f"📦 재고이관 요청이 들어왔어요\n{item_name} · {int(qty)}개 · {from_camp} → {to_camp}\n"
        f"요청자: {requested_by}\n승인/거절이 필요해요.",
    )


def approve_transfer_request(request_id, approved_by, approved_qty):
    """요청을 승인하고 이동중 상태로 전환. 보내는 캠프가 가능한 수량만큼만 승인할 수 있어서
    qty를 승인 수량으로 바꾸고, 원래 요청 수량은 requested_qty에 남긴다.
    (UPDATE의 우변은 모두 갱신 전 값을 보므로 requested_qty에는 원래 qty가 들어간다.)
    실제 재고 차감은 입고완료 시점에 이루어진다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'in_transit', approved_by = :approved_by, approved_at = now(),
                    requested_qty = coalesce(requested_qty, qty), qty = :approved_qty
                where id = :id and status = 'requested'
                """
            ),
            {"id": request_id, "approved_by": approved_by, "approved_qty": int(approved_qty)},
        )
        session.commit()
    r = _get_transfer_request(request_id)
    if r is not None:
        send_camp_chat_notification(
            r["to_camp"],
            f"✅ 이관 요청이 승인되어 이동중이에요\n{r['item_name']} · {fmt_transfer_qty(r)} · "
            f"{r['from_camp']} → {r['to_camp']}\n승인자: {approved_by}\n물건 받으면 입고완료 눌러주세요.",
        )


def reject_transfer_request(request_id, rejected_by, reason):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'rejected', approved_by = :rejected_by, approved_at = now(),
                    reject_reason = :reason
                where id = :id and status = 'requested'
                """
            ),
            {"id": request_id, "rejected_by": rejected_by, "reason": reason},
        )
        session.commit()
    r = _get_transfer_request(request_id)
    if r is not None:
        send_camp_chat_notification(
            r["to_camp"],
            f"❌ 이관 요청이 거절됐어요\n{r['item_name']} · {int(r['qty'])}개 · "
            f"{r['from_camp']} → {r['to_camp']}\n거절자: {rejected_by}\n사유: {reason}",
        )


def revert_transfer_approval(request_id, reverted_by):
    """잘못 누른 승인을 되돌려 요청중으로 돌린다 (관리자 전용). 승인 단계에서는 재고가
    움직이지 않으므로 상태만 되돌리면 된다 — 입고완료된 건은 대상이 아니다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'requested', approved_by = null, approved_at = null,
                    qty = coalesce(requested_qty, qty)
                where id = :id and status = 'in_transit'
                """
            ),
            {"id": request_id},
        )
        session.commit()
    r = _get_transfer_request(request_id)
    if r is not None:
        msg = (
            f"↩️ 이관 승인이 취소되어 다시 요청중이에요\n{r['item_name']} · {int(r['qty'])}개 · "
            f"{r['from_camp']} → {r['to_camp']}\n처리자: {reverted_by}"
        )
        send_camp_chat_notification(r["from_camp"], msg + "\n다시 확인 후 승인/거절해주세요.")
        send_camp_chat_notification(r["to_camp"], msg + "\n아직 발송되지 않은 상태예요.")


def complete_transfer_request(request_id, received_by, amt, received_qty):
    """입고완료 처리. 받는 캠프가 검수한 실제 수량(received_qty)과 이동 금액(amt)을 기록한다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'completed', received_by = :received_by, received_at = now(), amt = :amt,
                    received_qty = :received_qty
                where id = :id and status = 'in_transit'
                """
            ),
            {"id": request_id, "received_by": received_by, "amt": amt, "received_qty": int(received_qty)},
        )
        session.commit()
    r = _get_transfer_request(request_id)
    if r is not None:
        send_camp_chat_notification(
            r["from_camp"],
            f"📬 이관이 입고완료 처리됐어요\n{r['item_name']} · {fmt_transfer_qty(r)} · "
            f"{r['from_camp']} → {r['to_camp']}\n입고 확인자: {received_by}",
        )
