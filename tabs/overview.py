"""개요 탭."""

import streamlit as st

from formatting import fmt_int, fmt_won


def render(ctx):
    data = ctx.data
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("총 품목 수", f"{len(data['items']):,}종", border=True)
    k2.metric("총 재고 수량", f"{fmt_int(ctx.grand_qty)}개", border=True)
    k3.metric("총 재고 금액", fmt_won(ctx.grand_amt), border=True)
    k4.metric(
        "운영 캠프 수",
        f"{len(data['campsOrder'])}곳",
        delta=(f"재고 0인 캠프 {ctx.zero_camps}곳" if ctx.zero_camps else None),
        delta_color="inverse",
        border=True,
    )

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("RS팀별 재고 금액")
        st.bar_chart(ctx.team_df.set_index("팀")["재고 금액"])
    with c2:
        st.subheader("재고 금액 상위 캠프 TOP 8")
        top8 = ctx.camp_df.sort_values("재고 금액", ascending=False).head(8)
        st.bar_chart(top8.set_index("캠프")["재고 금액"])
