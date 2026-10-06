"""카테고리별 현황 탭."""

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from formatting import fmt_int, fmt_won, render_trend_chart
from categories import list_categories, list_category_items, save_category_items, delete_category
from usage_data import load_category_monthly_usage, load_recent_monthly_usage_by_sku


def render(ctx):
    data = ctx.data
    auth = ctx.auth
    st.caption(
        "임의로 SKU들을 묶어서 재고 금액과 월별 사용 금액을 따로 확인할 수 있어요 "
        "(예: 단종 예정 부품, 특정 모델 전용 부품 등)."
    )

    if auth["role"] == "admin":
        with st.expander("📁 카테고리 관리 (관리자 전용)"):
            existing_categories = list_categories()
            cat_admin_action = st.radio(
                "작업", ["만들기 / 수정", "삭제"], horizontal=True, key="cat_admin_action"
            )
            if cat_admin_action == "만들기 / 수정":
                cat_pick = st.selectbox(
                    "카테고리 선택 또는 새로 만들기", ["(새 카테고리)"] + existing_categories, key="cat_admin_pick"
                )
                if cat_pick == "(새 카테고리)":
                    cat_name_input = st.text_input("카테고리 이름", key="cat_admin_new_name")
                    default_codes = ""
                else:
                    cat_name_input = cat_pick
                    default_codes = "\n".join(list_category_items(cat_pick))
                codes_text = st.text_area(
                    "SKU 목록 (한 줄에 하나씩 붙여넣으세요)", value=default_codes, height=200, key="cat_admin_codes"
                )
                if st.button("저장", type="primary", icon=":material/save:", key="cat_admin_save_btn"):
                    name = cat_name_input.strip()
                    codes = [c.strip() for c in codes_text.splitlines() if c.strip()]
                    if not name:
                        st.error("카테고리 이름을 입력해주세요.", icon=":material/error:")
                    elif not codes:
                        st.error("SKU를 한 개 이상 입력해주세요.", icon=":material/error:")
                    else:
                        save_category_items(name, codes)
                        st.success(
                            f"'{name}' 카테고리에 SKU {len(set(codes))}개를 저장했습니다.",
                            icon=":material/check_circle:",
                        )
                        list_categories.clear()
                        list_category_items.clear()
                        st.rerun()
            else:
                if existing_categories:
                    del_pick = st.selectbox("삭제할 카테고리", existing_categories, key="cat_admin_del_pick")
                    if st.button("삭제", icon=":material/delete:", key="cat_admin_del_btn"):
                        delete_category(del_pick)
                        st.success(f"'{del_pick}' 카테고리를 삭제했습니다.", icon=":material/check_circle:")
                        list_categories.clear()
                        list_category_items.clear()
                        st.rerun()
                else:
                    st.caption("삭제할 카테고리가 없어요.")

    categories = list_categories()
    if not categories:
        st.info("아직 만들어진 카테고리가 없어요. 관리자가 위 패널에서 만들 수 있어요.", icon=":material/category:")
        return

    view_cat = st.selectbox("카테고리 선택", categories, key="cat_view_pick")
    cat_codes = list_category_items(view_cat)
    cat_code_set = set(cat_codes)
    matched_items = [it for it in data["items"] if it.get("c") in cat_code_set]
    matched_codes = {it["c"] for it in matched_items}
    unmatched_codes = cat_code_set - matched_codes

    total_qty = sum(it["q"] for it in matched_items)
    total_amt = sum(it["a"] for it in matched_items)

    k1, k2, k3 = st.columns(3)
    k1.metric("SKU 수", f"{len(cat_codes)}개", border=True)
    k2.metric("캠프 재고 수량", f"{fmt_int(total_qty)}개", border=True)
    k3.metric("캠프 재고 금액", fmt_won(total_amt), border=True)
    if unmatched_codes:
        st.caption(
            f":material/warning: 현재 재고 데이터에서 찾을 수 없는 SKU {len(unmatched_codes)}개가 있어요 "
            "(품목 코드를 다시 확인해주세요)."
        )

    st.subheader("월별 사용 금액 추이")
    try:
        usage_trend_df = load_category_monthly_usage(cat_codes)
    except Exception as e:
        usage_trend_df = pd.DataFrame()
        st.error(f"사용량 데이터를 불러오는 중 문제가 발생했습니다: {e}", icon=":material/error:")
    if usage_trend_df.empty:
        st.caption("이 카테고리에 대한 사용량 데이터가 없어요.")
    else:
        usage_trend_df = usage_trend_df.copy()
        usage_trend_df["월"] = (
            usage_trend_df["year"].astype(str) + "-" + usage_trend_df["month"].astype(str).str.zfill(2)
        )
        chart_df = usage_trend_df[["월", "amt"]].rename(columns={"amt": "사용 금액"})
        st.altair_chart(render_trend_chart(chart_df, "월", "사용 금액"), width="stretch")
        st.dataframe(
            usage_trend_df[["월", "qty", "amt"]]
            .rename(columns={"qty": "사용 수량", "amt": "사용 금액"})
            .sort_values("월", ascending=False),
            hide_index=True,
            column_config={
                "사용 수량": st.column_config.NumberColumn(format="%,d개"),
                "사용 금액": st.column_config.NumberColumn(format="₩%,d"),
            },
        )

    st.subheader("SKU별 상세")
    try:
        monthly_usage_df = load_recent_monthly_usage_by_sku(cat_codes)
        monthly_usage_map = dict(zip(monthly_usage_df["item_code"], monthly_usage_df["avg_qty"]))
    except Exception:
        monthly_usage_map = {}

    detail_rows = []
    for it in matched_items:
        avg_qty = monthly_usage_map.get(it["c"])
        months_left = (it["q"] / avg_qty) if avg_qty and avg_qty > 0 and it["q"] > 0 else None
        depletion_date = (
            (datetime.now() + timedelta(days=months_left * 30.44)).date() if months_left is not None else None
        )
        detail_rows.append(
            {
                "SKU": it["c"],
                "품명": it["n"],
                "재고 수량": it["q"],
                "재고 금액": it["a"],
                "월 사용수량": avg_qty,
                "소진 예상(개월)": round(months_left, 1) if months_left is not None else None,
                "예상 소진일": depletion_date.isoformat() if depletion_date else None,
            }
        )
    for code in unmatched_codes:
        detail_rows.append(
            {
                "SKU": code,
                "품명": "(재고 데이터에 없음)",
                "재고 수량": 0,
                "재고 금액": 0,
                "월 사용수량": monthly_usage_map.get(code),
                "소진 예상(개월)": None,
                "예상 소진일": None,
            }
        )
    detail_df = pd.DataFrame(detail_rows).sort_values("재고 금액", ascending=False)
    display_detail_df = detail_df.copy()
    display_detail_df["예상 소진일"] = display_detail_df["예상 소진일"].fillna("-")
    st.dataframe(
        display_detail_df,
        hide_index=True,
        column_config={
            "재고 수량": st.column_config.NumberColumn(format="%,d개"),
            "재고 금액": st.column_config.NumberColumn(format="₩%,d"),
            "월 사용수량": st.column_config.NumberColumn(format="%.1f개"),
            "소진 예상(개월)": st.column_config.NumberColumn(format="%.1f개월"),
        },
    )
    st.caption(
        "월 사용수량은 최근 3개월(이번 달 제외) 전체 캠프 합산 평균이에요. "
        "소진 예상은 현재 재고 수량 ÷ 월 사용수량 기준의 단순 추정이라 실제와 다를 수 있어요."
    )
