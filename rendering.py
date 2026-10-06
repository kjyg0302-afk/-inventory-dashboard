"""이관/발주 요청 한 건을 표시하고 승인/거절/입고완료 버튼을 처리하는 공용 UI 행.
로그인 배너와 rebalance/purchase 탭 양쪽에서 재사용한다."""

import streamlit as st

from stock_ops import find_item_by_code, deduct_camp_stock, add_camp_stock
from app_data import save_data
from transfers import (
    list_transfer_requests,
    list_all_transfer_requests,
    approve_transfer_request,
    reject_transfer_request,
    complete_transfer_request,
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
    c0, c1, c2, c3 = st.columns([1, 4, 1, 1])
    c0.badge("요청중", icon=":material/schedule:", color="orange")
    c1.write(f"{r['item_name']} · {r['from_camp']} → {r['to_camp']} · {int(r['qty'])}개 · 요청자: {r['requested_by']}")
    if c2.button("승인", key=f"{key_prefix}approve_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        elif r["from_camp"] == "물류창고":
            # 물류창고 재고는 이 앱의 캠프 재고 데이터에 없어서(박스히어로 쪽에서 별도 관리)
            # 가용재고 검증을 건너뛴다.
            approve_transfer_request(r["id"], my_name.strip())
            st.success("승인했습니다. 이동중 상태로 전환됩니다.", icon=":material/task_alt:")
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
            if available < r["qty"]:
                st.error(
                    f"{r['from_camp']}의 가용재고가 부족합니다. (가용 {available}개, 요청 {int(r['qty'])}개)",
                    icon=":material/error:",
                )
            else:
                approve_transfer_request(r["id"], my_name.strip())
                st.success("승인했습니다. 이동중 상태로 전환됩니다.", icon=":material/task_alt:")
                list_transfer_requests.clear()
                list_all_transfer_requests.clear()
                st.rerun()
    if c3.button("거절", key=f"{key_prefix}reject_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            reject_transfer_request(r["id"], my_name.strip())
            list_transfer_requests.clear()
            list_all_transfer_requests.clear()
            st.rerun()


def render_in_transit_request_row(r, data, my_name, key_prefix=""):
    """이동중 상태인 이관 요청 한 건을 표시하고 입고완료 버튼을 처리한다.
    로그인 배너의 "입고 확인 필요" 목록과 재분배 도우미 탭 양쪽에서 재사용한다.
    물류창고는 이 앱의 캠프 재고 데이터에 없어서, 물류창고가 보내거나 받는 쪽이면
    그쪽의 재고 증감 계산만 건너뛴다 (박스히어로 쪽 실제 재고는 창고에서 수동으로 맞춤)."""
    c0, c1, c2 = st.columns([1, 5, 1])
    c0.badge("이동중", icon=":material/local_shipping:", color="blue")
    c1.write(f"{r['item_name']} · {r['from_camp']} → {r['to_camp']} · {int(r['qty'])}개 · 승인자: {r['approved_by']}")
    if c2.button("입고완료", key=f"{key_prefix}receive_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            item = find_item_by_code(data, r["item_code"])
            try:
                if r["from_camp"] == "물류창고":
                    moved_amt = 0
                else:
                    moved_amt = deduct_camp_stock(item, r["from_camp"], int(r["qty"]))
            except ValueError as e:
                st.error(str(e), icon=":material/error:")
            else:
                if r["to_camp"] != "물류창고":
                    add_camp_stock(item, r["to_camp"], int(r["qty"]), moved_amt)
                save_data("inventory", data)
                complete_transfer_request(r["id"], my_name.strip(), moved_amt)
                st.success("입고 완료 처리했습니다.", icon=":material/inventory_2:")
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
