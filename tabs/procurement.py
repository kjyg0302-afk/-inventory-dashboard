"""구매발주(외부, 내자/외자) 탭 — 관리자 전용 기본 비노출."""

from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from boxhero import get_boxhero_token, fetch_boxhero_items
from formatting import KST
from vendors import list_vendors, create_vendor, delete_vendor
from procurement import (
    list_vendor_order_items,
    list_vendor_orders,
    list_po_lines,
    incoming_qty_by_item,
    create_vendor_order,
    po_number_exists,
    set_expected_arrival,
    receive_item,
    cancel_item,
)
from po_catalog import load_latest_template, load_catalog, save_po_template, search_catalog, next_po_number
from po_document import build_po_xlsx

CURRENCIES = ["USD", "KRW", "CNY", "JPY", "EUR"]
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _nn(v):
    """NaN/NaT/None/빈 문자열을 None으로."""
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return None if isinstance(v, str) and not v.strip() else v


def _as_date(v):
    v = _nn(v)
    return pd.Timestamp(v).date() if v is not None else None


def _label(name_ko, desc_en, package_no):
    return _nn(name_ko) or _nn(desc_en) or package_no


def _cart_line(item_code, item_name, package_no, desc_en, mpq, qty, price, currency):
    qty = int(qty)
    price = float(price) if price else None
    return {
        "item_code": item_code, "item_name": item_name, "package_no": package_no, "desc_en": desc_en,
        "mpq": float(mpq) if mpq else None, "qty": qty, "unit_price": price,
        "amt": round(qty * price, 4) if price else None, "currency": currency,
    }


def _dday(due, today, done):
    if due is None or done:
        return ""
    days = (due - today).days
    if days > 0:
        return f" (D-{days})"
    if days == 0:
        return " (D-day)"
    return f" (납기 {-days}일 지남)"


def _render_po_list(today):
    po_df = list_vendor_orders()
    if po_df.empty:
        st.caption("아직 만든 PO가 없어요.")
        return
    tpl = load_latest_template()
    lines_df = list_po_lines()
    for _, o in po_df.iterrows():
        n_lines = int(o["line_count"])
        closed = int(o["received_lines"]) + int(o["cancelled_lines"])
        done = n_lines > 0 and closed >= n_lines
        po_label = o["po_number"] if _nn(o["po_number"]) else f"PO번호 없음 (#{o['id']})"
        po_date = _as_date(o["po_date"]) or pd.Timestamp(o["ordered_at"]).tz_convert(KST).date()
        due = _as_date(o["due_date"])
        cur = _nn(o["currency"]) or "USD"

        p0, p1, p2 = st.columns([1, 6, 1.4])
        if done:
            p0.badge("입고완료", icon=":material/check_circle:", color="green")
        else:
            p0.badge("진행중", icon=":material/pending:", color="blue")
        p1.write(
            f"**{po_label}** · {o['vendor_name']} · 품목 {n_lines}종 · 총 {int(o['total_qty']):,}개 · "
            f"{float(o['total_amt']):,.2f} {cur}"
        )
        p1.caption(
            f"발주 {po_date} · 납기 {due or '-'}{_dday(due, today, done)} · "
            f"입고 {int(o['received_lines'])}/{n_lines} 품목 · 담당 {o['ordered_by']}"
        )
        if tpl is None:
            p2.caption("양식 없음")
            continue
        o_lines = lines_df[lines_df["vendor_order_id"] == o["id"]]
        header = {"po_number": po_label if _nn(o["po_number"]) else f"ORDER-{o['id']}",
                  "po_date": po_date, "due_date": due, "currency": cur}
        doc_lines = [
            {"package_no": _nn(l.package_no) or l.item_code, "name_ko": l.item_name, "desc_en": _nn(l.desc_en),
             "mpq": _nn(l.mpq), "qty": l.qty, "unit_price": _nn(l.unit_price)}
            for l in o_lines.itertuples()
        ]
        p2.download_button(
            "발주서",
            data=lambda h=header, ls=doc_lines, t=tpl[2]: build_po_xlsx(h, ls, t),
            file_name=f"{header['po_number']}.xlsx",
            mime=XLSX_MIME,
            key=f"po_dl_{o['id']}",
            icon=":material/download:",
            on_click="ignore",
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

    # ---------------- 발주서 양식 관리 (관리자) ----------------
    if ctx.auth["role"] == "admin":
        with st.expander("발주서 양식 관리 (관리자 전용)", icon=":material/description:"):
            tpl_info = load_latest_template()
            if tpl_info:
                uploaded = tpl_info[1].astimezone(KST).strftime("%Y-%m-%d %H:%M")
                st.caption(
                    f"현재 양식: **{tpl_info[0]}** · 등록 {uploaded} · 부품 카탈로그 {len(load_catalog()):,}개"
                )
            else:
                st.caption("아직 등록된 양식이 없어요. 회사 발주서 엑셀(PO 시트 + 부품 정보 RAWDATA 시트)을 올려주세요.")
            st.caption(
                "양식에 거래처 단가가 들어 있어서 GitHub가 아닌 앱 DB에만 보관해요. "
                "단가표가 바뀌면 새 양식을 올리면 부품 카탈로그도 같이 바뀌어요."
            )
            tpl_up = st.file_uploader("발주서 양식 엑셀", type=["xlsx"], key="po_template_upload")
            if tpl_up is not None and st.button("양식 등록", key="po_template_save_btn", icon=":material/upload:"):
                try:
                    n = save_po_template(
                        tpl_up.getvalue(), tpl_up.name, st.session_state.get("transfer_my_name") or "관리자"
                    )
                except ValueError as e:
                    st.error(str(e), icon=":material/error:")
                else:
                    st.success(f"양식을 등록했어요. 부품 카탈로그 {n:,}개를 불러왔어요.", icon=":material/check_circle:")

    st.divider()

    # ---------------- 신규 발주(PO) 등록 ----------------
    st.subheader("신규 발주(PO) 등록")
    vendors_df = list_vendors()
    if vendors_df.empty:
        st.info("먼저 위에서 거래처를 등록해주세요.", icon=":material/storefront:")
        return

    proc_my_name = st.text_input(
        "내 이름", value=st.session_state.get("transfer_my_name", ""), key="proc_my_name_input"
    )
    st.session_state["transfer_my_name"] = proc_my_name

    vendor_rows = {f"{v['name']} ({v['vendor_type']})": v for _, v in vendors_df.iterrows()}
    proc_vendor_label = st.selectbox("거래처", list(vendor_rows.keys()), key="proc_vendor_select")
    vendor = vendor_rows[proc_vendor_label]
    proc_vendor_id = int(vendor["id"])

    today = datetime.now(KST).date()
    suggested_po = next_po_number(today.year)
    h1, h2, h3, h4 = st.columns(4)
    # 추천 번호가 바뀌면(방금 PO를 만들었으면) 새 기본값이 들어가도록 키에 번호를 넣는다.
    po_number = h1.text_input("PO번호", value=suggested_po, key=f"proc_po_number_{suggested_po}")
    po_date = h2.date_input("발주일", value=today, key="proc_po_date")
    due_date = h3.date_input("납기일", value=today + timedelta(days=30), key="proc_due_date")
    default_cur = "USD" if vendor["vendor_type"] == "외자" else "KRW"
    currency = h4.selectbox(
        "통화", CURRENCIES, index=CURRENCIES.index(default_cur), key=f"proc_currency_{proc_vendor_id}"
    )

    if "proc_cart" not in st.session_state:
        st.session_state["proc_cart"] = []
    cart = st.session_state["proc_cart"]

    st.markdown("**품목 담기**")
    cat_q = st.text_input("부품 검색 (SKU · 한글명 · 나인봇 부품번호 · 영문명)", key="proc_catalog_query")
    if cat_q.strip():
        res = search_catalog(cat_q)
        if res.empty:
            st.info("카탈로그에 일치하는 부품이 없어요. 아래 '직접 입력'을 써주세요.", icon=":material/search_off:")
        else:
            options = {
                f"{_label(r.name_ko, r.desc_en, r.package_no)} · {r.package_no}" + (f" · {r.sku}" if _nn(r.sku) else ""): r
                for r in res.itertuples()
            }
            picked = options[st.radio("검색 결과", list(options.keys()), key="proc_catalog_radio")]
            mpq = _nn(picked.mpq)
            p_low, p_high = _nn(picked.price_low), _nn(picked.price_high)
            st.caption(
                (f"MPQ(포장단위) {mpq:g}개" if mpq else "MPQ 정보 없음")
                + f" · 카탈로그 단가(USD) 1-499개 {p_low if p_low is not None else '-'} / "
                f"500-5000개 {p_high if p_high is not None else '-'}"
            )
            default_price = float(p_high) if (currency == "USD" and p_high is not None) else 0.0
            with st.form(f"proc_cat_form_{picked.package_no}"):
                fc1, fc2 = st.columns(2)
                qty = fc1.number_input(
                    "수량(Pcs)", min_value=1, step=int(mpq) if mpq else 1, value=int(mpq) if mpq else 1,
                    key=f"proc_cat_qty_{picked.package_no}",
                )
                price = fc2.number_input(
                    f"단가 ({currency})", min_value=0.0, value=default_price, step=0.01, format="%.4f",
                    key=f"proc_cat_price_{picked.package_no}_{currency}",
                )
                if st.form_submit_button("목록에 추가", icon=":material/add:"):
                    cart.append(_cart_line(
                        item_code=_nn(picked.sku) or picked.package_no,
                        item_name=_label(picked.name_ko, picked.desc_en, picked.package_no),
                        package_no=picked.package_no, desc_en=_nn(picked.desc_en), mpq=mpq,
                        qty=qty, price=price, currency=currency,
                    ))
                    st.rerun()

    with st.expander("카탈로그에 없는 품목 직접 입력", icon=":material/edit_note:"):
        with st.form("proc_manual_form"):
            m1, m2 = st.columns(2)
            m_code = m1.text_input("품목코드 (거래처 부품번호 또는 SKU)")
            m_name = m2.text_input("품명")
            m3, m4 = st.columns(2)
            m_qty = m3.number_input("수량", min_value=1, step=1, value=1)
            m_price = m4.number_input(f"단가 ({currency})", min_value=0.0, step=0.01, format="%.4f")
            if st.form_submit_button("목록에 추가", icon=":material/add:"):
                if not m_code.strip() or not m_name.strip():
                    st.error("품목코드와 품명을 입력해주세요.", icon=":material/error:")
                else:
                    cart.append(_cart_line(
                        item_code=m_code.strip(), item_name=m_name.strip(), package_no=m_code.strip(),
                        desc_en=None, mpq=None, qty=m_qty, price=m_price, currency=currency,
                    ))
                    st.rerun()

    if cart:
        st.markdown(f"**담긴 품목 ({len(cart)}건)**")
        if any(line["currency"] != currency for line in cart):
            st.warning("통화를 바꿨어요. 이미 담긴 품목의 단가는 처음 담을 때 통화 기준이에요.", icon=":material/warning:")
        total_amt = 0.0
        for i, line in enumerate(cart):
            cc0, cc1, cc2 = st.columns([1, 6, 1])
            cc0.badge(str(i + 1), color="gray")
            pack = f" ({line['qty'] / line['mpq']:g}팩)" if line["mpq"] else ""
            price_txt = f" · 단가 {line['unit_price']:g} · 금액 {line['amt']:,.2f} {line['currency']}" if line["unit_price"] else ""
            cc1.write(f"{line['item_name']} · {line['package_no']} · {line['qty']:,}개{pack}{price_txt}")
            if line["mpq"] and line["qty"] % line["mpq"]:
                cc1.caption(f":material/info: MPQ({line['mpq']:g}개) 배수가 아니에요.")
            if cc2.button("삭제", key=f"proc_cart_remove_{i}"):
                cart.pop(i)
                st.rerun()
            total_amt += line["amt"] or 0
        st.caption(f"합계 {sum(l['qty'] for l in cart):,}개 · {total_amt:,.2f} {currency}")

        proc_note = st.text_area("발주 메모 (선택)", key="proc_order_note")
        if st.button("PO 생성", type="primary", icon=":material/receipt_long:"):
            po_number = po_number.strip()
            if not proc_my_name.strip():
                st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
            elif not po_number:
                st.error("PO번호를 입력해주세요.", icon=":material/error:")
            elif po_number_exists(po_number):
                st.error(f"{po_number}는 이미 있는 PO번호예요.", icon=":material/error:")
            else:
                create_vendor_order(
                    proc_vendor_id, proc_my_name.strip(), proc_note.strip(), cart,
                    po_number, po_date, due_date, currency,
                )
                st.session_state["proc_cart"] = []
                st.session_state["proc_last_po"] = po_number
                list_vendor_order_items.clear()
                list_vendor_orders.clear()
                list_po_lines.clear()
                st.rerun()

    if st.session_state.get("proc_last_po"):
        st.success(
            f"{st.session_state['proc_last_po']}를 만들었어요. 아래 PO 목록에서 발주서를 내려받을 수 있어요.",
            icon=":material/check_circle:",
        )

    st.divider()

    # ---------------- PO 목록 ----------------
    st.subheader("PO 목록")
    _render_po_list(today)

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
            po_prefix = f"[{r['po_number']}] " if _nn(r.get("po_number")) else ""
            oc1.write(f"{po_prefix}{r['item_name']} ({r['item_code']}) · {int(r['qty'])}개 · 거래처: {r['vendor_name']} · 요청자: {r['ordered_by']}")
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
