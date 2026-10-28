"""구매·PO 관리 탭 (외부 구매, 내자/외자) — 관리자 전용 기본 비노출.

구매 E2E 엑셀에서 가져온 PO 라인과 앱에서 만든 PO를 같이 보여준다: 미입고·PO 목록(상세·입고 등록)·
품목별 구매 이력·공급사별 요약·신규 PO 생성·(관리자) 엑셀 재업로드/발주서 양식."""

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from boxhero import get_boxhero_token, fetch_boxhero_items
from formatting import KST
from vendors import list_vendors, create_vendor, delete_vendor
from procurement import (
    STATUS_DONE,
    load_purchase_lines,
    list_receipts,
    add_receipt,
    delete_app_receipt,
    po_exists,
    create_purchase_order,
    doc_po_number,
    next_po_number,
)
from purchase_import import import_e2e
from po_catalog import (
    load_latest_template, load_catalog, save_po_template, search_catalog, template_last_po_number,
)
from po_document import build_po_xlsx

CURRENCIES = ["USD", "KRW", "CNY", "JPY", "EUR"]
DESTINATIONS = ["대구", "한국", "태국", "LA", "MEMPHIS", "GUAM", "베트남", "Alexandria"]
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
VIEWS = ["미입고·요약", "PO 목록", "품목별 이력", "공급사별", "신규 PO"]
STATUS_COLORS = {STATUS_DONE: "green", "부분 입고": "orange", "생산 중": "blue", "발주": "gray"}


def _nn(v):
    """NaN/NaT/None/빈 문자열을 None으로."""
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return None if isinstance(v, str) and not v.strip() else v


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


def _amount_by_currency(df):
    """{'USD': 1234.5, ...} -> '1,234.50 USD · 300.00 CNY'"""
    s = df.dropna(subset=["amount"]).groupby(df["currency"].fillna("?"))["amount"].sum()
    return " · ".join(f"{v:,.2f} {k}" for k, v in s.sort_values(ascending=False).items()) or "-"


def _my_name():
    name = st.text_input("내 이름", value=st.session_state.get("transfer_my_name", ""), key="proc_my_name_input")
    st.session_state["transfer_my_name"] = name
    return name.strip()


def _line_table(df, extra_cols=()):
    cols = {
        "po_no": "PO", "line_no": "관리번호", "supplier": "공급사", "item_name": "품명", "sku": "SKU",
        "mfr_pn": "제조사 PN", "qty": "발주", "received_qty": "입고", "open_qty": "잔량", "status": "상태",
        "po_approved_on": "PO 승인일", "contract_due": "계약 납기", "last_received_on": "최근 입고일",
        "destination": "입고처", **dict(extra_cols),
    }
    out = df[list(cols)].rename(columns=cols)
    for c in ["PO 승인일", "계약 납기", "최근 입고일"]:
        out[c] = out[c].dt.date
    return out


# ---------------------------------------------------------------- 미입고·요약
def _render_open(lines, today):
    open_lines = lines[lines["open_qty"] > 0]
    this_year = lines[lines["po_year"] == today.year]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("진행 중 PO", f"{open_lines['po_no'].nunique():,}건", border=True)
    k2.metric("미입고", f"{len(open_lines):,}줄 · {open_lines['open_qty'].sum():,.0f}개", border=True)
    k3.metric("계약 납기 지난 미입고", f"{int(open_lines['overdue'].sum()):,}줄", border=True)
    k4.metric(f"{today.year}년 발주 PO", f"{this_year['po_no'].nunique():,}건", border=True)
    st.caption(f"{today.year}년 발주 금액: {_amount_by_currency(this_year)}")

    st.markdown("**미입고 목록**")
    if open_lines.empty:
        st.caption("미입고 줄이 없어요.")
    else:
        t = open_lines.assign(경과일=(pd.Timestamp(today) - open_lines["po_approved_on"]).dt.days)
        t = t.sort_values(["overdue", "po_approved_on"], ascending=[False, True])
        table = _line_table(t, [("경과일", "PO 후 경과일"), ("overdue", "납기 지연")])
        st.dataframe(table, hide_index=True, width="stretch")
        st.caption("상태 '생산 중'은 엑셀에 적혀 있던 진행 상태예요. 입고를 등록하면 수량 기준으로 바뀌어요.")

    with st.expander("국내 재고 요약 (박스히어로 현재고 + 입고예정)", icon=":material/inventory_2:"):
        dom = open_lines[open_lines["domestic"]]
        if dom.empty:
            st.caption("국내(대구·한국) 입고 예정인 미입고가 없어요.")
            return
        stock = {}
        if get_boxhero_token():
            try:
                stock = {(it.get("sku") or "").strip().upper(): it.get("quantity", 0) for it in fetch_boxhero_items()}
            except Exception:
                st.caption("박스히어로 현재고를 불러오지 못했어요.")
        key = dom["sku"].fillna(dom["mfr_pn"]).fillna(dom["item_name"])
        g = dom.assign(key=key).groupby("key", as_index=False).agg(
            품명=("item_name", "last"), 입고예정=("open_qty", "sum"), PO=("po_no", lambda s: ", ".join(sorted(set(s.dropna())))),
            가장_이른_납기=("contract_due", "min"),
        )
        g["현재고"] = g["key"].str.upper().map(stock)
        g["합계"] = g["현재고"] + g["입고예정"]
        g["가장_이른_납기"] = g["가장_이른_납기"].dt.date
        g = g.rename(columns={"key": "SKU / PN", "가장_이른_납기": "가장 이른 계약 납기"})
        st.dataframe(g[["SKU / PN", "품명", "현재고", "입고예정", "합계", "가장 이른 계약 납기", "PO"]],
                     hide_index=True, width="stretch")
        st.caption("입고처가 대구·한국인 미입고만 더했어요. SKU가 없는 품목은 현재고를 알 수 없어 비어 있어요.")


# ---------------------------------------------------------------- PO 목록 + 상세
def _po_summary(lines):
    g = lines.groupby("po_no", dropna=False)
    po = g.agg(
        공급사=("supplier", lambda s: ", ".join(sorted(set(s.dropna()))) or "-"),
        구분=("supply_type", "first"),
        승인일=("po_approved_on", "min"),
        줄=("line_no", "count"),
        발주=("qty", "sum"),
        입고=("received_qty", "sum"),
        잔량=("open_qty", "sum"),
        계약납기=("contract_due", "min"),
        지연=("overdue", "sum"),
        원본=("source", lambda s: "앱" if (s == "app").all() else "엑셀"),
    ).reset_index()
    po["진행률"] = (po["입고"] / po["발주"].where(po["발주"] > 0) * 100).clip(upper=100).fillna(0)
    po["상태"] = po["잔량"].map(lambda x: "진행 중" if x > 0 else "완료")
    po["금액"] = po["po_no"].map(lambda p: _amount_by_currency(lines[lines["po_no"] == p]))
    po["승인일"] = po["승인일"].dt.date
    po["계약납기"] = po["계약납기"].dt.date
    return po.rename(columns={"po_no": "PO번호", "계약납기": "계약 납기", "지연": "납기 지연 줄"}).sort_values(
        "승인일", ascending=False, na_position="last"
    )


def _render_po_list(lines, today):
    f1, f2, f3, f4 = st.columns([1, 1.4, 1, 1.6])
    years = sorted(lines["po_year"].dropna().unique().tolist(), reverse=True)
    year = f1.selectbox("연도", ["전체"] + years, key="proc_po_year")
    supplier = f2.selectbox("공급사", ["전체"] + sorted(lines["supplier"].dropna().unique()), key="proc_po_supplier")
    state = f3.selectbox("상태", ["전체", "진행 중", "완료"], key="proc_po_state")
    q = f4.text_input("검색 (PO번호 · 품명 · SKU · PN)", key="proc_po_q").strip().lower()

    sel = lines
    if year != "전체":
        sel = sel[sel["po_year"] == year]
    if supplier != "전체":
        sel = sel[sel["supplier"] == supplier]
    if q:
        hay = (sel["po_no"].fillna("") + " " + sel["item_name"].fillna("") + " " + sel["sku"].fillna("") + " "
               + sel["mfr_pn"].fillna("")).str.lower()
        sel = sel[sel["po_no"].isin(sel.loc[hay.str.contains(q, regex=False), "po_no"])]
    po = _po_summary(sel)
    if state != "전체":
        po = po[po["상태"] == state]
    if po.empty:
        st.caption("조건에 맞는 PO가 없어요.")
        return

    st.caption(f"PO {len(po):,}건 · 표에서 PO를 고르면 아래에 상세와 입고 등록이 나와요.")
    event = st.dataframe(
        po[["PO번호", "상태", "공급사", "구분", "승인일", "계약 납기", "줄", "발주", "입고", "잔량", "진행률",
            "납기 지연 줄", "금액", "원본"]],
        hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="proc_po_table",
        column_config={"진행률": st.column_config.ProgressColumn("입고율", format="%.0f%%", min_value=0, max_value=100)},
    )
    if event.selection.rows:
        po_no = po.iloc[event.selection.rows[0]]["PO번호"]
        st.divider()
        _render_po_detail(lines, po_no, today)


def _render_po_detail(lines, po_no, today):
    po_lines = lines[lines["po_no"] == po_no] if _nn(po_no) else lines[lines["po_no"].isna()]
    first = po_lines.iloc[0]
    v = lambda col: _nn(first[col]) or "-"  # noqa: E731
    h1, h2 = st.columns([5, 1.2])
    h1.markdown(f"### {_nn(po_no) or 'PO번호 없음'}")
    h1.caption(
        f"{v('supplier')} · {v('supply_type')} · PI {v('pi_no')} · "
        f"PO 승인 {first['po_approved_on'].date() if pd.notna(first['po_approved_on']) else '-'} · "
        f"PR {v('pr_no')} ({v('pr_dept')}) · 담당 {v('buyer')} · 금액 {_amount_by_currency(po_lines)}"
    )
    tpl = load_latest_template()
    if tpl is not None and _nn(po_no):
        catalog = load_catalog().set_index("package_no")
        doc_lines = []
        for ln in po_lines.itertuples():
            cat = catalog.loc[ln.mfr_pn] if _nn(ln.mfr_pn) in catalog.index else None
            doc_lines.append({
                "package_no": _nn(ln.mfr_pn) or ln.sku, "name_ko": ln.item_name,
                "desc_en": _nn(cat["desc_en"]) if cat is not None else None,
                "mpq": _nn(cat["mpq"]) if cat is not None else None,
                "qty": ln.qty, "unit_price": _nn(ln.unit_price),
            })
        header = {
            "po_number": doc_po_number(po_no),
            "po_date": first["po_approved_on"].date() if pd.notna(first["po_approved_on"]) else today,
            "due_date": po_lines["contract_due"].min().date() if po_lines["contract_due"].notna().any() else None,
            "currency": _nn(first["currency"]) or "USD",
        }
        h2.download_button(
            "발주서", data=lambda h=header, ls=doc_lines, t=tpl[2]: build_po_xlsx(h, ls, t),
            file_name=f"{header['po_number']}.xlsx", mime=XLSX_MIME, key=f"po_dl_{po_no}",
            icon=":material/download:", on_click="ignore",
        )

    detail = _line_table(po_lines, [("unit_price", "단가"), ("currency", "통화"), ("bl_no", "BL번호"),
                                    ("pay_status", "지급 상태")])
    st.dataframe(detail.drop(columns=["PO", "공급사"]), hide_index=True, width="stretch")

    receipts = list_receipts(po_lines["id"].tolist())
    with st.expander(f"입고 기록 ({len(receipts)}건)", icon=":material/history:"):
        if receipts.empty:
            st.caption("아직 입고 기록이 없어요.")
        for r in receipts.itertuples():
            c0, c1, c2 = st.columns([1, 6, 1])
            c0.badge("앱" if r.source == "app" else "엑셀", color="blue" if r.source == "app" else "gray")
            c1.write(f"{r.line_no} · {r.item_name} · **{float(r.qty):,.0f}개** · {r.received_on or '날짜 없음'} · "
                     f"{r.location or '-'}" + (f" · 등록 {r.entered_by}" if r.source == "app" else ""))
            if r.source == "app" and c2.button("취소", key=f"rcpt_del_{r.id}"):
                delete_app_receipt(r.id)
                st.rerun()

    open_lines = po_lines[po_lines["open_qty"] > 0]
    if open_lines.empty:
        st.success("이 PO는 모두 입고됐어요.", icon=":material/check_circle:")
        return
    st.markdown("**입고 등록**")
    options = {f"{r.line_no} · {r.item_name} · 잔량 {r.open_qty:,.0f}개": r for r in open_lines.itertuples()}
    picked = options[st.selectbox("입고할 품목", list(options), key=f"rcpt_line_{po_no}")]
    with st.form(f"rcpt_form_{po_no}_{picked.id}"):
        name = _my_name()
        r1, r2, r3 = st.columns(3)
        qty = r1.number_input("입고 수량", min_value=1, value=int(picked.open_qty), step=1)
        rdate = r2.date_input("입고일", value=today)
        dest = _nn(picked.destination) or "대구"
        loc = r3.selectbox("입고처", DESTINATIONS if dest in DESTINATIONS else [dest] + DESTINATIONS,
                           index=DESTINATIONS.index(dest) if dest in DESTINATIONS else 0)
        if st.form_submit_button("입고 등록", type="primary", icon=":material/move_to_inbox:"):
            if not name:
                st.error("내 이름을 입력해주세요.", icon=":material/error:")
            else:
                add_receipt(picked.id, qty, rdate, loc, name)
                if qty > picked.open_qty:
                    st.toast(f"발주보다 {qty - picked.open_qty:,.0f}개 더 입고됐어요.", icon=":material/info:")
                st.rerun()
    st.caption("일부만 들어왔으면 들어온 수량만 등록하세요. 남은 수량은 미입고로 남고 다음에 또 등록할 수 있어요.")


# ---------------------------------------------------------------- 품목별 이력
def _render_item_history(lines):
    q = st.text_input("품목 검색 (SKU · 제조사 PN · 품명)", key="proc_item_q").strip().lower()
    if not q:
        st.caption("SKU, 나인봇 부품번호(AB.xx) 또는 품명 일부를 입력하세요.")
        return
    hay = (lines["sku"].fillna("") + " " + lines["mfr_pn"].fillna("") + " " + lines["item_name"].fillna("")).str.lower()
    hits = lines[hay.str.contains(q, regex=False)]
    if hits.empty:
        st.caption("구매 이력이 없어요.")
        return
    hits = hits.assign(key=hits["sku"].fillna(hits["mfr_pn"]).fillna(hits["item_name"]))
    keys = hits.groupby("key").agg(item=("item_name", "last"), n=("line_no", "count")).sort_values("n", ascending=False)
    if len(keys) > 1:
        labels = {f"{k} · {r.item} ({r.n}회)": k for k, r in keys.iterrows()}
        key = labels[st.selectbox(f"일치하는 품목 {len(keys)}개", list(labels), key="proc_item_pick")]
    else:
        key = keys.index[0]
    h = hits[hits["key"] == key].sort_values("po_approved_on")

    priced = h.dropna(subset=["unit_price"])
    last = priced.iloc[-1] if not priced.empty else None
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("발주 횟수", f"{len(h):,}회", border=True)
    k2.metric("총 발주 / 입고", f"{h['qty'].sum():,.0f} / {h['received_qty'].sum():,.0f}", border=True)
    k3.metric("최근 단가", f"{last['unit_price']:g} {last['currency']}" if last is not None else "-", border=True)
    k4.metric("평균 리드타임", f"{h['lead_days'].mean():.0f}일" if h["lead_days"].notna().any() else "-", border=True)
    st.caption("공급사: " + ", ".join(sorted(set(h["supplier"].dropna()))))
    if len(priced) > 1:
        for cur, g in priced.groupby("currency"):
            st.caption(f"{cur} 단가: 최저 {g['unit_price'].min():g} · 평균 {g['unit_price'].mean():.3g} · "
                       f"최고 {g['unit_price'].max():g}")
    table = _line_table(h.iloc[::-1], [("unit_price", "단가"), ("currency", "통화"), ("lead_days", "리드타임(일)")])
    st.dataframe(table, hide_index=True, width="stretch")


# ---------------------------------------------------------------- 공급사별
def _render_suppliers(lines):
    g = lines.groupby("supplier").agg(
        구분=("supply_type", "first"), PO=("po_no", "nunique"), 줄=("line_no", "count"),
        발주=("qty", "sum"), 입고=("received_qty", "sum"), 잔량=("open_qty", "sum"),
        평균_리드타임=("lead_days", "mean"), 최근_발주=("po_approved_on", "max"),
    )
    amt = lines.pivot_table(index="supplier", columns="currency", values="amount", aggfunc="sum")
    amt.columns = [f"금액 {c}" for c in amt.columns]
    g = g.join(amt).reset_index().sort_values("최근_발주", ascending=False)
    g["최근_발주"] = g["최근_발주"].dt.date
    g = g.rename(columns={"supplier": "공급사", "평균_리드타임": "평균 리드타임(일)", "최근_발주": "최근 발주"})
    st.dataframe(g, hide_index=True, width="stretch", column_config={
        "평균 리드타임(일)": st.column_config.NumberColumn(format="%.0f"),
        **{c: st.column_config.NumberColumn(format="localized") for c in g.columns if c.startswith("금액")},
    })

    st.markdown("**연도별 발주 금액**")
    yearly = lines.dropna(subset=["po_year"]).pivot_table(
        index="po_year", columns="currency", values="amount", aggfunc="sum"
    ).sort_index(ascending=False)
    yearly.index = yearly.index.astype(int).astype(str)
    st.dataframe(yearly.rename_axis("연도"), width="stretch",
                 column_config={c: st.column_config.NumberColumn(format="localized") for c in yearly.columns})
    st.caption("리드타임 = 다 입고된 줄의 마지막 입고일 − PO 승인일. 옛 엑셀 기록은 입고일이 없는 줄이 많아 빠져요.")


# ---------------------------------------------------------------- 신규 PO
def _render_new_po(today):
    with st.expander("거래처(공급업체) 관리", icon=":material/storefront:"):
        vendors_df = list_vendors()
        for _, v in vendors_df.iterrows():
            vc0, vc1, vc2 = st.columns([1, 4, 1])
            vc0.badge(v["vendor_type"], color="blue" if v["vendor_type"] == "외자" else "gray")
            extra = "".join(f" · {v[k]}" for k in ("contact", "note") if _nn(v.get(k)))
            vc1.write(f"**{v['name']}**{extra}")
            if vc2.button("삭제", key=f"vendor_del_{v['id']}"):
                delete_vendor(v["id"])
                list_vendors.clear()
                st.rerun()
        with st.form("vendor_add_form"):
            vcol1, vcol2 = st.columns(2)
            new_name = vcol1.text_input("거래처명")
            new_type = vcol2.selectbox("구분", ["외자", "내자"])
            new_contact = st.text_input("연락처 (선택)")
            new_note = st.text_input("메모 (선택)")
            if st.form_submit_button("거래처 추가", icon=":material/add_business:"):
                if not new_name.strip():
                    st.error("거래처명을 입력해주세요.", icon=":material/error:")
                elif new_name.strip() in set(vendors_df["name"]):
                    st.error("이미 등록된 거래처명이에요.", icon=":material/error:")
                else:
                    create_vendor(new_name.strip(), new_type, new_contact.strip(), new_note.strip())
                    list_vendors.clear()
                    st.rerun()

    vendors_df = list_vendors()
    if vendors_df.empty:
        st.info("먼저 거래처를 등록해주세요.", icon=":material/storefront:")
        return
    my_name = _my_name()
    vendor_rows = {f"{v['name']} ({v['vendor_type']})": v for _, v in vendors_df.iterrows()}
    default_vendor = next((i for i, k in enumerate(vendor_rows) if k.startswith("Segway Discovery")), 0)
    vendor = vendor_rows[st.selectbox("거래처", list(vendor_rows), index=default_vendor, key="proc_vendor_select")]

    suggested_po = next_po_number(today.year, template_last_po_number())
    h1, h2, h3, h4, h5 = st.columns(5)
    # 추천 번호가 바뀌면(방금 PO를 만들었으면) 새 기본값이 들어가도록 키에 번호를 넣는다.
    po_number = h1.text_input("PO번호", value=suggested_po, key=f"proc_po_number_{suggested_po}")
    po_date = h2.date_input("발주일", value=today, key="proc_po_date")
    due_date = h3.date_input("계약 납기", value=today + timedelta(days=60), key="proc_due_date")
    default_cur = "USD" if vendor["vendor_type"] == "외자" else "KRW"
    currency = h4.selectbox("통화", CURRENCIES, index=CURRENCIES.index(default_cur), key=f"proc_currency_{vendor['id']}")
    destination = h5.selectbox("입고처", DESTINATIONS, key="proc_destination")
    st.caption(f"발주서에는 {doc_po_number(po_number.strip())}로 찍혀요. 원장(엑셀)과 같은 연도PO_번호 체계예요.")

    cart = st.session_state.setdefault("proc_cart", [])
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
                qty = fc1.number_input("수량(Pcs)", min_value=1, step=int(mpq) if mpq else 1,
                                       value=int(mpq) if mpq else 1, key=f"proc_cat_qty_{picked.package_no}")
                price = fc2.number_input(f"단가 ({currency})", min_value=0.0, value=default_price, step=0.01,
                                         format="%.4f", key=f"proc_cat_price_{picked.package_no}_{currency}")
                if st.form_submit_button("목록에 추가", icon=":material/add:"):
                    cart.append(_cart_line(
                        item_code=_nn(picked.sku), item_name=_label(picked.name_ko, picked.desc_en, picked.package_no),
                        package_no=picked.package_no, desc_en=_nn(picked.desc_en), mpq=mpq,
                        qty=qty, price=price, currency=currency,
                    ))
                    st.rerun()

    with st.expander("카탈로그에 없는 품목 직접 입력", icon=":material/edit_note:"):
        with st.form("proc_manual_form"):
            m1, m2, m3 = st.columns(3)
            m_pn = m1.text_input("제조사 PN (거래처 부품번호)")
            m_sku = m2.text_input("SKU (선택)")
            m_name = m3.text_input("품명")
            m4, m5 = st.columns(2)
            m_qty = m4.number_input("수량", min_value=1, step=1, value=1)
            m_price = m5.number_input(f"단가 ({currency})", min_value=0.0, step=0.01, format="%.4f")
            if st.form_submit_button("목록에 추가", icon=":material/add:"):
                if not m_name.strip() or not (m_pn.strip() or m_sku.strip()):
                    st.error("품명과 제조사 PN(또는 SKU)을 입력해주세요.", icon=":material/error:")
                else:
                    cart.append(_cart_line(
                        item_code=m_sku.strip().upper() or None, item_name=m_name.strip(),
                        package_no=m_pn.strip() or None, desc_en=None, mpq=None, qty=m_qty, price=m_price,
                        currency=currency,
                    ))
                    st.rerun()

    if st.session_state.get("proc_last_po"):
        st.success(f"{st.session_state['proc_last_po']}를 만들었어요. 'PO 목록'에서 발주서를 내려받고 입고를 등록할 수 있어요.",
                   icon=":material/check_circle:")
    if not cart:
        return

    st.markdown(f"**담긴 품목 ({len(cart)}건)**")
    if any(line["currency"] != currency for line in cart):
        st.warning("통화를 바꿨어요. 이미 담긴 품목의 단가는 처음 담을 때 통화 기준이에요.", icon=":material/warning:")
    for i, line in enumerate(cart):
        cc0, cc1, cc2 = st.columns([1, 6, 1])
        cc0.badge(str(i + 1), color="gray")
        pack = f" ({line['qty'] / line['mpq']:g}팩)" if line["mpq"] else ""
        price_txt = f" · 단가 {line['unit_price']:g} · 금액 {line['amt']:,.2f} {line['currency']}" if line["unit_price"] else ""
        cc1.write(f"{line['item_name']} · {line['package_no'] or line['item_code']} · {line['qty']:,}개{pack}{price_txt}")
        if line["mpq"] and line["qty"] % line["mpq"]:
            cc1.caption(f":material/info: MPQ({line['mpq']:g}개) 배수가 아니에요.")
        if cc2.button("삭제", key=f"proc_cart_remove_{i}"):
            cart.pop(i)
            st.rerun()
    st.caption(f"합계 {sum(l['qty'] for l in cart):,}개 · {sum(l['amt'] or 0 for l in cart):,.2f} {currency}")

    note = st.text_area("발주 메모 (선택)", key="proc_order_note")
    if st.button("PO 생성", type="primary", icon=":material/receipt_long:"):
        po_number = po_number.strip()
        if not my_name:
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        elif not po_number:
            st.error("PO번호를 입력해주세요.", icon=":material/error:")
        elif po_exists(po_number):
            st.error(f"{po_number}는 이미 있는 PO번호예요.", icon=":material/error:")
        else:
            create_purchase_order(
                {"po_no": po_number, "po_date": po_date, "due_date": due_date, "currency": currency,
                 "supplier": vendor["name"], "supply_type": vendor["vendor_type"], "destination": destination,
                 "buyer": my_name, "note": note.strip()},
                cart,
            )
            st.session_state["proc_cart"] = []
            st.session_state["proc_last_po"] = po_number
            st.rerun()


# ---------------------------------------------------------------- 관리자
def _render_admin():
    with st.expander("구매 E2E 엑셀 다시 올리기 (관리자 전용)", icon=":material/upload_file:"):
        st.caption(
            "구매팀 E2E 통합이력 엑셀(발주관리 시트)을 올리면 바뀐 줄만 갱신하고 새 줄을 추가해요. "
            "앱에만 있는 줄은 지우지 않고, 앱에서 입고를 등록한 줄은 엑셀의 입고값으로 덮어쓰지 않아요."
        )
        up = st.file_uploader("E2E 엑셀", type=["xlsx"], key="e2e_upload")
        if up is not None:
            try:
                preview = import_e2e(up.getvalue(), dry_run=True)
            except ValueError as e:
                st.error(str(e), icon=":material/error:")
                return
            st.write(
                f"엑셀 {preview['rows']:,}줄 → 새 줄 **{preview['new_lines']:,}** · 바뀐 줄 **{preview['updated_lines']:,}** · "
                f"그대로 {preview['unchanged_lines']:,} · 입고 기록 변경 {preview['receipt_changes']:,}줄"
            )
            if preview["kept_app_receipts"]:
                st.caption(f"앱에서 입고를 등록해서 엑셀 입고값을 건너뛰는 줄: {preview['kept_app_receipts']:,}")
            if preview["changed_fields"]:
                st.caption("바뀐 항목: " + ", ".join(f"{k} {v}" for k, v in preview["changed_fields"].most_common()))
            if preview["renumbered"]:
                st.caption("관리번호가 없거나 중복이라 새 번호를 붙이는 줄:")
                st.dataframe(pd.DataFrame(preview["renumbered"]), hide_index=True)
            if st.button("반영", type="primary", key="e2e_apply", icon=":material/check:"):
                r = import_e2e(up.getvalue())
                load_purchase_lines.clear()
                list_vendors.clear()
                st.success(f"반영했어요. 새 줄 {r['new_lines']:,} · 바뀐 줄 {r['updated_lines']:,}", icon=":material/check_circle:")

    with st.expander("발주서 양식 관리 (관리자 전용)", icon=":material/description:"):
        tpl_info = load_latest_template()
        if tpl_info:
            uploaded = tpl_info[1].astimezone(KST).strftime("%Y-%m-%d %H:%M")
            st.caption(f"현재 양식: **{tpl_info[0]}** · 등록 {uploaded} · 부품 카탈로그 {len(load_catalog()):,}개")
        else:
            st.caption("아직 등록된 양식이 없어요. 회사 발주서 엑셀(PO 시트 + 부품 정보 RAWDATA 시트)을 올려주세요.")
        st.caption("양식에 거래처 단가가 들어 있어서 GitHub가 아닌 앱 DB에만 보관해요.")
        tpl_up = st.file_uploader("발주서 양식 엑셀", type=["xlsx"], key="po_template_upload")
        if tpl_up is not None and st.button("양식 등록", key="po_template_save_btn", icon=":material/upload:"):
            try:
                n = save_po_template(tpl_up.getvalue(), tpl_up.name, st.session_state.get("transfer_my_name") or "관리자")
            except ValueError as e:
                st.error(str(e), icon=":material/error:")
            else:
                st.success(f"양식을 등록했어요. 부품 카탈로그 {n:,}개를 불러왔어요.", icon=":material/check_circle:")


def render(ctx):
    today = datetime.now(KST).date()
    st.caption(
        "회사 바깥 거래처에서 사오는 구매(내자/외자)를 PO 라인 단위로 관리해요 — 구매요청 → PO → 입고 → 대금. "
        "구매팀 E2E 엑셀 이력과 앱에서 만든 PO가 같이 보여요."
    )
    lines = load_purchase_lines()
    view = st.segmented_control("보기", VIEWS, default=VIEWS[0], key="proc_view", label_visibility="collapsed") or VIEWS[0]
    if lines.empty and view != "신규 PO":
        st.info("아직 구매 데이터가 없어요.", icon=":material/info:")
    elif view == "미입고·요약":
        _render_open(lines, today)
    elif view == "PO 목록":
        _render_po_list(lines, today)
    elif view == "품목별 이력":
        _render_item_history(lines)
    elif view == "공급사별":
        _render_suppliers(lines)
    else:
        _render_new_po(today)

    if ctx.auth["role"] == "admin":
        st.divider()
        _render_admin()
