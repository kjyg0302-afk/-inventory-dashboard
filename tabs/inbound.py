"""미입고 현황 탭 — 물류팀용 조회 전용. 구매 단가·금액은 보여주지 않는다 (구매·PO 관리 탭과 권한을 나눈 이유)."""

import streamlit as st

from boxhero import get_boxhero_token, fetch_boxhero_items
from procurement import load_purchase_lines

QTY_FORMAT = st.column_config.NumberColumn(format="%,d개")


def render_domestic_stock(open_lines, expanded=False):
    """국내 입고분의 품목별 박스히어로 현재고 + 입고예정. 구매·PO 관리 탭에서도 같이 쓴다."""
    with st.expander("국내 재고 요약 (박스히어로 현재고 + 입고예정)", icon=":material/inventory_2:", expanded=expanded):
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
            가장_이른_입고=("expected_on", "min"),
        )
        g["현재고"] = g["key"].str.upper().map(stock)
        g["합계"] = g["현재고"] + g["입고예정"]
        g["가장_이른_입고"] = g["가장_이른_입고"].dt.date
        g = g.rename(columns={"key": "SKU / PN", "가장_이른_입고": "가장 이른 예상 입고일"})
        st.dataframe(g[["SKU / PN", "품명", "현재고", "입고예정", "합계", "가장 이른 예상 입고일", "PO"]],
                     hide_index=True, width="stretch")
        st.caption("입고처가 대구·한국인 미입고만 더했어요. SKU가 없는 품목은 현재고를 알 수 없어 비어 있어요.")


def render(ctx):
    lines = load_purchase_lines()
    open_lines = lines[lines["open_qty"] > 0]
    if open_lines.empty:
        st.caption("미입고 줄이 없어요.")
        return

    k1, k2, k3 = st.columns(3)
    k1.metric("미입고", f"{len(open_lines):,}줄 · {open_lines['open_qty'].sum():,.0f}개", border=True)
    k2.metric("진행 중 PO", f"{open_lines['po_no'].nunique():,}건", border=True)
    k3.metric("예상 입고일 지난 미입고", f"{int(open_lines['overdue'].sum()):,}줄", border=True)

    f1, f2, f3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    q = f1.text_input("검색 (SKU · 품명 · 제조사 PN · PO번호)", key="inbound_q").strip().lower()
    dest = f2.selectbox("입고처", ["전체"] + sorted(open_lines["destination"].dropna().unique()), key="inbound_dest")
    only_late = f3.checkbox("입고 지연만", key="inbound_late")

    sel = open_lines
    if q:
        hay = (sel["sku"].fillna("") + " " + sel["item_name"].fillna("") + " " + sel["mfr_pn"].fillna("") + " "
               + sel["po_no"].fillna("")).str.lower()
        sel = sel[hay.str.contains(q, regex=False)]
    if dest != "전체":
        sel = sel[sel["destination"] == dest]
    if only_late:
        sel = sel[sel["overdue"]]

    st.markdown("**미입고 목록**")
    if sel.empty:
        st.caption("조건에 맞는 미입고가 없어요.")
    else:
        sel = sel.sort_values(["overdue", "expected_on"], ascending=[False, True])
        cols = {
            "po_no": "PO", "sku": "SKU", "item_name": "품명", "supplier": "공급사", "qty": "발주", "received_qty": "입고",
            "open_qty": "미입고", "po_approved_on": "PO 승인일", "expected_on": "예상 입고일", "destination": "입고처",
            "status": "상태", "overdue": "입고 지연",
        }
        table = sel[list(cols)].rename(columns=cols)
        for c in ["PO 승인일", "예상 입고일"]:
            table[c] = table[c].dt.date
        st.dataframe(
            table, hide_index=True, width="stretch",
            column_config={c: QTY_FORMAT for c in ["발주", "입고", "미입고"]},
        )
        st.caption(
            f"{len(sel):,}줄 · {sel['open_qty'].sum():,.0f}개. "
            "예상 입고일은 계약 납기가 있으면 그 날짜, 없으면 PO 승인일 + 3.5개월이에요. "
            "둘 다 없으면 비어 있어요. 입고 등록은 구매·PO 관리 탭에서 해요."
        )

    render_domestic_stock(sel, expanded=True)
