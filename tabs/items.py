"""품목 검색 탭."""

import pandas as pd
import streamlit as st

from formatting import fmt_int, fmt_won, render_trend_chart
from usage_calc import get_usage_for_code


def render(ctx):
    data = ctx.data
    usage = ctx.usage
    search = st.text_input("부품명 또는 부품 번호로 검색", "")
    items = data["items"]
    if search.strip():
        q = search.strip().lower()
        items = [it for it in items if q in it["n"].lower() or q in str(it["c"]).lower()]
    st.caption(f"{len(items):,}개 품목 중 최대 50개 표시")
    monthly_item_camp_qty = (usage.get("monthlyItemCampQty") if usage else None) or {}
    for it in items[:50]:
        camp_usage = get_usage_for_code(usage, it["c"])
        with st.expander(f"{it['n']}  ·  {it['c']}  ·  총 {fmt_int(it['q'])}개  ·  {fmt_won(it['a'])}"):
            if not it["x"]:
                st.caption("보유 캠프 없음")
            else:
                rows = []
                for camp, (q, a) in sorted(it["x"].items(), key=lambda kv: -kv[1][0]):
                    avg = camp_usage.get(camp, {}).get("avg") if camp_usage else None
                    rows.append({"캠프": camp, "재고 수량": q, "주 평균 사용량": avg if avg is not None else "-"})
                st.dataframe(pd.DataFrame(rows), hide_index=True)

            if not camp_usage:
                st.caption("이 품목의 사용량 데이터가 없어요.")
            else:
                st.markdown("**사용량 추이**")
                gran_col, scope_col = st.columns(2)
                granularity = gran_col.radio(
                    "기간 단위", ["주별", "월별"], horizontal=True, key=f"usage_gran_{it['c']}"
                )
                usage_camps = sorted(camp_usage.keys())
                scope = scope_col.selectbox(
                    "범위", ["지바이크 전체"] + usage_camps, key=f"usage_scope_{it['c']}"
                )

                if granularity == "주별":
                    if scope == "지바이크 전체":
                        weekly_totals = {}
                        for camp, u in camp_usage.items():
                            for y, w, qv in u["w"]:
                                weekly_totals[(y, w)] = weekly_totals.get((y, w), 0) + qv
                        series = sorted(weekly_totals.items())
                    else:
                        u = camp_usage.get(scope)
                        series = [((y, w), qv) for y, w, qv in u["w"]] if u else []
                    trend_df = pd.DataFrame(
                        {"주차": [f"{y}-{w}" for (y, w), _ in series], "사용량": [v for _, v in series]}
                    )
                    x_col = "주차"
                else:
                    item_monthly = monthly_item_camp_qty.get(it["c"], {})
                    months = sorted(item_monthly.keys())
                    if scope == "지바이크 전체":
                        values = [sum(item_monthly[m].values()) for m in months]
                    else:
                        values = [item_monthly[m].get(scope, 0) for m in months]
                    trend_df = pd.DataFrame({"월": months, "사용량": values})
                    x_col = "월"

                if trend_df.empty:
                    st.caption("표시할 데이터가 없어요.")
                else:
                    st.altair_chart(render_trend_chart(trend_df, x_col, "사용량"), width="stretch")
                    wide_df = trend_df.set_index(x_col).T
                    st.dataframe(wide_df, hide_index=False)
