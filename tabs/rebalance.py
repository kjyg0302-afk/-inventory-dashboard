"""재고이관(캠프<>캠프) 탭."""

from datetime import timedelta

import pandas as pd
import streamlit as st

from formatting import (
    ACCENT_2,
    CHART_MUTED,
    fmt_int,
    fmt_request_timeline,
    fmt_transfer_qty,
    render_group_bars,
    render_trend_chart,
)
from reject_analysis import (
    BEFORE_WEEKS,
    build_tracking,
    camp_decision_stats,
    load_decided_requests,
    load_rejections,
    week_start_of,
)
from usage_calc import get_usage_for_code
from transfers import (
    list_all_transfer_requests,
    list_transfer_requests,
    list_transfer_requests_for_camp,
    create_transfer_request,
)
from rendering import render_pending_request_row, render_in_transit_request_row


def _render_reject_analysis(data):
    rej = load_rejections()
    if rej.empty:
        st.caption("아직 거절된 이관 요청이 없어요.")
        return

    st.markdown("**캠프별 승인·거절 현황**")
    stats = camp_decision_stats(load_decided_requests()).sort_values(
        ["거절 횟수", "승인 횟수"], ascending=False, na_position="last"
    )
    st.dataframe(
        stats,
        hide_index=True,
        column_config={
            "승인 횟수": st.column_config.NumberColumn(format="%d건"),
            "거절 횟수": st.column_config.NumberColumn(format="%d건"),
            "승인율(%)": st.column_config.ProgressColumn("승인율", format="%.1f%%", min_value=0, max_value=100),
            "승인받은 횟수": st.column_config.NumberColumn(format="%d건"),
            "거절당한 횟수": st.column_config.NumberColumn(format="%d건"),
            "승인받은 비율(%)": st.column_config.ProgressColumn("승인받은 비율", format="%.1f%%", min_value=0, max_value=100),
        },
    )
    st.caption(
        "승인·거절·승인율은 요청을 받은(보내는) 캠프 기준, 승인받은·거절당한·승인받은 비율은 요청한 캠프 기준이에요. "
        "아직 처리 전인 요청중 건은 빼고 계산해요. 승인은 이동중·입고완료를 모두 포함해요."
    )

    stock = {}
    for it in data["items"]:
        code = str(it["c"]).strip().upper()
        for camp, (q, _a) in it.get("x", {}).items():
            stock[(camp, code)] = q
    track, weekly, last_wk = build_tracking(rej, lambda camp, code: stock.get((camp, code), 0))

    st.markdown("**거절 품목 사용량 추적 (거절한 캠프 기준)**")
    if last_wk is not None:
        st.caption(
            f"사용량 데이터는 {last_wk:%Y-%m-%d} 시작 주까지 있어요. 이후 주는 사용량 엑셀을 올리면 채워져요. "
            f"거절 전 주평균은 최초 거절일 직전 {BEFORE_WEEKS}주 기준이에요."
        )
    track = track.sort_values(["거절 횟수", "거절 수량"], ascending=False)
    st.dataframe(
        track,
        hide_index=True,
        column_config={
            "거절 수량": st.column_config.NumberColumn(format="%,d개"),
            "현재 보유": st.column_config.NumberColumn(format="%,d개"),
            "거절 전 주평균": st.column_config.NumberColumn(format="%.1f개/주"),
            "거절 후 주평균": st.column_config.NumberColumn(format="%.1f개/주"),
            "거절 후 누적 사용": st.column_config.NumberColumn(format="%,d개"),
        },
    )
    st.caption(
        "\"거절 후 데이터 주수\"가 0이면 아직 거절 이후 사용량이 안 올라온 거예요. "
        "보유 재고는 많은데 거절 전·후 사용량이 거의 없다면, 쓰지 않는 재고를 쥐고 거절한 경우일 수 있어요."
    )

    labels = {f"{r['거절한 캠프']} · {r['품목명']} ({r['SKU']})": r for _, r in track.iterrows()}
    picked = labels[st.selectbox("주간 사용량을 볼 품목", list(labels.keys()), key="reject_track_pick")]
    w0 = week_start_of(picked["최초 거절일"])
    end = max(last_wk or w0, w0)
    weeks = [w0 - timedelta(weeks=BEFORE_WEEKS) + timedelta(weeks=i)
             for i in range((end - w0).days // 7 + BEFORE_WEEKS + 1)]
    s = weekly[(weekly["camp"] == picked["거절한 캠프"]) & (weekly["code"] == picked["SKU"])]
    qty_by_week = dict(zip(s["week_start"], s["qty"]))
    chart_df = pd.DataFrame({
        "주": [f"{w:%m/%d}주" for w in weeks],
        "사용량": [qty_by_week.get(w, 0) for w in weeks],
        "구간": ["거절 전" if w < w0 else "거절 후" for w in weeks],
    })
    st.altair_chart(
        render_group_bars(chart_df, "주", "사용량", "구간", {"거절 전": CHART_MUTED, "거절 후": ACCENT_2}),
        width="stretch",
    )
    st.caption(
        f"{picked['거절한 캠프']}에서 {picked['최초 거절일']}에 처음 거절 (요청한 캠프: {picked['요청한 캠프']}) · "
        f"현재 보유 {int(picked['현재 보유']):,}개"
        + (f" · 최근 거절 사유: {picked['최근 거절 사유']}" if picked["최근 거절 사유"] else "")
    )


def _render_my_camp_transfers(data, camp):
    """품목을 검색하지 않아도 내 캠프가 보내거나 받는 이관 요청을 한눈에 본다.
    진행중은 전부, 완료/거절은 최근 30일만."""
    with st.expander(f"📋 {camp} 재고이관 현황 (요청한 건 · 요청받은 건)", icon=":material/list_alt:", expanded=True):
        df = list_transfer_requests_for_camp(camp)
        if df.empty:
            st.caption("최근 30일간 이 캠프와 관련된 이관 요청이 없어요.")
            return

        my_name = st.text_input(
            "내 이름 (승인/거절/입고완료 시 기록)",
            value=st.session_state.get("transfer_my_name", ""),
            key="rebalance_mine_name_input",
        )
        st.session_state["transfer_my_name"] = my_name

        to_approve = df[(df["status"] == "requested") & (df["from_camp"] == camp)]
        to_receive = df[(df["status"] == "in_transit") & (df["to_camp"] == camp)]
        my_waiting = df[(df["status"] == "requested") & (df["to_camp"] == camp)]
        to_send = df[(df["status"] == "in_transit") & (df["from_camp"] == camp)]
        done = df[df["status"].isin(["completed", "rejected"])]

        if not to_approve.empty:
            st.markdown("**요청받은 건 · 승인/거절 필요**")
            for _, r in to_approve.iterrows():
                render_pending_request_row(r, data, my_name, key_prefix="mine_")
        if not to_receive.empty:
            st.markdown("**받을 물건 · 입고 확인 필요**")
            for _, r in to_receive.iterrows():
                render_in_transit_request_row(r, data, my_name, key_prefix="mine_")
        if not my_waiting.empty:
            st.markdown("**내가 요청한 건 · 상대 캠프 승인 대기중**")
            for _, r in my_waiting.iterrows():
                st.caption(
                    f":material/schedule: {r['item_name']} · {r['from_camp']} → {r['to_camp']} · "
                    f"{fmt_transfer_qty(r)} · 요청자: {r['requested_by']} · {fmt_request_timeline(r)}"
                )
        if not to_send.empty:
            st.markdown("**보낼 물건 · 승인됨 (받는 캠프 입고완료 대기)**")
            for _, r in to_send.iterrows():
                st.caption(
                    f":material/local_shipping: {r['item_name']} · {r['from_camp']} → {r['to_camp']} · "
                    f"{fmt_transfer_qty(r)} · 승인자: {r['approved_by']} · {fmt_request_timeline(r)}"
                )
        if to_approve.empty and to_receive.empty and my_waiting.empty and to_send.empty:
            st.caption("지금 진행 중인 이관 요청은 없어요.")

        if not done.empty:
            with st.expander(f"최근 30일 완료/거절 내역 ({len(done)}건)", icon=":material/history:"):
                for _, r in done.iterrows():
                    direction = "보냄" if r["from_camp"] == camp else "받음"
                    dc0, dc1 = st.columns([1, 5])
                    if r["status"] == "completed":
                        dc0.badge(f"입고완료·{direction}", icon=":material/check_circle:", color="green")
                    else:
                        dc0.badge(f"거절·{direction}", icon=":material/cancel:", color="red")
                    reason_suffix = f" · 사유: {r['reject_reason']}" if pd.notna(r.get("reject_reason")) else ""
                    dc1.caption(
                        f"{r['item_name']} · {r['from_camp']} → {r['to_camp']} · {fmt_transfer_qty(r)}"
                        f"{reason_suffix} · {fmt_request_timeline(r)}"
                    )


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
                        render_in_transit_request_row(r, data, admin_tr_name, key_prefix="all_", allow_revert=True)

    if auth["role"] == "admin":
        with st.expander("📉 거절 분석 (관리자 전용)", icon=":material/insights:"):
            _render_reject_analysis(data)

    if my_login_camp:
        _render_my_camp_transfers(data, my_login_camp)

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
                        f"{fmt_transfer_qty(r)} · 요청자: {r['requested_by']} (상대 캠프 승인 대기중) · "
                        f"{fmt_request_timeline(r)}"
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
                        f"{fmt_transfer_qty(r)} · 승인자: {r['approved_by']} (입고 대기중) · "
                        f"{fmt_request_timeline(r)}"
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
                    reason_suffix = f" · 사유: {r['reject_reason']}" if pd.notna(r.get("reject_reason")) else ""
                    dc1.caption(
                        f"{r['from_camp']} → {r['to_camp']} · {fmt_transfer_qty(r)} · {who}{reason_suffix} · "
                        f"{fmt_request_timeline(r)}"
                    )

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
