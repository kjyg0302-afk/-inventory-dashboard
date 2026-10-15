"""구매발주(외부, 내자/외자) 탭 — 관리자 전용 기본 비노출."""

from datetime import date, timedelta

import streamlit as st

from boxhero import get_boxhero_token, fetch_boxhero_items
from vendors import list_vendors, create_vendor, delete_vendor
from procurement import (
    list_vendor_order_items,
    incoming_qty_by_item,
    create_vendor_order,
    set_expected_arrival,
    receive_item,
    cancel_item,
)


def render(ctx):
    data = ctx.data

    st.caption(
        "거래처(공급업체)에서 실제로 물건을 사오는 구매발주를 발주 → 입고예정 → 입고 단계로 추적해요. "
        "재고이관(창고<>캠프)은 회사 내부 이동이고, 이 탭은 회사 바깥에서 사오는 구매예요."
    )

    # ---------------- 재고 요약 (현재고 + 입고예정) ----------------
    st.subheader("재고 요약 (현재고 + 입고예정)")
    incoming_df = incoming_qty_by_item()
    if incoming_df.empty:
        st.caption("입고 대기중인 구매발주가 없어요.")
    else:
        stock_by_sku = {}
        if get_boxhero_token():
            try:
                for it in fetch_boxhero_items():
                    sku = (it.get("sku") or "").strip().upper()
                    if sku:
                        stock_by_sku[sku] = it.get("quantity", 0)
            except Exception:
                pass
        for _, r in incoming_df.iterrows():
            code_u = str(r["item_code"]).strip().upper()
            cur_qty = stock_by_sku.get(code_u, "-")
            incoming_qty = int(r["incoming_qty"])
            total = f"{cur_qty + incoming_qty}개" if isinstance(cur_qty, (int, float)) else "-"
            eta = f" · 가장 빠른 입고예정일 {r['earliest_expected']}" if r["earliest_expected"] else ""
            st.caption(
                f":material/inventory_2: **{r['item_name']}** ({r['item_code']}) · "
                f"현재고 {cur_qty if isinstance(cur_qty, str) else f'{cur_qty}개'} + "
                f"입고예정 {incoming_qty}개 = 합계 {total}{eta}"
            )

    st.divider()

    # ---------------- 거래처 관리 ----------------
    with st.expander("거래처(공급업체) 관리", icon=":material/storefront:"):
        vendors_df = list_vendors()
        if not vendors_df.empty:
            for _, v in vendors_df.iterrows():
                vc0, vc1, vc2 = st.columns([1, 4, 1])
                vc0.badge(v["vendor_type"], color="blue" if v["vendor_type"] == "외자" else "gray")
                contact_suffix = f" · {v['contact']}" if v.get("contact") else ""
                note_suffix = f" · {v['note']}" if v.get("note") else ""
                vc1.write(f"**{v['name']}**{contact_suffix}{note_suffix}")
                if vc2.button("삭제", key=f"vendor_del_{v['id']}"):
                    try:
                        delete_vendor(v["id"])
                    except Exception:
                        st.error("이 거래처로 등록된 발주가 있어서 삭제할 수 없어요.", icon=":material/error:")
                    else:
                        list_vendors.clear()
                        st.rerun()
        else:
            st.caption("등록된 거래처가 없어요. 아래에서 추가해주세요.")

        with st.form("vendor_add_form"):
            vcol1, vcol2 = st.columns(2)
            new_vendor_name = vcol1.text_input("거래처명")
            new_vendor_type = vcol2.selectbox("구분", ["내자", "외자"])
            new_vendor_contact = st.text_input("연락처 (선택)")
            new_vendor_note = st.text_input("메모 (선택)")
            if st.form_submit_button("거래처 추가", icon=":material/add_business:"):
                existing_names = set(vendors_df["name"]) if not vendors_df.empty else set()
                if not new_vendor_name.strip():
                    st.error("거래처명을 입력해주세요.", icon=":material/error:")
                elif new_vendor_name.strip() in existing_names:
                    st.error("이미 등록된 거래처명이에요.", icon=":material/error:")
                else:
                    create_vendor(new_vendor_name.strip(), new_vendor_type, new_vendor_contact.strip(), new_vendor_note.strip())
                    list_vendors.clear()
                    st.success(f"{new_vendor_name} 거래처를 추가했습니다.", icon=":material/check_circle:")
                    st.rerun()

    st.divider()

    # ---------------- 신규 발주 등록 ----------------
    st.subheader("신규 발주 등록")
    vendors_df = list_vendors()
    if vendors_df.empty:
        st.info("먼저 위에서 거래처를 등록해주세요.", icon=":material/storefront:")
        return

    proc_my_name = st.text_input(
        "내 이름", value=st.session_state.get("transfer_my_name", ""), key="proc_my_name_input"
    )
    st.session_state["transfer_my_name"] = proc_my_name

    vendor_options = {f"{v['name']} ({v['vendor_type']})": v["id"] for _, v in vendors_df.iterrows()}
    proc_vendor_label = st.selectbox("거래처", list(vendor_options.keys()), key="proc_vendor_select")
    proc_vendor_id = vendor_options[proc_vendor_label]
    st.caption(f"아래에서 담는 품목은 모두 **{proc_vendor_label}** 앞으로 발주돼요.")

    if "proc_cart" not in st.session_state:
        st.session_state["proc_cart"] = []

    proc_query = st.text_input("발주할 품목명 또는 번호 입력", "", key="proc_query")
    proc_selected = None
    if proc_query.strip():
        q = proc_query.strip().lower()
        all_candidates = [it for it in data["items"] if q in it["n"].lower() or q in str(it["c"]).lower()]
        proc_candidates = all_candidates[:15]
        if proc_candidates:
            if len(all_candidates) > len(proc_candidates):
                st.caption(f"검색 결과 {len(all_candidates)}건 중 상위 {len(proc_candidates)}개만 표시했어요.")
            proc_options = {f"{it['n']} ({it['c']})": it for it in proc_candidates}
            proc_picked_label = st.radio("검색 결과", list(proc_options.keys()), key="proc_radio")
            proc_selected = proc_options[proc_picked_label]
        else:
            st.info("일치하는 품목이 없습니다.", icon=":material/search_off:")

    if proc_selected:
        proc_item_code = proc_selected["c"]
        proc_item_name = proc_selected["n"]
        with st.form(f"proc_form_{proc_item_code}"):
            pcol1, pcol2, pcol3 = st.columns(3)
            proc_qty = pcol1.number_input("발주 수량", min_value=1, step=1, value=1, key=f"proc_qty_{proc_item_code}")
            proc_unit_price = pcol2.number_input("단가 (선택)", min_value=0.0, step=100.0, value=0.0, key=f"proc_price_{proc_item_code}")
            proc_currency = pcol3.selectbox("통화", ["KRW", "USD", "CNY", "JPY", "EUR"], key=f"proc_cur_{proc_item_code}")
            if st.form_submit_button("목록에 추가", icon=":material/add:"):
                st.session_state["proc_cart"].append(
                    {
                        "item_code": proc_item_code,
                        "item_name": proc_item_name,
                        "qty": int(proc_qty),
                        "unit_price": proc_unit_price or None,
                        "currency": proc_currency,
                        "amt": (proc_unit_price * proc_qty) if proc_unit_price else None,
                    }
                )
                st.session_state.pop(f"proc_qty_{proc_item_code}", None)
                st.session_state.pop(f"proc_price_{proc_item_code}", None)
                st.rerun()

    if st.session_state["proc_cart"]:
        st.markdown(f"**담긴 발주 목록 ({len(st.session_state['proc_cart'])}건)**")
        for i, row in enumerate(st.session_state["proc_cart"]):
            cc0, cc1, cc2 = st.columns([1, 5, 1])
            cc0.badge(str(i + 1), color="gray")
            price_suffix = f" · {row['unit_price']:g} {row['currency']}/개" if row["unit_price"] else ""
            cc1.write(f"{row['item_name']} ({row['item_code']}) · {row['qty']}개{price_suffix}")
            if cc2.button("삭제", key=f"proc_cart_remove_{i}"):
                st.session_state["proc_cart"].pop(i)
                st.rerun()

        proc_note = st.text_area("발주 메모 (선택)", key="proc_order_note")
        if st.button("발주 등록", type="primary", icon=":material/send:"):
            if not proc_my_name.strip():
                st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
            else:
                create_vendor_order(
                    proc_vendor_id, proc_my_name.strip(), proc_note.strip(), st.session_state["proc_cart"]
                )
                st.session_state["proc_cart"] = []
                st.success("발주를 등록했습니다.", icon=":material/send:")
                list_vendor_order_items.clear()
                st.rerun()

    st.divider()

    # ---------------- 진행중 발주 ----------------
    st.subheader("진행중 발주")
    ordered_df = list_vendor_order_items(statuses=["ordered"])
    expected_df = list_vendor_order_items(statuses=["expected"])

    if ordered_df.empty and expected_df.empty:
        st.caption("진행중인 발주가 없어요.")

    if not ordered_df.empty:
        st.markdown("**발주 등록됨 (입고예정일 입력 필요)**")
        for _, r in ordered_df.iterrows():
            oc0, oc1, oc2 = st.columns([1, 4, 2])
            oc0.badge("발주", icon=":material/receipt_long:", color="orange")
            oc1.write(f"{r['item_name']} ({r['item_code']}) · {int(r['qty'])}개 · 거래처: {r['vendor_name']} · 요청자: {r['ordered_by']}")
            new_eta = oc2.date_input("입고예정일", value=date.today() + timedelta(days=7), key=f"eta_input_{r['id']}", label_visibility="collapsed")
            if oc2.button("입고예정 등록", key=f"eta_btn_{r['id']}"):
                set_expected_arrival(r["id"], new_eta)
                list_vendor_order_items.clear()
                st.rerun()

    if not expected_df.empty:
        st.markdown("**입고예정**")
        for _, r in expected_df.iterrows():
            is_overdue = r["expected_arrival_date"] and r["expected_arrival_date"] < date.today()
            ec0, ec1 = st.columns([1, 4])
            if is_overdue:
                ec0.badge("지연", icon=":material/warning:", color="red")
            else:
                ec0.badge("입고예정", icon=":material/local_shipping:", color="blue")
            ec1.write(
                f"{r['item_name']} ({r['item_code']}) · {int(r['qty'])}개 · 거래처: {r['vendor_name']} · "
                f"입고예정일: {r['expected_arrival_date']}"
            )
            with st.form(f"receive_form_{r['id']}"):
                rc1, rc2, rc3 = st.columns([2, 1, 1])
                recv_qty = rc1.number_input(
                    "수령 수량", min_value=0, step=1, value=int(r["qty"]), key=f"recv_qty_{r['id']}"
                )
                if rc2.form_submit_button("입고처리", icon=":material/check_circle:"):
                    if not proc_my_name.strip():
                        st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                    else:
                        receive_item(r["id"], recv_qty, proc_my_name.strip())
                        list_vendor_order_items.clear()
                        st.rerun()
                if rc3.form_submit_button("취소", icon=":material/cancel:"):
                    cancel_item(r["id"])
                    list_vendor_order_items.clear()
                    st.rerun()

    # ---------------- 완료/취소 내역 ----------------
    done_df = list_vendor_order_items(statuses=["received", "cancelled"])
    if not done_df.empty:
        with st.expander(f"완료/취소 내역 ({len(done_df)}건)", icon=":material/history:"):
            for _, r in done_df.iterrows():
                is_done = r["status"] == "received"
                dc0, dc1 = st.columns([1, 5])
                if is_done:
                    dc0.badge("입고완료", icon=":material/check_circle:", color="green")
                    dc1.caption(
                        f"{r['item_name']} ({r['item_code']}) · {int(r['received_qty'])}개 수령 · "
                        f"거래처: {r['vendor_name']} · 확인자: {r['received_by']}"
                    )
                else:
                    dc0.badge("취소", icon=":material/cancel:", color="red")
                    dc1.caption(f"{r['item_name']} ({r['item_code']}) · {int(r['qty'])}개 · 거래처: {r['vendor_name']}")
