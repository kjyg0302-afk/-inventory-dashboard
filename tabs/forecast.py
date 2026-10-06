"""FORECAST(수요 예측) 탭."""

from datetime import datetime

import pandas as pd
import streamlit as st

from usage_data import top_camp_items_recent
from forecast import save_demand_forecasts, load_demand_forecasts, list_demand_forecasts


def render(ctx):
    data = ctx.data
    st.caption(
        "캠프별로 품목별 다음 몇 달치 예상 사용량을 직접 입력해요. 최근 3개월(이번 달 제외) 평균이 "
        "참고용으로 먼저 채워지고, 필요하면 조정해서 저장하면 됩니다. "
        "(추후 회귀분석 기반 자동 예측으로 보완/대체할 예정)"
    )

    fc_my_name = st.text_input(
        "내 이름", value=st.session_state.get("transfer_my_name", ""), key="fc_my_name_input"
    )
    st.session_state["transfer_my_name"] = fc_my_name

    fc_camp = st.selectbox("캠프 선택", data["campsOrder"], key="fc_camp_select")

    fc_now = datetime.now()
    fc_month_options = []
    fy, fm = fc_now.year, fc_now.month
    for _ in range(3):
        fm += 1
        if fm > 12:
            fm = 1
            fy += 1
        fc_month_options.append((fy, fm))
    fc_month_labels = [f"{y}-{m:02d}" for y, m in fc_month_options]
    fc_month_label = st.selectbox("예측 대상 월", fc_month_labels, key="fc_month_select")
    fc_year, fc_month = fc_month_options[fc_month_labels.index(fc_month_label)]

    try:
        with st.spinner("최근 사용 품목을 불러오는 중이에요..."):
            top_items_df = top_camp_items_recent(fc_camp, limit=30)
            existing_df = load_demand_forecasts(fc_camp, fc_year, fc_month)
    except Exception as e:
        st.error(f"데이터를 불러오는 중 문제가 발생했습니다: {e}", icon=":material/error:")
    else:
        if top_items_df.empty:
            st.caption(f"{fc_camp}의 최근 3개월 사용량 데이터가 없어요.")
        else:
            existing_by_code = (
                {row["item_code"]: row for _, row in existing_df.iterrows()} if not existing_df.empty else {}
            )

            fc_rows = []
            for _, r in top_items_df.iterrows():
                code = r["item_code"]
                existing = existing_by_code.get(code)
                default_qty = (
                    float(existing["predicted_qty"]) if existing is not None else round(r["avg_qty"] or 0, 1)
                )
                fc_rows.append(
                    {
                        "SKU": code,
                        "품명": r["item_name"] or "-",
                        "최근 3개월 평균": r["avg_qty"],
                        "예측 수량": default_qty,
                    }
                )
            fc_table_df = pd.DataFrame(fc_rows)

            st.caption(f"{fc_camp} · {fc_month_label} 예측 (최근 사용량 상위 {len(fc_table_df)}개 품목)")
            with st.form(f"fc_form_{fc_camp}_{fc_year}_{fc_month}"):
                fc_edited_df = st.data_editor(
                    fc_table_df,
                    hide_index=True,
                    disabled=["SKU", "품명", "최근 3개월 평균"],
                    column_config={
                        "최근 3개월 평균": st.column_config.NumberColumn(format="%.1f개"),
                        "예측 수량": st.column_config.NumberColumn(format="%d개", min_value=0, step=1),
                    },
                    key=f"fc_editor_{fc_camp}_{fc_year}_{fc_month}",
                )
                fc_submitted = st.form_submit_button("예측 저장", type="primary", icon=":material/save:")

            if fc_submitted:
                if not fc_my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                else:
                    save_rows = [
                        {
                            "camp": fc_camp,
                            "item_code": r["SKU"],
                            "item_name": r["품명"],
                            "forecast_year": fc_year,
                            "forecast_month": fc_month,
                            "predicted_qty": float(r["예측 수량"]),
                            "historical_avg_qty": (
                                float(r["최근 3개월 평균"]) if pd.notna(r["최근 3개월 평균"]) else None
                            ),
                            "entered_by": fc_my_name.strip(),
                        }
                        for _, r in fc_edited_df.iterrows()
                    ]
                    save_demand_forecasts(save_rows)
                    st.success(
                        f"{fc_camp} {fc_month_label} 예측 {len(save_rows)}건을 저장했습니다.",
                        icon=":material/check_circle:",
                    )
                    load_demand_forecasts.clear()
                    list_demand_forecasts.clear()
                    st.rerun()

    st.subheader("저장된 예측 이력")
    fc_hist_df = list_demand_forecasts(camp=fc_camp)
    if fc_hist_df.empty:
        st.caption("아직 저장된 예측이 없어요.")
    else:
        fc_hist_display = fc_hist_df.copy()
        fc_hist_display["예측 대상월"] = (
            fc_hist_display["forecast_year"].astype(str) + "-"
            + fc_hist_display["forecast_month"].astype(str).str.zfill(2)
        )
        st.dataframe(
            fc_hist_display[
                ["예측 대상월", "item_name", "item_code", "predicted_qty", "historical_avg_qty", "entered_by"]
            ].rename(columns={"item_name": "품명", "item_code": "SKU"}),
            hide_index=True,
            column_config={
                "predicted_qty": st.column_config.NumberColumn("예측 수량", format="%.1f개"),
                "historical_avg_qty": st.column_config.NumberColumn("최근 3개월 평균", format="%.1f개"),
            },
        )
