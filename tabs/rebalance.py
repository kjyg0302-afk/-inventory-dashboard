"""재고이관(캠프<>캠프) 탭."""

import pandas as pd
import streamlit as st

from formatting import fmt_int, render_trend_chart
from usage_calc import get_usage_for_code
from transfers import list_all_transfer_requests, list_transfer_requests, create_transfer_request
from rendering import render_pending_request_row, render_in_transit_request_row


def render(ctx):
    data = ctx.data
    usage = ctx.usage
    auth = ctx.auth
    my_login_camp = ctx.my_login_camp

    if auth["role"] == "admin":
        with st.expander("🔄 전체 재고이관(캠프<>캠프) 현황", icon=":material/list_alt:", expanded=True):
            all_tr_df = list_all_transfer_requests()
            all_requested = all_tr_df[all_tr_df["status"] == "requested"] if not all_tr_df.empty else all_tr_df
            all_in_transit = all_tr_df[all_tr_df["status"] == "in_transit"] if not all_tr_df.empty else all_tr_df

            if all_requested.empty and all_in_transit.empty:
                st.caption("현재 진행 중인 이관 요청이 없어요.")
            else:
                admin_tr_name = st.text_input(
                    "내 이름 (승인/거절/입고완료 시 기록)",
                    value=st.session_state.get("transfer_my_name", ""),
                    key="rebalance_all_name_input",
                )
                st.session_state["transfer_my_name"] = admin_tr_name

                if not all_requested.empty:
                    st.markdown("**요청중**")
                    for _, r in all_requested.iterrows():
                        render_pending_request_row(r, data, admin_tr_name, key_prefix="all_")

                if not all_in_transit.empty:
                    st.markdown("**이동중**")
                    for _, r in all_in_transit.iterrows():
                        render_in_transit_request_row(r, data, admin_tr_name, key_prefix="all_")

    st.caption("품목을 선택하면 캠프별 재고 편차를 확인할 수 있어요")
    query = st.text_input("재분배를 검토할 품목명 또는 번호 입력", "", key="rebalance_query")
    selected = None
    if query.strip():
        q = query.strip().lower()
        all_candidates = [it for it in data["items"] if q in it["n"].lower() or q in str(it["c"]).lower()]
        candidates = all_candidates[:15]
        if candidates:
            if len(all_candidates) > len(candidates):
                st.caption(
                    f"검색 결과 {len(all_candidates)}건 중 상위 {len(candidates)}개만 표시했어요. "
                    "검색어를 더 구체적으로 입력하면 찾는 품목이 더 잘 보여요."
                )
            options = {f"{it['n']} ({it['c']})": it for it in candidates}
            picked_label = st.radio("검색 결과", list(options.keys()), index=0)
            selected = options[picked_label]
        else:
            st.info("일치하는 품목이 없습니다.", icon=":material/search_off:")

    if selected:
        item_usage = get_usage_for_code(usage, selected["c"])
        req_df = list_transfer_requests(selected["c"])
        if not req_df.empty:
            # 승인(이동중) 상태는 아직 실제 재고에서 빠지지 않았지만(입고완료 시에만 차감),
            # 이미 다른 곳으로 나가기로 확정된 수량이라 "가용재고" 계산에서는 미리 빼준다.
            in_transit_out = req_df[req_df["status"] == "in_transit"].groupby("from_camp")["qty"].sum()
        else:
            in_transit_out = pd.Series(dtype=int)

        rows = []
        for camp in data["campsOrder"]:
            pair = selected["x"].get(camp)
            qty = pair[0] if pair else 0
            intransit_qty = int(in_transit_out.get(camp, 0))
            u = item_usage.get(camp) if item_usage else None
            avg = u["avg"] if u else None
            weeks_of_stock = round(qty / avg, 1) if avg and avg > 0 else None
            rows.append(
                {
                    "팀": data["campToTeam"].get(camp, "-"),
                    "캠프": camp,
                    "재고 수량": qty,
                    "가용재고": qty - intransit_qty,  # 재고 수량 - 이동중(승인됐지만 아직 미입고)
                    "이동중재고": intransit_qty,  # 승인되어 이 캠프에서 나가는 중인 수량 (입고완료 전)
                    "주 평균 사용량": avg,  # None(NaN) 또는 숫자
                    "소진 예상(주)": weeks_of_stock,  # None(NaN) 또는 숫자
                }
            )
        df_rows = pd.DataFrame(rows).sort_values("재고 수량", ascending=False)

        # 재분배 제안 (NaN은 항상 False로 비교되므로 안전)
        suggested_transfer = None
        shortages = df_rows[(df_rows["재고 수량"] == 0) & (df_rows["주 평균 사용량"] > 0)]
        if not shortages.empty:
            donors = df_rows[(df_rows["재고 수량"] > 1)]
            donors = donors[donors["소진 예상(주)"].isna() | (donors["소진 예상(주)"] > 4)]
            donors = donors.sort_values("재고 수량", ascending=False)
            if not donors.empty:
                top = donors.iloc[0]
                move_qty = max(1, int(top["재고 수량"] // 2))
                to_camps = ", ".join(shortages["캠프"].head(3).tolist())
                suggested_transfer = {"from": top["캠프"], "to": shortages["캠프"].iloc[0], "qty": move_qty}
                st.warning(
                    f"**{top['캠프']}**에 {fmt_int(top['재고 수량'])}개 보유 중인 반면, **{to_camps}**에는 재고가 없습니다. "
                    f"약 {move_qty}개 이동을 검토해보세요. (최근 사용량 데이터를 반영한 제안)",
                    icon=":material/lightbulb:",
                )
        else:
            with_stock = df_rows[df_rows["재고 수량"] > 0]
            empty = df_rows[df_rows["재고 수량"] == 0]
            if not with_stock.empty and not empty.empty and with_stock.iloc[0]["재고 수량"] >= 2:
                top = with_stock.iloc[0]
                move_qty = max(1, int(top["재고 수량"] // 2))
                to_camps = ", ".join(empty["캠프"].head(3).tolist())
                suggested_transfer = {"from": top["캠프"], "to": empty["캠프"].iloc[0], "qty": move_qty}
                st.warning(
                    f"**{top['캠프']}**에 {fmt_int(top['재고 수량'])}개 보유 중인 반면, **{to_camps}**에는 재고가 없습니다. "
                    f"약 {move_qty}개 이동을 검토해보세요. (사용량 데이터가 없어 재고량만 기준으로 한 참고용 제안)",
                    icon=":material/lightbulb:",
                )

        st.subheader("캠프 간 재고 이관")
        st.caption("요청 → 승인(이동중) → 입고완료 순서로 처리돼요. 실제 재고 수량은 입고완료 시점에 보내는 캠프에서 빠지고 받는 캠프에 더해지며, 승인되면 그 전까지는 \"가용재고\"에서만 미리 제외되어 보입니다.")

        my_name = st.text_input(
            "내 이름", value=st.session_state.get("transfer_my_name", ""), key="transfer_my_name_input"
        )
        st.session_state["transfer_my_name"] = my_name

        item_code = selected["c"]
        with st.form(f"transfer_request_form_{item_code}"):
            # 물류창고도 보내는/받는 쪽으로 고를 수 있게 캠프 목록 끝에 추가한다 (물류창고 재고는
            # 이 앱의 캠프 재고 데이터에 없어서 "캠프별 재고 현황" 표에는 넣지 않고, 요청 생성용
            # 선택지로만 쓴다 — 가용재고 검증은 승인/입고완료 처리 쪽에서 물류창고만 건너뛴다).
            camps_order = data["campsOrder"]
            camps_order_with_wh = camps_order + ["물류창고"]
            default_from = camps_order.index(suggested_transfer["from"]) if suggested_transfer else 0
            default_to = camps_order.index(suggested_transfer["to"]) if suggested_transfer else min(1, len(camps_order) - 1)
            fc1, fc2, fc3 = st.columns([2, 2, 1])
            with fc1:
                from_camp_sel = st.selectbox("보내는 캠프", camps_order_with_wh, index=default_from)
            with fc2:
                to_camp_sel = st.selectbox("받는 캠프", camps_order_with_wh, index=default_to)
            with fc3:
                qty_sel = st.number_input(
                    "수량", min_value=1, step=1, value=suggested_transfer["qty"] if suggested_transfer else 1
                )
            if st.form_submit_button("이관 요청"):
                if not my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                elif from_camp_sel == to_camp_sel:
                    st.error("보내는 캠프와 받는 캠프가 같습니다.", icon=":material/error:")
                else:
                    create_transfer_request(
                        item_code, selected["n"], from_camp_sel, to_camp_sel, int(qty_sel), my_name.strip()
                    )
                    st.success("이관 요청을 등록했습니다.", icon=":material/send:")
                    list_transfer_requests.clear()
                    list_all_transfer_requests.clear()
                    st.rerun()

        req_df = list_transfer_requests(item_code)
        if my_login_camp and not req_df.empty:
            # 캠프로 로그인했으면 나와 관련된(보내거나 받는) 요청만 보여준다.
            req_df = req_df[(req_df["from_camp"] == my_login_camp) | (req_df["to_camp"] == my_login_camp)]
        requested_rows = req_df[req_df["status"] == "requested"] if not req_df.empty else req_df
        in_transit_rows = req_df[req_df["status"] == "in_transit"] if not req_df.empty else req_df
        done_rows = req_df[req_df["status"].isin(["completed", "rejected"])] if not req_df.empty else req_df

        if not requested_rows.empty:
            st.markdown("**요청중**")
            for _, r in requested_rows.iterrows():
                if not my_login_camp or r["from_camp"] == my_login_camp:
                    render_pending_request_row(r, data, my_name)
                else:
                    # 내가 승인 주체가 아니라 내가 요청한 쪽(to_camp) — 진행 상황만 보여준다.
                    st.caption(
                        f":material/schedule: {r['item_name']} · {r['from_camp']} → {r['to_camp']} · "
                        f"{int(r['qty'])}개 · 요청자: {r['requested_by']} (상대 캠프 승인 대기중)"
                    )

        if not in_transit_rows.empty:
            st.markdown("**이동중**")
            for _, r in in_transit_rows.iterrows():
                if not my_login_camp or r["to_camp"] == my_login_camp:
                    render_in_transit_request_row(r, data, my_name)
                else:
                    # 내가 입고완료 주체가 아니라 보낸 쪽(from_camp) — 진행 상황만 보여준다.
                    st.caption(
                        f":material/local_shipping: {r['item_name']} · {r['from_camp']} → {r['to_camp']} · "
                        f"{int(r['qty'])}개 · 승인자: {r['approved_by']} (입고 대기중)"
                    )

        if not done_rows.empty and auth["role"] == "admin":
            with st.expander(f"완료/거절 내역 ({len(done_rows)}건)", icon=":material/history:"):
                for _, r in done_rows.iterrows():
                    is_done = r["status"] == "completed"
                    who = r["received_by"] if is_done else r["approved_by"]
                    dc0, dc1 = st.columns([1, 5])
                    if is_done:
                        dc0.badge("입고완료", icon=":material/check_circle:", color="green")
                    else:
                        dc0.badge("거절", icon=":material/cancel:", color="red")
                    dc1.caption(f"{r['from_camp']} → {r['to_camp']} · {int(r['qty'])}개 · {who}")

        if item_usage:
            weekly_totals = {}
            for camp, u in item_usage.items():
                for y, w, qv in u["w"]:
                    key = (y, w)
                    weekly_totals[key] = weekly_totals.get(key, 0) + qv
            trend = sorted(weekly_totals.items())[-12:]
            if trend:
                trend_df = pd.DataFrame(
                    {"주차": [f"{y}-{w}" for (y, w), _ in trend], "사용량": [v for _, v in trend]}
                )
                st.subheader("전체 캠프 합산 · 최근 12주 사용량 추이")
                st.altair_chart(render_trend_chart(trend_df, "주차", "사용량"), width="stretch")
        else:
            st.caption("이 품목의 사용량 데이터가 아직 없습니다. (재고 수량만으로 비교합니다)")

        st.dataframe(
            df_rows,
            hide_index=True,
            column_config={
                "재고 수량": st.column_config.NumberColumn(format="%,d개"),
                "가용재고": st.column_config.NumberColumn(format="%,d개"),
                "이동중재고": st.column_config.NumberColumn(format="%,d개"),
                "주 평균 사용량": st.column_config.NumberColumn(format="%.1f개/주"),
                "소진 예상(주)": st.column_config.NumberColumn(format="%.1f주"),
            },
        )
