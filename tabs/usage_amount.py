"""월별 사용 금액 탭."""

import altair as alt
import pandas as pd
import streamlit as st

from formatting import ACCENT, CHART_GRID, CHART_MUTED, fmt_won, monthly_mean_excluding_current, render_trend_chart


def render(ctx):
    usage = ctx.usage
    monthly_camp_amount = usage.get("monthlyCampAmount") if usage else None
    if not monthly_camp_amount:
        st.info("사용량 엑셀을 업로드하면 캠프별·월별 사용 금액을 확인할 수 있어요. (JSON 업로드에는 이 데이터가 없어요)", icon=":material/payments:")
        return

    months = sorted(monthly_camp_amount.keys())
    amt_rows = [
        {"월": m, "캠프": camp, "금액": amt}
        for m in months
        for camp, amt in monthly_camp_amount[m].items()
    ]
    amt_df = pd.DataFrame(amt_rows)

    monthly_total = amt_df.groupby("월")["금액"].sum().reindex(months, fill_value=0)
    grand_total = monthly_total.sum()
    monthly_avg = monthly_mean_excluding_current(monthly_total)

    k1, k2, k3 = st.columns(3)
    k1.metric(
        "총 사용 금액",
        fmt_won(grand_total),
        border=True,
        chart_data=monthly_total,
        chart_type="area",
    )
    k2.metric("월평균 사용 금액", fmt_won(monthly_avg), border=True)
    k3.metric("데이터 기간", f"{months[0]} ~ {months[-1]} ({len(months)}개월)", border=True)
    st.caption("이번 달은 아직 마감 전이라 월평균 계산에서는 제외했어요 (그래프·합계에는 포함돼요).")

    st.subheader("전체 캠프 합산 · 월별 사용 금액 추이")
    overall_trend_df = monthly_total.reset_index()
    overall_trend_df.columns = ["월", "금액"]
    st.altair_chart(render_trend_chart(overall_trend_df, "월", "금액"), width="stretch")
    st.dataframe(
        overall_trend_df.sort_values("월", ascending=False),
        hide_index=True,
        column_config={"금액": st.column_config.NumberColumn(format="₩%,d")},
    )

    sub_camp, sub_sku = st.tabs([":material/location_on: 캠프별 사용 금액", ":material/settings: 부품별 사용 금액"])

    with sub_camp:
        camp_totals = (
            amt_df.groupby("캠프")["금액"].sum().sort_values(ascending=False).reset_index()
        )
        camp_totals.columns = ["캠프", "총 사용 금액"]

        picked_camp = st.selectbox("캠프 선택", ["전체 캠프 합산"] + camp_totals["캠프"].tolist())
        if picked_camp != "전체 캠프 합산":
            camp_series = (
                amt_df[amt_df["캠프"] == picked_camp].set_index("월")["금액"].reindex(months, fill_value=0)
            )
            cc1, cc2 = st.columns(2)
            cc1.metric(f"{picked_camp} 총 사용 금액", fmt_won(camp_series.sum()), border=True)
            cc2.metric(
                f"{picked_camp} 월평균 사용 금액",
                fmt_won(monthly_mean_excluding_current(camp_series)),
                border=True,
            )
            camp_trend_df = camp_series.reset_index()
            camp_trend_df.columns = ["월", "금액"]
            st.altair_chart(render_trend_chart(camp_trend_df, "월", "금액"), width="stretch")
            st.dataframe(
                camp_trend_df.sort_values("월", ascending=False),
                hide_index=True,
                column_config={"금액": st.column_config.NumberColumn(format="₩%,d")},
            )

        st.subheader("캠프별 총 사용 금액 순위 (전체 기간 합계)")
        camp_bar = (
            alt.Chart(camp_totals)
            .mark_bar(color=ACCENT, cornerRadiusTopRight=3, cornerRadiusBottomRight=3, height=14)
            .encode(
                y=alt.Y(
                    "캠프:N",
                    sort="-x",
                    title=None,
                    axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False),
                ),
                x=alt.X(
                    "총 사용 금액:Q",
                    title=None,
                    axis=alt.Axis(
                        labelColor=CHART_MUTED, labelFontSize=11, gridColor=CHART_GRID, domain=False, ticks=False
                    ),
                ),
                tooltip=[alt.Tooltip("캠프:N"), alt.Tooltip("총 사용 금액:Q", format=",.0f")],
            )
            .properties(height=max(220, len(camp_totals) * 20))
            .configure_view(strokeWidth=0)
            .configure(background="transparent")
        )
        st.altair_chart(camp_bar, width="stretch")
        st.dataframe(
            camp_totals,
            hide_index=True,
            column_config={"총 사용 금액": st.column_config.NumberColumn(format="₩%,d")},
        )

    with sub_sku:
        monthly_sku_amount = usage.get("monthlySkuAmount")
        sku_names = usage.get("skuNames") or {}
        if not monthly_sku_amount:
            st.caption("이 사용량 데이터에는 부품별 금액 정보가 없어요. 사용량 엑셀을 다시 업로드하면 채워집니다.")
        else:
            sku_amt_rows = [
                {"SKU": sku, "월": m, "금액": amt}
                for m in months
                for sku, amt in monthly_sku_amount.get(m, {}).items()
            ]
            sku_amt_df = pd.DataFrame(sku_amt_rows)

            sku_totals = sku_amt_df.groupby("SKU")["금액"].sum().sort_values(ascending=False).reset_index()
            sku_totals.columns = ["SKU", "총 사용 금액"]
            sku_totals["품명"] = sku_totals["SKU"].map(sku_names).fillna("-")

            sku_weekly_amount = usage.get("skuWeeklyAmount") or {}
            sku_totals["주평균 사용금액"] = sku_totals["SKU"].map(sku_weekly_amount)

            sku_monthly_pivot = (
                sku_amt_df.pivot_table(index="SKU", columns="월", values="금액", fill_value=0)
                .reindex(columns=months, fill_value=0)
            )
            sku_monthly_avg = sku_monthly_pivot.apply(monthly_mean_excluding_current, axis=1)
            sku_totals["월평균 사용금액"] = sku_totals["SKU"].map(sku_monthly_avg)

            # 품명이 같은 부품이 섞여 있을 수 있어, 그래프/표 표시용으로는 SKU를 덧붙여 구분한다.
            sku_totals["표시명"] = sku_totals["품명"] + " (" + sku_totals["SKU"] + ")"
            sku_totals = sku_totals[
                ["표시명", "품명", "SKU", "총 사용 금액", "주평균 사용금액", "월평균 사용금액"]
            ]

            sku_query = st.text_input("SKU 또는 품명으로 검색해서 월별 추이 보기", "", key="usage_sku_query")
            if sku_query.strip():
                q = sku_query.strip().lower()
                all_candidates = [
                    s for s in sku_totals["SKU"] if q in s.lower() or q in str(sku_names.get(s, "")).lower()
                ]
                candidates = all_candidates[:15]
                if candidates:
                    if len(all_candidates) > len(candidates):
                        st.caption(
                            f"검색 결과 {len(all_candidates)}건 중 상위 {len(candidates)}개만 표시했어요. "
                            "검색어를 더 구체적으로 입력하면 찾는 품목이 더 잘 보여요."
                        )
                    labels = {f"{s} · {sku_names.get(s, '-')}": s for s in candidates}
                    picked_label = st.radio("검색 결과", list(labels.keys()), key="usage_sku_radio")
                    picked_sku = labels[picked_label]
                    sku_series = (
                        sku_amt_df[sku_amt_df["SKU"] == picked_sku]
                        .set_index("월")["금액"]
                        .reindex(months, fill_value=0)
                    )
                    sc1, sc2 = st.columns(2)
                    sc1.metric(f"{picked_label} 총 사용 금액", fmt_won(sku_series.sum()), border=True)
                    sc2.metric(
                        f"{picked_label} 월평균 사용 금액",
                        fmt_won(monthly_mean_excluding_current(sku_series)),
                        border=True,
                    )
                    sku_trend_df = sku_series.reset_index()
                    sku_trend_df.columns = ["월", "금액"]
                    st.altair_chart(render_trend_chart(sku_trend_df, "월", "금액"), width="stretch")
                    st.dataframe(
                        sku_trend_df.sort_values("월", ascending=False),
                        hide_index=True,
                        column_config={"금액": st.column_config.NumberColumn(format="₩%,d")},
                    )
                else:
                    st.caption("일치하는 부품이 없습니다.")

            st.subheader("부품별 총 사용 금액 순위 (전체 기간 합계)")
            top_n = 25
            sku_bar_df = sku_totals.head(top_n)
            sku_bar = (
                alt.Chart(sku_bar_df)
                .mark_bar(color=ACCENT, cornerRadiusTopRight=3, cornerRadiusBottomRight=3, height=14)
                .encode(
                    y=alt.Y(
                        "표시명:N",
                        sort="-x",
                        title=None,
                        axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False),
                    ),
                    x=alt.X(
                        "총 사용 금액:Q",
                        title=None,
                        axis=alt.Axis(
                            labelColor=CHART_MUTED,
                            labelFontSize=11,
                            gridColor=CHART_GRID,
                            domain=False,
                            ticks=False,
                        ),
                    ),
                    tooltip=[
                        alt.Tooltip("품명:N"),
                        alt.Tooltip("SKU:N"),
                        alt.Tooltip("총 사용 금액:Q", format=",.0f"),
                    ],
                )
                .properties(height=max(220, len(sku_bar_df) * 20))
                .configure_view(strokeWidth=0)
                .configure(background="transparent")
            )
            st.altair_chart(sku_bar, width="stretch")
            st.caption(f"상위 {top_n}개만 그래프로 표시했어요 (전체 {len(sku_totals):,}개 부품은 아래 표에서 검색 가능).")

            sku_table_search = st.text_input("SKU 또는 품명으로 검색 (표)", "", key="usage_sku_table_search")
            sku_table_df = sku_totals
            if sku_table_search.strip():
                q = sku_table_search.strip().lower()
                sku_table_df = sku_table_df[
                    sku_table_df["SKU"].str.lower().str.contains(q, regex=False)
                    | sku_table_df["품명"].str.lower().str.contains(q, regex=False)
                ]
            st.dataframe(
                sku_table_df[["품명", "SKU", "총 사용 금액", "주평균 사용금액", "월평균 사용금액"]],
                hide_index=True,
                column_config={
                    "총 사용 금액": st.column_config.NumberColumn(format="₩%,d"),
                    "주평균 사용금액": st.column_config.NumberColumn(format="₩%,d"),
                    "월평균 사용금액": st.column_config.NumberColumn(format="₩%,d"),
                },
            )
