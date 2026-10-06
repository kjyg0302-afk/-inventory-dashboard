"""재고이관(창고<>캠프) 탭."""

import streamlit as st

from usage_data import camp_weekly_usage_rate
from purchase_orders import (
    create_warehouse_order,
    list_warehouse_orders,
    approve_warehouse_order,
    reject_warehouse_order,
)
from rendering import render_incoming_warehouse_order_row


def render(ctx):
    data = ctx.data
    usage = ctx.usage
    auth = ctx.auth
    my_login_camp = ctx.my_login_camp

    st.caption(
        "품목을 선택하고 받을 캠프를 지정해서 물류창고에 발주를 요청할 수 있어요. "
        "승인은 \"창고에서 발송 준비\" 단계이며, 박스히어로 실제 재고는 여기서 자동으로 바뀌지 않아요 "
        "(창고 쪽은 수동으로 처리해주세요). 입고완료를 눌러야 캠프 재고에 반영됩니다."
    )

    po_my_name = st.text_input(
        "내 이름", value=st.session_state.get("transfer_my_name", ""), key="po_my_name_input"
    )
    st.session_state["transfer_my_name"] = po_my_name

    po_camp = st.selectbox("받는 캠프", data["campsOrder"], key="po_camp_select")
    st.caption(f"아래에서 담는 품목은 모두 **{po_camp}** 앞으로 발주돼요. 캠프를 바꾸면 그 다음부터 담는 품목에 적용돼요.")

    if "po_cart" not in st.session_state:
        st.session_state["po_cart"] = []

    st.subheader("발주 목록에 담기")
    po_query = st.text_input("발주할 품목명 또는 번호 입력", "", key="po_query")
    po_selected = None
    if po_query.strip():
        q = po_query.strip().lower()
        all_po_candidates = [it for it in data["items"] if q in it["n"].lower() or q in str(it["c"]).lower()]
        po_candidates = all_po_candidates[:15]
        if po_candidates:
            if len(all_po_candidates) > len(po_candidates):
                st.caption(
                    f"검색 결과 {len(all_po_candidates)}건 중 상위 {len(po_candidates)}개만 표시했어요. "
                    "검색어를 더 구체적으로 입력하면 찾는 품목이 더 잘 보여요."
                )
            po_options = {f"{it['n']} ({it['c']})": it for it in po_candidates}
            po_picked_label = st.radio("검색 결과", list(po_options.keys()), key="po_radio")
            po_selected = po_options[po_picked_label]
        else:
            st.info("일치하는 품목이 없습니다.", icon=":material/search_off:")

    if po_selected:
        po_item_code = po_selected["c"]
        po_item_name = po_selected["n"]
        with st.form(f"po_form_{po_item_code}_{po_camp}"):
            st.caption(f"받는 캠프: **{po_camp}**")
            po_camp_avg = camp_weekly_usage_rate(usage, po_item_code, po_camp)
            if po_camp_avg:
                st.caption(f"{po_camp}의 이 품목 주 평균 사용량: {po_camp_avg:g}개/주")
            else:
                st.caption(f"{po_camp}의 이 품목 사용량 데이터가 없어요 (2배 초과 검증을 생략해요).")
            po_qty = st.number_input("발주 수량", min_value=1, step=1, value=1, key=f"po_qty_{po_item_code}")
            po_threshold = po_camp_avg * 2 if po_camp_avg else None
            po_over = po_threshold is not None and po_qty > po_threshold
            po_reason = ""
            if po_over:
                st.warning(
                    f"주 평균 사용량({po_camp_avg:g}개)의 2배({po_threshold:g}개)를 초과하는 발주예요. "
                    "사유를 입력해주세요.",
                    icon=":material/warning:",
                )
                po_reason = st.text_area("발주 사유", key=f"po_reason_{po_item_code}")
            if st.form_submit_button("목록에 추가", icon=":material/add:"):
                if po_over and not po_reason.strip():
                    st.error("주 평균 사용량의 2배를 초과하는 발주는 사유를 입력해야 해요.", icon=":material/error:")
                else:
                    st.session_state["po_cart"].append(
                        {
                            "item_code": po_item_code,
                            "item_name": po_item_name,
                            "camp": po_camp,
                            "qty": int(po_qty),
                            "weekly_avg": po_camp_avg,
                            "reason": po_reason.strip(),
                        }
                    )
                    # 담기에 성공했을 때만 입력칸을 비운다 (검증 실패 시에는 입력한 값이 유지돼야 함)
                    st.session_state.pop(f"po_qty_{po_item_code}", None)
                    st.session_state.pop(f"po_reason_{po_item_code}", None)
                    st.rerun()

    if st.session_state["po_cart"]:
        st.subheader(f"담긴 발주 목록 ({len(st.session_state['po_cart'])}건)")
        for i, row in enumerate(st.session_state["po_cart"]):
            cc0, cc1, cc2 = st.columns([1, 5, 1])
            cc0.badge(str(i + 1), color="gray")
            avg_suffix = f" (주평균 {row['weekly_avg']:g}개)" if row["weekly_avg"] else ""
            reason_suffix = f" · 사유: {row['reason']}" if row["reason"] else ""
            cc1.write(
                f"{row['item_name']} ({row['item_code']}) → {row['camp']} · {row['qty']}개{avg_suffix}{reason_suffix}"
            )
            if cc2.button("삭제", key=f"po_cart_remove_{i}"):
                st.session_state["po_cart"].pop(i)
                st.rerun()

        if st.button("전체 발주 요청 제출", type="primary", icon=":material/send:"):
            if not po_my_name.strip():
                st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
            else:
                for row in st.session_state["po_cart"]:
                    create_warehouse_order(
                        row["item_code"], row["item_name"], row["camp"], row["qty"],
                        row["weekly_avg"], row["reason"], po_my_name.strip(),
                    )
                st.session_state["po_cart"] = []
                st.success("발주 요청을 모두 등록했습니다.", icon=":material/send:")
                list_warehouse_orders.clear()
                st.rerun()

    po_df = list_warehouse_orders()
    can_approve_po = auth["role"] == "admin" or my_login_camp == "물류창고"
    if my_login_camp and my_login_camp != "물류창고" and not po_df.empty:
        # 캠프로 로그인했으면(물류창고 제외) 내 캠프로 오는 발주만 보여준다.
        po_df = po_df[po_df["to_camp"] == my_login_camp]
    po_requested = po_df[po_df["status"] == "requested"] if not po_df.empty else po_df
    po_in_transit = po_df[po_df["status"] == "in_transit"] if not po_df.empty else po_df
    po_done = po_df[po_df["status"].isin(["completed", "rejected"])] if not po_df.empty else po_df

    if not po_requested.empty:
        st.markdown("**요청중**")
        if can_approve_po:
            for _, r in po_requested.iterrows():
                c0, c1, c2, c3, c4 = st.columns([0.5, 1, 4, 1, 1])
                c0.checkbox("", key=f"po_bulk_chk_{r['id']}", label_visibility="collapsed")
                c1.badge("요청중", icon=":material/schedule:", color="orange")
                reason_suffix = f" · 사유: {r['reason']}" if r.get("reason") else ""
                c2.write(
                    f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · "
                    f"요청자: {r['requested_by']}{reason_suffix}"
                )
                if c3.button("승인", key=f"po_approve_{r['id']}"):
                    if not po_my_name.strip():
                        st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                    else:
                        approve_warehouse_order(r["id"], po_my_name.strip())
                        list_warehouse_orders.clear()
                        st.rerun()
                if c4.button("거절", key=f"po_reject_{r['id']}"):
                    if not po_my_name.strip():
                        st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                    else:
                        reject_warehouse_order(r["id"], po_my_name.strip())
                        list_warehouse_orders.clear()
                        st.rerun()

            po_bulk_ids = [
                int(r["id"]) for _, r in po_requested.iterrows() if st.session_state.get(f"po_bulk_chk_{r['id']}")
            ]
            if po_bulk_ids:
                if st.button(
                    f"체크한 {len(po_bulk_ids)}건 일괄 승인", type="primary", icon=":material/done_all:",
                    key="po_bulk_approve_btn",
                ):
                    if not po_my_name.strip():
                        st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                    else:
                        for oid in po_bulk_ids:
                            approve_warehouse_order(oid, po_my_name.strip())
                        st.success(f"{len(po_bulk_ids)}건을 일괄 승인했습니다.", icon=":material/done_all:")
                        list_warehouse_orders.clear()
                        st.rerun()
        else:
            # 승인/거절은 물류창고(또는 관리자)만 할 수 있어서, 캠프 로그인에는 진행 상황만 보여준다.
            for _, r in po_requested.iterrows():
                reason_suffix = f" · 사유: {r['reason']}" if r.get("reason") else ""
                st.caption(
                    f":material/schedule: {r['item_name']} ({r['item_code']}) → {r['to_camp']} · "
                    f"{int(r['qty'])}개 · 요청자: {r['requested_by']}{reason_suffix} (창고 승인 대기중)"
                )

    if not po_in_transit.empty:
        st.markdown("**승인됨 (입고 대기)**")
        for _, r in po_in_transit.iterrows():
            can_receive = auth["role"] == "admin" or my_login_camp == r["to_camp"]
            if can_receive:
                render_incoming_warehouse_order_row(r, data, po_my_name)
            else:
                # 입고완료는 실제로 받는 캠프(또는 관리자)만 할 수 있어서, 그 외에는 진행 상황만 보여준다.
                ic0, ic1 = st.columns([1, 6])
                ic0.badge("승인됨", icon=":material/local_shipping:", color="blue")
                ic1.write(
                    f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · "
                    f"승인자: {r['approved_by']}"
                )

    if not po_done.empty:
        with st.expander(f"완료/거절 내역 ({len(po_done)}건)", icon=":material/history:"):
            for _, r in po_done.iterrows():
                is_done = r["status"] == "completed"
                who = r["received_by"] if is_done else r["approved_by"]
                dc0, dc1 = st.columns([1, 5])
                if is_done:
                    dc0.badge("입고완료", icon=":material/check_circle:", color="green")
                else:
                    dc0.badge("거절", icon=":material/cancel:", color="red")
                reason_suffix = f" · 사유: {r['reason']}" if r.get("reason") else ""
                dc1.caption(
                    f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · {who}{reason_suffix}"
                )
