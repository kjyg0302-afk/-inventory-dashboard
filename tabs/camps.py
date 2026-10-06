"""캠프별 현황 탭."""

import altair as alt
import streamlit as st

from formatting import ACCENT, CHART_GRID, CHART_MUTED


def render(ctx):
    usage = ctx.usage
    camp_weekly_amount = (usage.get("campWeeklyAmount") if usage else None) or {}
    camp_full_df = ctx.camp_df.copy()
    camp_full_df["주 사용 금액"] = camp_full_df["캠프"].map(camp_weekly_amount)
    camp_full_df["재고 보유(주)"] = camp_full_df.apply(
        lambda r: round(r["재고 금액"] / r["주 사용 금액"], 1) if r["주 사용 금액"] else None, axis=1
    )

    sort_col = st.selectbox(
        "정렬 기준", ["재고 금액", "재고 수량", "캠프", "팀", "재고 보유(주)"], index=0
    )
    ascending = st.checkbox("오름차순", value=False)
    camp_full_df = camp_full_df.sort_values(sort_col, ascending=ascending, na_position="last")

    st.caption(
        "재고 보유(주)는 캠프 사용량 엑셀 기준 주 평균 사용 금액 대비, 현재 재고 금액이 "
        "몇 주치인지를 나타내요. 사용량 데이터가 없으면 빈 칸으로 표시돼요."
    )

    money_df = camp_full_df.sort_values("재고 금액", ascending=False)
    camp_order = money_df["캠프"].tolist()
    coverage_df = camp_full_df.dropna(subset=["재고 보유(주)"])
    LINE_COLOR = "#F5A623"

    st.subheader("캠프별 재고 금액 · 재고 지수(보유 주수)")
    st.caption("막대 = 재고 금액(왼쪽 축), 선 = 재고 지수·재고 보유 주수(오른쪽 축, 주황색).")

    bars_chart = (
        alt.Chart(money_df)
        .mark_bar(color=ACCENT, cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X(
                "캠프:N",
                sort=camp_order,
                title=None,
                axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=10, labelAngle=-60, domain=False, ticks=False),
            ),
            y=alt.Y(
                "재고 금액:Q",
                title="재고 금액",
                axis=alt.Axis(
                    titleColor=ACCENT, labelColor=ACCENT, labelFontSize=11, gridColor=CHART_GRID,
                    domain=False, ticks=False,
                ),
            ),
            tooltip=[alt.Tooltip("캠프:N"), alt.Tooltip("재고 금액:Q", format=",.0f")],
        )
    )

    line_chart = (
        alt.Chart(coverage_df)
        .mark_line(
            interpolate="monotone",
            color=LINE_COLOR,
            strokeWidth=2.5,
            point=alt.OverlayMarkDef(filled=True, fill=LINE_COLOR, stroke="#10141B", strokeWidth=1.5, size=45),
        )
        .encode(
            x=alt.X("캠프:N", sort=camp_order, title=None),
            y=alt.Y(
                "재고 보유(주):Q",
                title="재고 보유(주)",
                axis=alt.Axis(
                    titleColor=LINE_COLOR, labelColor=LINE_COLOR, labelFontSize=11, orient="right",
                    domain=False, ticks=False, grid=False,
                ),
            ),
            tooltip=[alt.Tooltip("캠프:N"), alt.Tooltip("재고 보유(주):Q", format=",.1f")],
        )
    )

    combo_chart = (
        alt.layer(bars_chart, line_chart)
        .resolve_scale(y="independent")
        .properties(height=360)
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )
    st.altair_chart(combo_chart, width="stretch")

    st.dataframe(
        camp_full_df,
        hide_index=True,
        column_config={
            "재고 수량": st.column_config.NumberColumn(format="%,d개"),
            "재고 금액": st.column_config.NumberColumn(format="₩%,d"),
            "주 사용 금액": st.column_config.NumberColumn(format="₩%,d"),
            "재고 보유(주)": st.column_config.NumberColumn(format="%.1f주"),
        },
    )
