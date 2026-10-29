"""이관/발주 요청 한 건을 표시하고 승인/거절/입고완료 버튼을 처리하는 공용 UI 행.
로그인 배너와 rebalance/purchase 탭 양쪽에서 재사용한다."""

import streamlit as st

from auth import NO_STOCK_ACCOUNTS
from formatting import fmt_request_timeline, fmt_transfer_qty
from stock_ops import find_item_by_code, deduct_camp_stock, add_camp_stock
from app_data import save_data
from transfers import (
    list_transfer_requests,
    list_all_transfer_requests,
    approve_transfer_request,
    reject_transfer_request,
    complete_transfer_request,
    revert_transfer_approval,
)
from purchase_orders import (
    list_warehouse_orders,
    approve_warehouse_order,
    reject_warehouse_order,
    complete_warehouse_order,
)


def render_pending_request_row(r, data, my_name, key_prefix=""):
    """요청중 상태인 이관 요청 한 건을 표시하고 승인/거절 버튼을 처리한다.
    로그인 배너의 "내 요청함"과 재분배 도우미 탭 양쪽에서 재사용한다.
    모든 탭이 매 rerun마다 함께 그려지는 Streamlit 특성상, 같은 요청이 배너와 탭에
    동시에 나타날 수 있어 key_prefix로 위젯 키가 겹치지 않게 한다."""
    req_qty = int(r["qty"])
    c0, c1, c2, c3 = st.columns([1, 4, 1, 1])
    c0.badge("요청중", icon=":material/schedule:", color="orange")
    c1.write(f"{r['item_name']} · {r['from_camp']} → {r['to_camp']} · {fmt_transfer_qty(r)} · 요청자: {r['requested_by']}")
    c1.caption(fmt_request_timeline(r))
    with c2.popover("승인"):
        # 보내는 캠프가 가진 만큼만 보낼 수 있게 승인 수량을 줄일 수 있다 (요청 수량보다 많이는 불가).
        send_qty = st.number_input(
            "보낼 수량", min_value=1, max_value=req_qty, value=req_qty, step=1,
            key=f"{key_prefix}approve_qty_{r['id']}",
        )
        if send_qty < req_qty:
            st.caption(f"요청 {req_qty}개 중 {int(send_qty)}개만 보내요. 요청한 캠프에 조정된 수량으로 알림이 가요.")
        if st.button("승인 확정", key=f"{key_prefix}approve_{r['id']}", type="primary"):
            if not my_name.strip():
                st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
            elif r["from_camp"] in NO_STOCK_ACCOUNTS:
                # 물류창고(박스히어로에서 별도 관리)·본사 재고는 이 앱의 캠프 재고 데이터에 없어서
                # 가용재고 검증을 건너뛴다.
                approve_transfer_request(r["id"], my_name.strip(), send_qty)
                list_transfer_requests.clear()
                list_all_transfer_requests.clear()
                st.rerun()
            else:
                item = find_item_by_code(data, r["item_code"])
                current_qty = item["x"].get(r["from_camp"], [0, 0])[0] if item else 0
                req_df_all = list_transfer_requests(r["item_code"])
                already_out = int(
                    req_df_all[
                        (req_df_all["status"] == "in_transit") & (req_df_all["from_camp"] == r["from_camp"])
                    ]["qty"].sum()
                )
                available = current_qty - already_out
                if available < send_qty:
                    st.error(
                        f"{r['from_camp']}의 가용재고가 부족합니다. (가용 {available}개, 보낼 수량 {int(send_qty)}개)",
                        icon=":material/error:",
                    )
                else:
                    approve_transfer_request(r["id"], my_name.strip(), send_qty)
                    list_transfer_requests.clear()
                    list_all_transfer_requests.clear()
                    st.rerun()
    with c3.popover("거절"):
        reason = st.text_input("거절 사유", key=f"{key_prefix}reject_reason_{r['id']}")
        if st.button("거절 확정", key=f"{key_prefix}reject_{r['id']}", type="primary"):
            if not my_name.strip():
                st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
            elif not reason.strip():
                st.error("거절 사유를 입력해주세요.", icon=":material/error:")
            else:
                reject_transfer_request(r["id"], my_name.strip(), reason.strip())
                list_transfer_requests.clear()
                list_all_transfer_requests.clear()
                st.rerun()


def render_in_transit_request_row(r, data, my_name, key_prefix="", allow_revert=False):
    """이동중 상태인 이관 요청 한 건을 표시하고 입고완료 버튼을 처리한다.
    로그인 배너의 "입고 확인 필요" 목록과 재분배 도우미 탭 양쪽에서 재사용한다.
    물류창고·본사는 이 앱의 캠프 재고 데이터에 없어서, 그쪽이 보내거나 받는 쪽이면
    그쪽의 재고 증감 계산만 건너뛴다 (박스히어로 쪽 실제 재고는 창고에서 수동으로 맞춤).
    allow_revert=True(관리자 전체 현황)이면 잘못 누른 승인을 되돌리는 버튼도 보여준다."""
    if allow_revert:
        c0, c1, c2, c3 = st.columns([1, 4, 1, 1])
    else:
        c0, c1, c2 = st.columns([1, 5, 1])
    c0.badge("이동중", icon=":material/local_shipping:", color="blue")
    c1.write(f"{r['item_name']} · {r['from_camp']} → {r['to_camp']} · {fmt_transfer_qty(r)} · 승인자: {r['approved_by']}")
    c1.caption(fmt_request_timeline(r))
    if allow_revert:
        with c3.popover("승인 취소"):
            st.caption("요청중 상태로 되돌려요. 재고는 바뀌지 않고, 양쪽 캠프에 알림이 가요.")
            if st.button("취소 확정", key=f"{key_prefix}revert_{r['id']}", type="primary"):
                if not my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                else:
                    revert_transfer_approval(r["id"], my_name.strip())
                    list_transfer_requests.clear()
                    list_all_transfer_requests.clear()
                    st.rerun()
    sent_qty = int(r["qty"])
    with c2.popover("입고완료"):
        # 검수 결과 실제로 받은 수량으로 입고처리한다. 승인 수량과 다르면 받은 수량 기준으로
        # 양쪽 재고가 움직이고, 차이는 요청/승인/입고 수량으로 기록에 남는다.
        recv_qty = st.number_input(
            "실제 받은 수량", min_value=1, value=sent_qty, step=1, key=f"{key_prefix}receive_qty_{r['id']}"
        )
        if recv_qty != sent_qty:
            st.caption(f"승인 수량({sent_qty}개)과 달라요. 받은 {int(recv_qty)}개 기준으로 재고가 옮겨지고 차이는 기록돼요.")
        if st.button("입고 확정", key=f"{key_prefix}receive_{r['id']}", type="primary"):
            if not my_name.strip():
                st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
            else:
                item = find_item_by_code(data, r["item_code"])
                try:
                    if r["from_camp"] in NO_STOCK_ACCOUNTS:
                        moved_amt = 0
                    else:
                        moved_amt = deduct_camp_stock(item, r["from_camp"], int(recv_qty))
                except ValueError as e:
                    st.error(str(e), icon=":material/error:")
                else:
                    if r["to_camp"] not in NO_STOCK_ACCOUNTS:
                        add_camp_stock(item, r["to_camp"], int(recv_qty), moved_amt)
                    save_data("inventory", data)
                    complete_transfer_request(r["id"], my_name.strip(), moved_amt, recv_qty)
                    list_transfer_requests.clear()
                    list_all_transfer_requests.clear()
                    st.rerun()


def render_pending_warehouse_order_row(r, my_name, key_prefix=""):
    """요청중 상태인 발주 요청(캠프 -> 물류창고) 한 건을 표시하고 승인/거절 버튼을 처리한다."""
    c0, c1, c2 = st.columns([4, 1, 1])
    reason_suffix = f" · 사유: {r['reason']}" if r.get("reason") else ""
    c0.write(
        f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · "
        f"요청자: {r['requested_by']}{reason_suffix}"
    )
    c0.caption(fmt_request_timeline(r))
    if c1.button("승인", key=f"{key_prefix}wh_po_approve_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            approve_warehouse_order(r["id"], my_name.strip())
            list_warehouse_orders.clear()
            st.rerun()
    if c2.button("거절", key=f"{key_prefix}wh_po_reject_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            reject_warehouse_order(r["id"], my_name.strip())
            list_warehouse_orders.clear()
            st.rerun()


def render_incoming_warehouse_order_row(r, data, my_name, key_prefix=""):
    """승인되어 입고 대기중인 발주 요청(물류창고 -> 캠프) 한 건을 표시하고 입고완료 버튼을 처리한다.
    입고완료는 실제로 물건을 받아 검수까지 마친 캠프가 눌러야 한다."""
    c0, c1, c2 = st.columns([1, 5, 1])
    c0.badge("승인됨", icon=":material/local_shipping:", color="blue")
    c1.write(
        f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · 승인자: {r['approved_by']}"
    )
    c1.caption(fmt_request_timeline(r))
    if c2.button("입고완료", key=f"{key_prefix}po_receive_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            item = find_item_by_code(data, r["item_code"])
            if not item:
                st.error("해당 품목을 현재 재고 데이터에서 찾을 수 없어요.", icon=":material/error:")
            else:
                unit_amt = (item["a"] / item["q"]) if item.get("q") else 0
                amt = round(unit_amt * r["qty"])
                add_camp_stock(item, r["to_camp"], int(r["qty"]), amt)
                save_data("inventory", data)
                complete_warehouse_order(r["id"], my_name.strip(), amt)
                st.success("입고 완료 처리했습니다.", icon=":material/inventory_2:")
                list_warehouse_orders.clear()
                st.rerun()
