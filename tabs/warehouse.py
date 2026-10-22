"""창고 현황 탭."""

from datetime import datetime

import pandas as pd
import streamlit as st

from formatting import fmt_int, fmt_won, render_trend_chart
from boxhero import (
    get_boxhero_token,
    fetch_boxhero_locations,
    fetch_boxhero_items,
    load_out_lines,
    sync_boxhero_out_tx,
    last_out_tx_time,
)
from formatting import KST
from snapshots import (
    save_warehouse_snapshot,
    current_inventory_period,
    save_inventory_value_snapshot,
    build_warehouse_value_snapshot_rows,
    load_warehouse_snapshots,
)
from usage_calc import estimate_depletion, weekly_usage_rate

OUT_PERIODS = {"최근 30일": 30, "최근 3개월": 90, "최근 6개월": 180, "최근 1년": 365, "전체": None}


def _priced_out_lines(days, wh_items):
    """저장된 출고 라인에 지금 판매가로 금액을 붙이고, 품목명/SKU는 박스히어로 최신 값으로 맞춘다."""
    df = load_out_lines(days).copy()
    if df.empty:
        df["amt"] = []
        return df
    by_id = {it["id"]: it for it in wh_items}
    df["price"] = df["item_id"].map(lambda i: float(by_id.get(i, {}).get("price") or 0))
    df["sku"] = df["item_id"].map(lambda i: by_id.get(i, {}).get("sku")).fillna(df["sku"])
    df["name"] = df["item_id"].map(lambda i: by_id.get(i, {}).get("name")).fillna(df["name"])
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
    df["amt"] = df["qty"] * df["price"]
    df["time_kst"] = pd.to_datetime(df["transaction_time"], utc=True).dt.tz_convert(KST)
    return df


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
    out30 = _priced_out_lines(30, wh_items)
    out30_by_sku = out30.groupby("sku")["qty"].sum().to_dict() if not out30.empty else {}
    wh_rows = []
    for it in wh_items:
        sku = it["sku"]
        qty = it.get("quantity", 0)
        price = float(it.get("price") or 0)
        weeks, depletion_date = estimate_depletion(usage, qty, sku)
        row = {
            "SKU": sku,
            "품목명": it["name"],
            "재고 수량": qty,
            "단가": price,
            "재고 금액": qty * price,
            "주 사용량": weekly_usage_rate(usage, sku),
            "소진 예상(주)": weeks,
            "예상 소진일": depletion_date.isoformat() if depletion_date else None,
            "30일 출고": out30_by_sku.get(sku, 0),
        }
        wh_rows.append(row)
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
            "30일 출고": st.column_config.NumberColumn(format="%,d개"),
        },
    )
    st.caption("\"30일 출고\"는 저장된 박스히어로 출고 이력 기준이에요 (아래에서 새 출고분을 가져올 수 있어요).")

    _render_out_history(wh_items)


def _render_out_history(wh_items):
    st.subheader("창고 출고 이력 (수량 · 금액)")
    info = last_out_tx_time()
    last_t = info["t"]
    if last_t is not None and not pd.isna(last_t):
        last_txt = pd.Timestamp(last_t).tz_convert(KST).strftime("%Y-%m-%d %H:%M")
        st.caption(f"저장된 출고 {int(info['n']):,}건 · 가장 최근 출고 {last_txt} (박스히어로 판매가 기준 금액)")
    else:
        st.caption("아직 저장된 출고 이력이 없어요. 아래 버튼으로 가져와주세요 (처음엔 오래 걸려요).")
    if st.button("박스히어로에서 새 출고 가져오기", key="sync_out_tx_btn", icon=":material/refresh:"):
        try:
            with st.spinner("새로 생긴 출고 건의 품목 상세를 받아오는 중이에요..."):
                n = sync_boxhero_out_tx()
        except Exception as e:
            st.error(f"출고 이력을 가져오는 중 문제가 발생했습니다: {e}", icon=":material/error:")
        else:
            st.toast(f"새 출고 {n:,}건을 저장했어요.", icon=":material/check_circle:")
            st.rerun()

    period_label = st.segmented_control(
        "기간", list(OUT_PERIODS), default="최근 30일", key="wh_out_period"
    ) or "최근 30일"
    days = OUT_PERIODS[period_label]
    df = _priced_out_lines(days, wh_items)
    if df.empty:
        st.caption("이 기간에 저장된 출고 이력이 없어요.")
        return

    span_days = days or max(1, (pd.Timestamp.now(tz=KST) - df["time_kst"].min()).days)
    k1, k2, k3 = st.columns(3)
    k1.metric(f"{period_label} 출고 수량", f"{fmt_int(df['qty'].sum())}개", border=True)
    k2.metric(f"{period_label} 출고 금액", fmt_won(df["amt"].sum()), border=True)
    k3.metric("출고 건수", f"{df['tx_id'].nunique():,}건", border=True)

    # 30일은 일별, 그보다 길면 월별로 추이를 본다.
    bucket = df["time_kst"].dt.strftime("%Y-%m-%d" if days == 30 else "%Y-%m")
    unit = "일별" if days == 30 else "월별"
    trend = df.assign(기간=bucket).groupby("기간", as_index=False).agg(출고수량=("qty", "sum"), 출고금액=("amt", "sum"))
    trend.columns = ["기간", "출고 수량", "출고 금액"]
    qty_tab, amt_tab = st.tabs([f"{unit} 출고 수량", f"{unit} 출고 금액"])
    with qty_tab:
        st.altair_chart(render_trend_chart(trend, "기간", "출고 수량"), width="stretch")
    with amt_tab:
        st.altair_chart(render_trend_chart(trend, "기간", "출고 금액"), width="stretch")
    if days != 30:
        st.caption("이번 달은 아직 진행 중이라 적게 보여요.")

    st.markdown(f"**품목별 출고량 ({period_label})**")
    stock_by_sku = {it["sku"]: it.get("quantity", 0) for it in wh_items}
    items = df.groupby("sku", as_index=False).agg(품목명=("name", "last"), qty=("qty", "sum"), amt=("amt", "sum"))
    items["주 평균 출고"] = items["qty"] / span_days * 7
    items["현재 창고 재고"] = items["sku"].map(stock_by_sku).fillna(0)
    items["출고 기준 소진 예상(주)"] = (items["현재 창고 재고"] / items["주 평균 출고"]).where(items["주 평균 출고"] > 0)
    items = items.rename(columns={"sku": "SKU", "qty": "출고 수량", "amt": "출고 금액"}).sort_values(
        "출고 수량", ascending=False
    )
    st.dataframe(
        items,
        hide_index=True,
        column_config={
            "출고 수량": st.column_config.NumberColumn(format="%,d개"),
            "출고 금액": st.column_config.NumberColumn(format="₩%,d"),
            "주 평균 출고": st.column_config.NumberColumn(format="%.1f개/주"),
            "현재 창고 재고": st.column_config.NumberColumn(format="%,d개"),
            "출고 기준 소진 예상(주)": st.column_config.NumberColumn(format="%.1f주"),
        },
    )
    st.caption(
        "\"출고 기준 소진 예상\"은 지금 창고 재고를 이 기간의 평균 출고 속도로 나눈 값이에요 "
        "(위 표의 소진 예상은 캠프 사용량 기준)."
    )

    with st.expander("출고 건별 내역", icon=":material/receipt_long:"):
        txs = (
            df.groupby("tx_id", as_index=False)
            .agg(일시=("time_kst", "first"), 메모=("memo", "first"), 품목수=("sku", "count"), 출고수량=("qty", "sum"), 출고금액=("amt", "sum"))
            .sort_values("일시", ascending=False)
        )
        txs["일시"] = txs["일시"].dt.strftime("%Y-%m-%d %H:%M")
        txs = txs.drop(columns=["tx_id"]).rename(columns={"품목수": "품목 수", "출고수량": "출고 수량", "출고금액": "출고 금액"})
        st.dataframe(
            txs,
            hide_index=True,
            column_config={
                "출고 금액": st.column_config.NumberColumn(format="₩%,d"),
                "출고 수량": st.column_config.NumberColumn(format="%,d개"),
            },
        )
