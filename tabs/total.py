"""지바이크 전체 재고(창고+캠프) 탭."""

import altair as alt
import pandas as pd
import streamlit as st

from formatting import ACCENT, CHART_GRID, CHART_MUTED, fmt_int, fmt_won
from boxhero import get_boxhero_token, fetch_boxhero_items
from snapshots import load_inventory_value_trend
from usage_calc import estimate_depletion, weekly_usage_rate


def render(ctx):
    data = ctx.data
    usage = ctx.usage
    if not get_boxhero_token():
        st.info(
            "박스히어로 연동이 설정되면 창고 + 캠프 전체 재고를 함께 볼 수 있어요.",
            icon=":material/link_off:",
        )
        return

    try:
        with st.spinner("박스히어로에서 창고 데이터를 가져오는 중이에요..."):
            wh_items = fetch_boxhero_items()
    except Exception as e:
        st.error(f"박스히어로 연동 중 문제가 발생했습니다: {e}", icon=":material/error:")
        return

    warehouse_qty = sum(it.get("quantity", 0) for it in wh_items)
    warehouse_amt = sum(it.get("quantity", 0) * float(it.get("price") or 0) for it in wh_items)
    camp_qty = ctx.grand_qty
    camp_amt = ctx.grand_amt
    total_qty = warehouse_qty + camp_qty
    total_amt = warehouse_amt + camp_amt

    st.caption("수량 기준")
    k1, k2, k3 = st.columns(3)
    k1.metric("지바이크 전체 재고 수량", f"{fmt_int(total_qty)}개", border=True)
    k2.metric("캠프 재고 (36곳 합계)", f"{fmt_int(camp_qty)}개", border=True)
    k3.metric("물류창고 재고", f"{fmt_int(warehouse_qty)}개", border=True)

    st.caption("금액 기준")
    m1, m2, m3 = st.columns(3)
    m1.metric("지바이크 전체 재고 금액", fmt_won(total_amt), border=True)
    m2.metric("캠프 재고 금액", fmt_won(camp_amt), border=True)
    m3.metric("물류창고 재고 금액", fmt_won(warehouse_amt), border=True)
    st.caption(
        "창고 재고 금액은 원가(cost) 입력이 대부분 비어있어 판매가(price) 기준으로 계산했어요 "
        "— 캠프 재고 금액과 산정 기준이 달라 완전히 동일한 비교는 아니에요."
    )

    breakdown_df = pd.DataFrame(
        {"구분": ["캠프 (36곳 합계)", "물류창고"], "재고 금액": [camp_amt, warehouse_amt]}
    )
    st.subheader("창고 vs 캠프 재고 금액 비중")
    breakdown_bar = (
        alt.Chart(breakdown_df)
        .mark_bar(color=ACCENT, cornerRadiusTopRight=4, cornerRadiusBottomRight=4, height=32)
        .encode(
            y=alt.Y(
                "구분:N",
                sort="-x",
                title=None,
                axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=12, domain=False, ticks=False),
            ),
            x=alt.X(
                "재고 금액:Q",
                title=None,
                axis=alt.Axis(
                    labelColor=CHART_MUTED, labelFontSize=11, gridColor=CHART_GRID, domain=False, ticks=False
                ),
            ),
            tooltip=[alt.Tooltip("구분:N"), alt.Tooltip("재고 금액:Q", format=",.0f")],
        )
        .properties(height=140)
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )
    st.altair_chart(breakdown_bar, width="stretch")

    st.subheader("주별 재고 금액 추이 (창고 + 캠프)")
    trend_raw = load_inventory_value_trend()
    if trend_raw.empty:
        st.caption("아직 쌓인 현황이 없어요. 이번 주부터 자동으로 쌓여요 (매주 처음 접속할 때 그 주의 최신 값으로 갱신돼요).")
    else:
        trend_wide = trend_raw.pivot_table(
            index="period_label", columns="source", values="amt", aggfunc="sum", fill_value=0
        ).reset_index()
        for col in ("camp", "warehouse"):
            if col not in trend_wide.columns:
                trend_wide[col] = 0
        trend_wide["전체"] = trend_wide["camp"] + trend_wide["warehouse"]
        trend_wide = trend_wide.rename(columns={"camp": "캠프", "warehouse": "물류창고"})
        trend_long = trend_wide.melt(
            id_vars="period_label", value_vars=["전체", "캠프", "물류창고"], var_name="구분", value_name="재고 금액"
        )
        trend_line = (
            alt.Chart(trend_long)
            .mark_line(point=True, strokeWidth=2)
            .encode(
                x=alt.X(
                    "period_label:O",
                    sort=None,
                    title=None,
                    axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False, grid=False),
                ),
                y=alt.Y(
                    "재고 금액:Q",
                    title=None,
                    axis=alt.Axis(
                        labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False,
                        gridColor=CHART_GRID, tickCount=4,
                    ),
                ),
                color=alt.Color("구분:N", scale=alt.Scale(scheme="category10"), legend=alt.Legend(title=None, labelColor=CHART_MUTED)),
                tooltip=[alt.Tooltip("period_label:O", title="주차"), alt.Tooltip("구분:N"), alt.Tooltip("재고 금액:Q", format=",.0f")],
            )
            .properties(height=240)
            .configure_view(strokeWidth=0)
            .configure(background="transparent")
        )
        st.altair_chart(trend_line, width="stretch")
        st.caption(
            "스냅샷 시점 단가 기준 금액이에요. SKU별 수량은 그대로 쌓이고 있어서, 나중에 필요하면 "
            "같은 기준(예: 현재 단가)으로 다시 계산한 추이도 만들 수 있어요."
        )

    st.subheader("SKU별 창고-캠프 재고 비교")
    camp_by_sku = {
        it["c"].strip().upper(): it for it in data["items"] if it.get("c")
    }
    wh_by_sku = {it["sku"].strip().upper(): it for it in wh_items if it.get("sku")}
    all_skus = set(camp_by_sku) | set(wh_by_sku)

    compare_rows = []
    for sku in all_skus:
        camp_it = camp_by_sku.get(sku)
        wh_it = wh_by_sku.get(sku)
        camp_q = camp_it["q"] if camp_it else 0
        camp_a = camp_it["a"] if camp_it else 0
        wh_q = wh_it.get("quantity", 0) if wh_it else 0
        wh_price = float(wh_it.get("price") or 0) if wh_it else 0
        wh_a = wh_q * wh_price
        name = camp_it["n"] if camp_it else wh_it["name"]
        total_q = wh_q + camp_q
        # 사용량 조회는 원래 표기(대소문자)를 써야 정확히 매칭된다 (join용 sku는 대문자로 통일돼있음).
        orig_code = camp_it["c"] if camp_it else wh_it["sku"]
        weeks, depletion_date = estimate_depletion(usage, total_q, orig_code)
        compare_rows.append(
            {
                "SKU": sku,
                "품명": name,
                "물류창고 수량": wh_q,
                "캠프 재고 수량": camp_q,
                "합계 수량": total_q,
                "재고 금액": wh_a + camp_a,
                "주 사용량": weekly_usage_rate(usage, orig_code),
                "소진 예상(주)": weeks,
                "예상 소진일": depletion_date.isoformat() if depletion_date else None,
            }
        )
    compare_df = pd.DataFrame(compare_rows).sort_values("재고 금액", ascending=False)

    compare_search = st.text_input("SKU 또는 품명으로 검색", "", key="total_compare_search")
    if compare_search.strip():
        q = compare_search.strip().lower()
        compare_df = compare_df[
            compare_df["SKU"].str.lower().str.contains(q, regex=False)
            | compare_df["품명"].str.lower().str.contains(q, regex=False)
        ]
    st.caption(
        f"{len(compare_df):,}개 SKU (창고·캠프 어느 한쪽에라도 있는 품목 전체). "
        "주 사용량/예상 소진일은 캠프 사용량 엑셀 기준(전체 캠프 합산)이에요."
    )
    display_compare_df = compare_df.copy()
    display_compare_df["예상 소진일"] = display_compare_df["예상 소진일"].fillna("-")
    st.dataframe(
        display_compare_df,
        hide_index=True,
        column_config={
            "물류창고 수량": st.column_config.NumberColumn(format="%,d개"),
            "캠프 재고 수량": st.column_config.NumberColumn(format="%,d개"),
            "합계 수량": st.column_config.NumberColumn(format="%,d개"),
            "재고 금액": st.column_config.NumberColumn(format="₩%,d"),
            "주 사용량": st.column_config.NumberColumn(format="%.1f개/주"),
            "소진 예상(주)": st.column_config.NumberColumn(format="%.1f주"),
        },
    )
