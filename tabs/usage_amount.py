"""월별 사용 금액 탭."""

import io
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from categories import list_categories, list_category_items
from formatting import (
    ACCENT,
    ACCENT_2,
    CHART_GRID,
    CHART_MUTED,
    fmt_won,
    monthly_mean_excluding_current,
    render_trend_chart,
    weeknum,
)
from usage_data import list_usage_camps, load_usage_period_item

ALL = "전체"
SORT_TOTAL = "합계 많은 순"
SORT_RISING = "최근 증가 순"
CHART_MAX = 6
CHART_COLORS = [ACCENT, ACCENT_2, "#4FC3A1", "#E36D8C", "#B48CF2", "#E8D45B"]


def _render_item_chart(rows, pivot, periods, labels, value_label):
    """고른 부품들의 기간별 사용량을 큰 선 그래프로 (하나면 영역 그래프, 여럿이면 색으로 구분)."""
    long = pd.DataFrame([
        {"부품": f"{r['품명']} ({code})", "기간": labels[p], value_label: float(pivot.at[code, p])}
        for code, r in rows.iterrows()
        for p in periods
    ])
    if len(rows) == 1:
        st.markdown(f"**{long['부품'].iloc[0]}** · 합계 {rows['합계'].iloc[0]:,.0f}")
        st.altair_chart(render_trend_chart(long, "기간", value_label, height=360), width="stretch")
        return
    axis = dict(labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False)
    chart = (
        alt.Chart(long)
        .mark_line(interpolate="monotone", strokeWidth=2.5, point=alt.OverlayMarkDef(size=40))
        .encode(
            x=alt.X("기간:O", sort=None, title=None, axis=alt.Axis(grid=False, **axis)),
            y=alt.Y(f"{value_label}:Q", title=None, axis=alt.Axis(gridColor=CHART_GRID, tickCount=5, **axis)),
            color=alt.Color(
                "부품:N", sort=list(long["부품"].unique()),
                scale=alt.Scale(range=CHART_COLORS),
                legend=alt.Legend(orient="bottom", columns=2, title=None, labelColor=CHART_MUTED, labelLimit=320),
            ),
            tooltip=["부품:N", "기간:O", alt.Tooltip(f"{value_label}:Q", format=",.0f")],
        )
        .properties(height=380)
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )
    st.altair_chart(chart, width="stretch")


def render_usage_matrix(sku_names):
    """(관리자) 부품 전체를 월별/주별로 펼친 사용량 표 + 엑셀 다운로드."""
    c1, c2, c3 = st.columns(3)
    unit_label = c1.segmented_control("단위", ["월별", "주별"], default="월별", key="um_unit") or "월별"
    value_label = c2.segmented_control("값", ["수량", "금액"], default="수량", key="um_value") or "수량"
    unit = "month" if unit_label == "월별" else "week"
    if unit == "month":
        span = c3.selectbox("기간", [3, 6, 12, ALL], index=1, key="um_span_m",
                            format_func=lambda v: v if v == ALL else f"최근 {v}개월")
    else:
        span = c3.selectbox("기간", [8, 12, 26, 52, ALL], index=1, key="um_span_w",
                            format_func=lambda v: v if v == ALL else f"최근 {v}주")

    f1, f2, f3, f4 = st.columns([1, 1, 1.4, 1])
    camp = f1.selectbox("캠프", [ALL] + list_usage_camps(), key="um_camp")
    category = f2.selectbox("카테고리", [ALL] + list_categories(), key="um_category")
    query = f3.text_input("부품코드·품명 검색", key="um_query").strip().lower()
    sort = f4.selectbox("정렬", [SORT_TOTAL, SORT_RISING], key="um_sort")

    df = load_usage_period_item(unit, None if camp == ALL else camp)
    if df.empty:
        st.info("이 조건에 해당하는 사용량 데이터가 없어요.")
        return
    value_col = "qty" if value_label == "수량" else "amt"
    periods = sorted(df["period"].unique())
    if span != ALL:
        periods = periods[-span:]
    df = df[df["period"].isin(periods)]

    pivot = df.pivot_table(index="item_code", columns="period", values=value_col, aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(columns=periods, fill_value=0)
    names = df.groupby("item_code")["item_name"].max()
    table = pd.DataFrame({
        "부품코드": pivot.index,
        "품명": [sku_names.get(c) or names.get(c) or "-" for c in pivot.index],
    }, index=pivot.index)

    if category != ALL:
        table = table[table["부품코드"].isin(set(list_category_items(category)))]
    if query:
        table = table[
            table["부품코드"].str.lower().str.contains(query, regex=False)
            | table["품명"].str.lower().str.contains(query, regex=False)
        ]
    pivot = pivot.loc[table.index]

    # 아직 끝나지 않은 이번 달/이번 주는 열 이름에 표시하고, 증가 계산에서는 뺀다
    today = date.today()
    current = today.strftime("%Y-%m") if unit == "month" else f"{today.year}-{weeknum(today):02d}주"
    closed = [p for p in periods if p != current]
    table["합계"] = pivot.sum(axis=1)
    if len(closed) >= 2:
        table["최근 증감"] = pivot[closed[-1]] - pivot[closed[:-1]].mean(axis=1)
    else:
        table["최근 증감"] = 0.0
    table["추세"] = pivot.values.tolist()
    labels = {p: f"{p} (진행중)" if p == current else p for p in periods}
    table = table.join(pivot.rename(columns=labels))
    table = table.sort_values("최근 증감" if sort == SORT_RISING else "합계", ascending=False)

    st.caption(
        f"부품 {len(table):,}개 · {periods[0]} ~ {periods[-1]} · {camp if camp != ALL else '전체 캠프 합산'}"
        + (f" · 카테고리 {category}" if category != ALL else "")
        + ". '최근 증감'은 마지막으로 끝난 기간이 그 앞 기간들의 평균보다 얼마나 많은지예요."
    )
    if value_label == "수량":
        num = st.column_config.NumberColumn(format="%,.0f")
    else:
        num = st.column_config.NumberColumn(format="₩%,.0f")
    column_config = {c: num for c in ["합계", "최근 증감", *labels.values()]}
    column_config["추세"] = st.column_config.LineChartColumn("추세", width="small")
    chart_box = st.container()
    event = st.dataframe(
        table, hide_index=True, column_config=column_config, height=560,
        # 조건이 바뀌면 줄 번호가 달라지므로 선택도 새로 시작한다
        on_select="rerun", selection_mode="multi-row",
        key=f"um_table_{unit}_{value_label}_{span}_{camp}_{category}_{query}_{sort}",
    )

    # 표에서 고른 줄, 아니면 검색 결과가 몇 개 안 될 때 그 부품들을 크게 그린다
    picked = [table.index[i] for i in event.selection.rows if i < len(table)]
    if not picked and query and len(table) <= CHART_MAX:
        picked = list(table.index)
    with chart_box:
        if picked:
            _render_item_chart(table.loc[picked[:CHART_MAX]], pivot, periods, labels, value_label)
            if len(picked) > CHART_MAX:
                st.caption(f"그래프는 앞의 {CHART_MAX}개만 그렸어요.")
        else:
            st.caption(f":material/touch_app: 표에서 줄을 누르면(여러 개 가능) 그 부품의 그래프가 여기 크게 나와요. "
                       f"검색 결과가 {CHART_MAX}개 이하면 자동으로 그려요.")

    out = io.BytesIO()
    table.drop(columns=["추세"]).to_excel(out, index=False, sheet_name=f"부품별 {unit_label} {value_label}")
    st.download_button(
        "엑셀 다운로드",
        out.getvalue(),
        file_name=f"부품별_{unit_label}_{value_label}_{periods[0]}_{periods[-1]}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:",
        key="um_download",
    )


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

    sub_labels = [":material/location_on: 캠프별 사용 금액", ":material/settings: 부품별 사용 금액"]
    is_admin = ctx.auth["role"] == "admin"
    if is_admin:
        sub_labels.append(":material/table_chart: 부품별 사용량 표")
    sub_tabs = st.tabs(sub_labels)
    sub_camp, sub_sku = sub_tabs[0], sub_tabs[1]
    if is_admin:
        with sub_tabs[2]:
            render_usage_matrix(usage.get("skuNames") or {})

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
