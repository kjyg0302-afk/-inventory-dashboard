"""창고 현황 탭."""

from datetime import datetime

import pandas as pd
import streamlit as st

from formatting import fmt_int, fmt_won, render_trend_chart
from boxhero import (
    get_boxhero_token,
    fetch_boxhero_locations,
    fetch_boxhero_items,
    fetch_boxhero_recent_out_transactions,
)
from snapshots import (
    save_warehouse_snapshot,
    current_inventory_period,
    save_inventory_value_snapshot,
    build_warehouse_value_snapshot_rows,
    load_warehouse_snapshots,
)
from usage_calc import estimate_depletion, weekly_usage_rate


def render(ctx):
    usage = ctx.usage
    if not get_boxhero_token():
        st.info(
            "박스히어로 API 토큰이 설정되지 않았어요. .streamlit/secrets.toml.example을 참고해 등록해주세요.",
            icon=":material/link_off:",
        )
        return

    try:
        with st.spinner("박스히어로에서 창고 데이터를 가져오는 중이에요..."):
            wh_locations = fetch_boxhero_locations()
            wh_items = fetch_boxhero_items()
    except Exception as e:
        st.error(f"박스히어로 연동 중 문제가 발생했습니다: {e}", icon=":material/error:")
        return

    warehouse_qty = sum(it.get("quantity", 0) for it in wh_items)
    warehouse_amt = sum(it.get("quantity", 0) * float(it.get("price") or 0) for it in wh_items)
    warehouse_name = wh_locations[0]["name"] if wh_locations else "물류창고"

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("창고 재고 수량", f"{fmt_int(warehouse_qty)}개", border=True)
    k2.metric("창고 재고 금액", fmt_won(warehouse_amt), border=True)
    k3.metric("창고 품목 수", f"{len(wh_items):,}종", border=True)
    k4.metric("창고", warehouse_name, border=True)
    st.caption(
        "박스히어로 API에서 5분 주기로 새로 가져온 데이터예요. "
        "창고 재고 금액은 원가(cost) 입력이 대부분 비어있어 판매가(price) 기준으로 계산했어요."
    )

    _today_str = datetime.now().date().isoformat()
    if st.session_state.get("_wh_snap_date") != _today_str:
        try:
            save_warehouse_snapshot(warehouse_qty, warehouse_amt, len(wh_items))
            st.session_state["_wh_snap_date"] = _today_str
        except Exception:
            pass  # 스냅샷 저장에 실패해도 화면 표시는 계속 진행

    _cur_period_label = current_inventory_period()[2]
    if st.session_state.get("_wh_value_snap_period") != _cur_period_label:
        try:
            save_inventory_value_snapshot("warehouse", build_warehouse_value_snapshot_rows(wh_items))
            st.session_state["_wh_value_snap_period"] = _cur_period_label
        except Exception:
            pass  # 스냅샷 저장에 실패해도 화면 표시는 계속 진행

    st.subheader("월별 물류창고 재고 금액")
    snapshots = load_warehouse_snapshots()
    if snapshots.empty:
        st.caption("아직 쌓인 스냅샷이 없어요.")
    else:
        snapshots = snapshots.copy()
        snapshots["월"] = pd.to_datetime(snapshots["snapshot_date"]).dt.strftime("%Y-%m")
        monthly_wh = snapshots.groupby("월", as_index=False).last()[["월", "total_amt"]]
        monthly_wh.columns = ["월", "재고 금액"]
        st.altair_chart(render_trend_chart(monthly_wh, "월", "재고 금액"), width="stretch")
        st.dataframe(
            monthly_wh.sort_values("월", ascending=False),
            hide_index=True,
            column_config={"재고 금액": st.column_config.NumberColumn(format="₩%,d")},
        )
        st.caption(
            "박스히어로 API는 현재 시점 재고만 알려줘서, 창고 현황 탭을 열 때마다 그날의 "
            "스냅샷을 기록해 추이를 쌓고 있어요. 과거 데이터는 없어 오늘부터 시작돼요."
        )

    st.subheader("품목별 창고 재고")
    st.caption("주 사용량/예상 소진일은 캠프 사용량 엑셀 기준(전체 캠프 합산)이라, 사용량 데이터가 없는 SKU는 \"-\"로 표시돼요.")
    wh_search = st.text_input("품목명 또는 SKU로 검색", "", key="warehouse_search")
    wh_rows = []
    for it in wh_items:
        sku = it["sku"]
        qty = it.get("quantity", 0)
        price = float(it.get("price") or 0)
        weeks, depletion_date = estimate_depletion(usage, qty, sku)
        wh_rows.append(
            {
                "SKU": sku,
                "품목명": it["name"],
                "재고 수량": qty,
                "단가": price,
                "재고 금액": qty * price,
                "주 사용량": weekly_usage_rate(usage, sku),
                "소진 예상(주)": weeks,
                "예상 소진일": depletion_date.isoformat() if depletion_date else None,
            }
        )
    wh_df = pd.DataFrame(wh_rows).sort_values("재고 금액", ascending=False)
    if wh_search.strip():
        q = wh_search.strip().lower()
        wh_df = wh_df[
            wh_df["품목명"].str.lower().str.contains(q, regex=False)
            | wh_df["SKU"].str.lower().str.contains(q, regex=False)
        ]
    display_wh_df = wh_df.copy()
    display_wh_df["예상 소진일"] = display_wh_df["예상 소진일"].fillna("-")
    st.dataframe(
        display_wh_df,
        hide_index=True,
        column_config={
            "재고 수량": st.column_config.NumberColumn(format="%,d개"),
            "단가": st.column_config.NumberColumn(format="₩%,d"),
            "재고 금액": st.column_config.NumberColumn(format="₩%,d"),
            "주 사용량": st.column_config.NumberColumn(format="%.1f개/주"),
            "소진 예상(주)": st.column_config.NumberColumn(format="%.1f주"),
        },
    )

    st.subheader("최근 30일 출고 금액")
    st.caption(
        "건별 품목 상세를 하나씩 조회해서 계산하기 때문에 건수가 많으면 1~2분 걸려요. "
        "그래서 자동으로 돌리지 않고, 버튼을 눌렀을 때만 계산해요 (다른 탭 조작 속도에 영향 없게)."
    )
    if st.button("출고 이력 불러오기 / 새로고침", key="load_out_tx_btn", icon=":material/refresh:"):
        try:
            with st.spinner("건별 품목 상세를 조회해 판매가 기준 출고 금액을 계산하는 중이에요... (건수가 많으면 1~2분 걸릴 수 있어요)"):
                st.session_state["wh_out_txs"] = fetch_boxhero_recent_out_transactions(days=30)
        except Exception as e:
            st.error(f"출고 이력을 가져오는 중 문제가 발생했습니다: {e}", icon=":material/error:")

    out_txs = st.session_state.get("wh_out_txs")
    if out_txs is None:
        st.caption("위 버튼을 눌러 최근 30일 출고 이력을 불러오세요.")
    else:
        if not out_txs:
            st.caption("최근 30일간 출고 이력이 없어요.")
        else:
            total_out_amt = sum(tx["amt"] for tx in out_txs)
            st.caption(f"최근 30일 출고 금액 합계: {fmt_won(total_out_amt)} (판매가 기준, {len(out_txs):,}건)")

            daily = {}
            for tx in out_txs:
                day = tx["transaction_time"][:10]
                daily[day] = daily.get(day, 0) + tx["amt"]
            daily_df = pd.DataFrame(sorted(daily.items()), columns=["날짜", "출고 금액"])
            st.altair_chart(render_trend_chart(daily_df, "날짜", "출고 금액"), width="stretch")

            tx_rows = [
                {
                    "일시": tx["transaction_time"][:16].replace("T", " "),
                    "출고 금액": tx["amt"],
                    "출고 수량": abs(tx.get("total_quantity", 0)),
                    "품목 수": tx.get("count_of_items", 0),
                    "메모": tx.get("memo", ""),
                }
                for tx in out_txs
            ]
            st.dataframe(
                pd.DataFrame(tx_rows),
                hide_index=True,
                column_config={
                    "출고 금액": st.column_config.NumberColumn(format="₩%,d"),
                    "출고 수량": st.column_config.NumberColumn(format="%,d개"),
                },
            )
