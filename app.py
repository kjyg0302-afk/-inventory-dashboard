"""
지바이크 SCM 대시보드 (Streamlit)
- 태블로에서 받은 재고 엑셀(피벗 형식)과, scm_data.db에서 export_weekly_usage.py로
  뽑은 주단위 사용량 JSON(또는 사용량 엑셀)을 업로드하면 대시보드가 채워집니다.
- 업로드한 데이터는 Supabase(Postgres) DB에 저장되어, 다시 접속하는 모든 사람에게
  그대로 보입니다. 앱이 잠들었다 깨어나거나 재배포되어도 DB에 저장된 데이터는
  유지됩니다. (연결 설정은 .streamlit/secrets.toml.example, schema.sql 참고)
"""

import json

import pandas as pd
import streamlit as st

from parsing import (
    parse_inventory_excel,
    parse_usage_excel,
    parse_usage_excel_raw,
    parse_usage_transactions_raw,
)
from tableau import get_tableau_config, fetch_tableau_inventory
from auth import (
    verify_password,
    list_camp_credential_names,
    get_camp_password_hash,
    set_camp_password,
)
from context import TabContext
import tabs.overview
import tabs.camps
import tabs.items
import tabs.rebalance
import tabs.usage_amount
import tabs.category
import tabs.forecast
import tabs.total
import tabs.purchase
import tabs.warehouse
from rendering import (
    render_pending_request_row,
    render_in_transit_request_row,
    render_pending_warehouse_order_row,
    render_incoming_warehouse_order_row,
)
from purchase_orders import list_pending_warehouse_orders, list_incoming_warehouse_orders_for_camp
from snapshots import current_inventory_period, save_inventory_value_snapshot, build_camp_value_snapshot_rows
from transfers import (
    list_pending_transfer_requests_for_camp,
    list_outgoing_transit_requests_for_camp,
    list_incoming_transit_requests_for_camp,
    list_my_requested_awaiting_approval,
)
from usage_data import save_usage_facts, save_usage_transactions
from app_data import (
    load_data,
    save_data,
    backup_data_before_overwrite,
    load_camp_tab_permissions,
    save_camp_tab_permissions,
    load_warehouse_tab_permissions,
    save_warehouse_tab_permissions,
)
from boxhero import apply_boxhero_names_to_inventory, apply_boxhero_names_to_usage
from chat_notify import load_camp_chat_webhooks, save_camp_chat_webhook, delete_camp_chat_webhook

st.set_page_config(page_title="지바이크 SCM 대시보드", layout="wide")

st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header[data-testid="stHeader"] {background: transparent;}
    .block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1300px;}
    </style>
    """,
    unsafe_allow_html=True,
)

# 탭 키 -> 라벨. 관리자는 전체를 보고, 캠프/물류창고 로그인은 허용된 탭만 본다
# (기본값은 재고이관 2개 — load_camp_tab_permissions 참고).
TAB_DEFS = [
    ("overview", ":material/dashboard: 개요"),
    ("camps", ":material/location_on: 캠프별 현황"),
    ("items", ":material/search: 품목 검색"),
    ("rebalance", ":material/sync_alt: 재고이관(캠프<>캠프)"),
    ("usage_amount", ":material/payments: 월별 사용 금액"),
    ("category", ":material/category: 카테고리별 현황"),
    ("forecast", ":material/query_stats: FORECAST"),
    ("warehouse", ":material/warehouse: 창고 현황"),
    ("purchase", ":material/local_shipping: 재고이관(창고<>캠프)"),
    ("total", ":material/inventory: 지바이크 전체 재고"),
]





# ---------------- 로그인 ----------------
# 개인별 계정이 아니라, 캠프 하나당 비밀번호 하나를 공유하는 가벼운 방식. 관리자는 별도
# 마스터 비밀번호(secrets.toml)로 로그인해서 캠프 제한 없이 전체를 보고 캠프 비밀번호도 관리한다.

def render_login():
    st.markdown(
        """
        <div style="display:flex;align-items:center;gap:14px;margin:48px 0 28px;">
            <div style="width:42px;height:42px;border-radius:50%;flex-shrink:0;
                        background:linear-gradient(135deg, #6FA0FF, #4A6FE0);
                        box-shadow:0 4px 16px rgba(74,111,224,0.45);
                        display:flex;align-items:center;justify-content:center;">
                <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="white"
                     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                    <polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
                    <line x1="12" y1="22.08" x2="12" y2="12"/>
                </svg>
            </div>
            <div style="font-size:21px;font-weight:700;letter-spacing:-0.01em;">지바이크 SCM 대시보드</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    login_mode = st.radio(
        "로그인 방식", ["캠프로 로그인", "물류창고로 로그인", "관리자로 로그인"], horizontal=True
    )
    with st.form("login_form"):
        if login_mode == "캠프로 로그인":
            camps = [c for c in list_camp_credential_names() if c != "물류창고"]
            camp = st.selectbox("캠프", camps) if camps else None
            pin = st.text_input("비밀번호", type="password")
            submitted = st.form_submit_button("로그인", type="primary", icon=":material/login:")
            if submitted:
                if not camps:
                    st.error("등록된 캠프 계정이 없어요. 관리자에게 문의해주세요.", icon=":material/error:")
                elif verify_password(pin, get_camp_password_hash(camp)):
                    st.session_state["auth"] = {"role": "camp", "camp": camp}
                    st.rerun()
                else:
                    st.error("비밀번호가 올바르지 않습니다.", icon=":material/error:")
        elif login_mode == "물류창고로 로그인":
            pin = st.text_input("비밀번호", type="password")
            submitted = st.form_submit_button("로그인", type="primary", icon=":material/login:")
            if submitted:
                if verify_password(pin, get_camp_password_hash("물류창고")):
                    st.session_state["auth"] = {"role": "camp", "camp": "물류창고"}
                    st.rerun()
                else:
                    st.error("비밀번호가 올바르지 않습니다.", icon=":material/error:")
        else:
            admin_pw = st.text_input("관리자 비밀번호", type="password")
            submitted = st.form_submit_button("로그인", type="primary", icon=":material/login:")
            if submitted:
                try:
                    correct = st.secrets["admin"]["password"]
                except Exception:
                    correct = None
                if correct and admin_pw == correct:
                    st.session_state["auth"] = {"role": "admin", "camp": None}
                    st.rerun()
                else:
                    st.error("비밀번호가 올바르지 않습니다.", icon=":material/error:")


if not st.session_state.get("auth"):
    render_login()
    st.stop()


# ---------------- 세션 상태 로드 ----------------

if "inventory" not in st.session_state:
    st.session_state.inventory = load_data("inventory")
if "usage" not in st.session_state:
    st.session_state.usage = load_data("usage")

data = st.session_state.inventory
usage = st.session_state.usage


# ---------------- 헤더 ----------------

col_title, col_upload1, col_upload2 = st.columns([3, 1, 1])
with col_title:
    st.markdown(
        """
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:2px;">
            <div style="width:42px;height:42px;border-radius:50%;flex-shrink:0;
                        background:linear-gradient(135deg, #6FA0FF, #4A6FE0);
                        box-shadow:0 4px 16px rgba(74,111,224,0.45);
                        display:flex;align-items:center;justify-content:center;">
                <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="white"
                     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                    <polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
                    <line x1="12" y1="22.08" x2="12" y2="12"/>
                </svg>
            </div>
            <div style="font-size:21px;font-weight:700;letter-spacing:-0.01em;">지바이크 SCM 대시보드</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    updated = data["updatedAt"][:16].replace("T", " ") if data else "-"
    st.caption(f"전 캠프 재고 데이터 · 마지막 업데이트 {updated}")

with col_upload1:
    inv_file = st.file_uploader("재고 엑셀로 갱신", type=["xlsx", "xls"], key="inv_upload")
    # file_uploader는 업로드된 파일을 계속 들고 있어서, 다른 상호작용으로 스크립트가
    # 재실행될 때마다 이 블록이 다시 돌지 않도록 이미 처리한 파일인지 확인한다.
    if inv_file is not None and st.session_state.get("_inv_file_id") != inv_file.file_id:
        try:
            parsed = parse_inventory_excel(inv_file)
            parsed = apply_boxhero_names_to_inventory(parsed)
            backup_data_before_overwrite("inventory", "재고 엑셀 업로드")
            save_data("inventory", parsed)
            st.session_state.inventory = parsed
            data = parsed
            st.session_state["_inv_file_id"] = inv_file.file_id
            st.success(f"업데이트 완료 · 품목 {len(parsed['items']):,}개 · 캠프 {len(parsed['campsOrder'])}곳", icon=":material/check_circle:")
        except Exception as e:
            st.error(f"파일을 읽는 중 문제가 발생했습니다: {e}", icon=":material/error:")

    if get_tableau_config():
        if st.button("태블로에서 바로 동기화", icon=":material/sync:", key="tableau_sync_btn"):
            try:
                with st.spinner("태블로에서 재고 데이터를 받아오는 중이에요..."):
                    parsed = fetch_tableau_inventory()
                parsed = apply_boxhero_names_to_inventory(parsed)
                backup_data_before_overwrite("inventory", "태블로 동기화")
                save_data("inventory", parsed)
                st.session_state.inventory = parsed
                data = parsed
                st.success(f"동기화 완료 · 품목 {len(parsed['items']):,}개 · 캠프 {len(parsed['campsOrder'])}곳", icon=":material/check_circle:")
            except Exception as e:
                st.error(f"태블로 동기화 중 문제가 발생했습니다: {e}", icon=":material/error:")

with col_upload2:
    usage_file = st.file_uploader(
        "사용량 데이터 갱신 (엑셀 또는 JSON)", type=["xlsx", "xls", "json"], key="usage_upload"
    )
    if usage_file is not None and st.session_state.get("_usage_file_id") != usage_file.file_id:
        try:
            with st.spinner("사용량 데이터를 처리하는 중이에요... (대용량 엑셀은 시간이 걸릴 수 있어요)"):
                if usage_file.name.lower().endswith(".json"):
                    parsed_usage = json.load(usage_file)
                    if "items" not in parsed_usage:
                        raise ValueError("사용량 JSON 형식이 올바르지 않습니다.")
                    parsed_usage, _ = apply_boxhero_names_to_usage(parsed_usage)
                else:
                    preview_cols = set(pd.read_excel(usage_file, nrows=0).columns)
                    usage_file.seek(0)
                    is_raw_format = {"상태", "수량", "부품계"} <= preview_cols
                    if is_raw_format:
                        parsed_usage = parse_usage_excel_raw(usage_file)
                    else:
                        parsed_usage = parse_usage_excel(usage_file)
                    facts_df = parsed_usage.pop("facts")
                    parsed_usage, facts_df = apply_boxhero_names_to_usage(parsed_usage, facts_df)
                    save_usage_facts(facts_df)
                    if is_raw_format:
                        usage_file.seek(0)
                        tx_df = parse_usage_transactions_raw(usage_file)
                        save_usage_transactions(tx_df)
                backup_data_before_overwrite("usage", "사용량 데이터 업로드")
                save_data("usage", parsed_usage)
            st.session_state.usage = parsed_usage
            usage = parsed_usage
            st.session_state["_usage_file_id"] = usage_file.file_id
            st.success(f"사용량 갱신 완료 · 부품 {len(parsed_usage['items']):,}종", icon=":material/check_circle:")
        except Exception as e:
            st.error(f"사용량 파일을 읽는 중 문제가 발생했습니다: {e}", icon=":material/error:")

if usage and usage.get("weekRange"):
    wr = usage["weekRange"]
    st.info(f"사용량 데이터 기준: {wr['from'][0]}-{wr['from'][1]}주 ~ {wr['to'][0]}-{wr['to'][1]}주 ({wr['count']}주)", icon=":material/insights:")

st.divider()

if not data:
    st.warning("아직 업로드된 재고 데이터가 없어요. 위에서 재고 엑셀을 업로드해주세요.", icon=":material/upload_file:")
    st.stop()


# ---------------- 로그인 상태 / 내 캠프 알림 ----------------

_login_accounts = data["campsOrder"] + ["물류창고"]

auth = st.session_state["auth"]
# 캠프로 로그인했을 때만 실제 소속 캠프로 제한한다. 관리자는 "캠프로 보기(my_camp)"를
# 선택해도 탭 안의 이관 요청 목록 자체는 전부 보이는 게 맞아서 필터링하지 않는다.
my_login_camp = auth["camp"] if auth["role"] == "camp" else None
col_auth1, col_auth2 = st.columns([4, 1])
with col_auth1:
    if auth["role"] == "camp":
        st.caption(f":material/lock: **{auth['camp']}**로 로그인됨")
        my_camp = auth["camp"]
    else:
        st.caption(":material/lock: **관리자**로 로그인됨")
        admin_view_camp = st.selectbox(
            "캠프/창고로 보기 (알림 확인용)", ["선택 안 함"] + _login_accounts, key="admin_view_camp"
        )
        my_camp = admin_view_camp if admin_view_camp != "선택 안 함" else None
with col_auth2:
    if st.button("로그아웃", icon=":material/logout:"):
        del st.session_state["auth"]
        st.rerun()

if my_camp == "물류창고":
    try:
        pending_df = list_pending_warehouse_orders()
    except Exception:
        pending_df = pd.DataFrame()
    if not pending_df.empty:
        st.warning(
            f"**물류창고** 앞으로 승인 대기 중인 발주 요청이 **{len(pending_df)}건** 있어요. "
            "아래에서 바로 승인/거절할 수 있어요.",
            icon=":material/notifications_active:",
        )
        my_name_banner = st.text_input(
            "내 이름", value=st.session_state.get("transfer_my_name", ""), key="banner_my_name_input"
        )
        st.session_state["transfer_my_name"] = my_name_banner
        for _, r in pending_df.iterrows():
            render_pending_warehouse_order_row(r, my_name_banner, key_prefix="banner_")
elif my_camp:
    try:
        pending_out_df = list_pending_transfer_requests_for_camp(my_camp)
        outgoing_transit_df = list_outgoing_transit_requests_for_camp(my_camp)
        incoming_transit_df = list_incoming_transit_requests_for_camp(my_camp)
        incoming_wh_df = list_incoming_warehouse_orders_for_camp(my_camp)
        my_awaiting_df = list_my_requested_awaiting_approval(my_camp)
    except Exception:
        pending_out_df = pd.DataFrame()
        outgoing_transit_df = pd.DataFrame()
        incoming_transit_df = pd.DataFrame()
        incoming_wh_df = pd.DataFrame()
        my_awaiting_df = pd.DataFrame()

    actionable_n = len(pending_out_df) + len(incoming_transit_df) + len(incoming_wh_df)
    if actionable_n > 0 or not my_awaiting_df.empty or not outgoing_transit_df.empty:
        if actionable_n > 0:
            st.warning(
                f"**{my_camp}** 앞으로 처리할 요청이 **{actionable_n}건** 있어요. "
                "아래에서 바로 처리할 수 있어요.",
                icon=":material/notifications_active:",
            )
        my_name_banner = st.text_input(
            "내 이름", value=st.session_state.get("transfer_my_name", ""), key="banner_my_name_input"
        )
        st.session_state["transfer_my_name"] = my_name_banner

        if not pending_out_df.empty:
            st.markdown("**승인 대기중 (내가 보내는 쪽, 캠프 간 이관)**")
            for _, r in pending_out_df.iterrows():
                render_pending_request_row(r, data, my_name_banner, key_prefix="banner_")

        if not outgoing_transit_df.empty:
            st.markdown("**📤 발송해야 할 물건 (승인됨 · 받는 캠프가 입고완료 누르면 사라져요)**")
            for _, r in outgoing_transit_df.iterrows():
                st.caption(
                    f":material/local_shipping: {r['item_name']} · {int(r['qty'])}개 → **{r['to_camp']}** "
                    f"(승인자: {r['approved_by']})"
                )

        if not incoming_transit_df.empty:
            st.markdown("**입고 확인 필요 (캠프 간 이관)**")
            for _, r in incoming_transit_df.iterrows():
                render_in_transit_request_row(r, data, my_name_banner, key_prefix="banner_")

        if not incoming_wh_df.empty:
            st.markdown("**입고/검수 확인 필요 (물류창고 발주)**")
            for _, r in incoming_wh_df.iterrows():
                render_incoming_warehouse_order_row(r, data, my_name_banner, key_prefix="banner_")

        if not my_awaiting_df.empty:
            st.markdown("**내 요청 · 상대 캠프 승인 대기중** (정보용, 상대가 승인해야 진행돼요)")
            for _, r in my_awaiting_df.iterrows():
                st.caption(
                    f":material/schedule: {r['item_name']} · {r['from_camp']} → {r['to_camp']} · "
                    f"{int(r['qty'])}개"
                )

if auth["role"] == "admin":
    with st.expander("🔑 캠프/창고 비밀번호 관리 (관리자 전용)"):
        pw_camp = st.selectbox("캠프/창고 선택", _login_accounts, key="admin_pw_camp")
        new_pw = st.text_input("새 비밀번호", type="password", key="admin_pw_new")
        if st.button("비밀번호 설정", key="admin_pw_set_btn"):
            if new_pw.strip():
                set_camp_password(pw_camp, new_pw.strip())
                st.success(f"{pw_camp} 비밀번호를 설정했습니다.", icon=":material/check_circle:")
            else:
                st.error("비밀번호를 입력해주세요.", icon=":material/error:")

    with st.expander("💬 캠프별 구글챗 알림 웹훅 관리 (관리자 전용)"):
        st.caption(
            "캠프/물류창고마다 담당자 전용 구글챗 공간(스페이스)을 만들고 웹훅 URL을 등록하면, "
            "재고이관 요청/승인/거절/입고완료 시점마다 그 담당자에게 알림이 가요. "
            "비워두면 그 캠프는 알림을 받지 않아요."
        )
        current_webhooks = load_camp_chat_webhooks()
        wh_camp = st.selectbox("캠프/창고 선택", _login_accounts, key="admin_webhook_camp")
        wh_url = st.text_input(
            "웹훅 URL", value=current_webhooks.get(wh_camp, ""), key=f"admin_webhook_url_{wh_camp}"
        )
        wcol1, wcol2 = st.columns(2)
        if wcol1.button("저장", key="admin_webhook_save_btn"):
            if wh_url.strip():
                save_camp_chat_webhook(wh_camp, wh_url.strip())
                st.success(f"{wh_camp} 웹훅을 저장했습니다.", icon=":material/check_circle:")
                load_camp_chat_webhooks.clear()
                st.rerun()
            else:
                st.error("웹훅 URL을 입력해주세요.", icon=":material/error:")
        if wcol2.button("삭제", key="admin_webhook_delete_btn"):
            delete_camp_chat_webhook(wh_camp)
            st.success(f"{wh_camp} 웹훅을 삭제했습니다.", icon=":material/check_circle:")
            load_camp_chat_webhooks.clear()
            st.rerun()
        if current_webhooks:
            st.caption(f"현재 등록된 캠프: {', '.join(sorted(current_webhooks.keys()))}")

    with st.expander("🔐 캠프 화면(탭) 권한 관리 (관리자 전용)"):
        st.caption("물류창고를 제외한 일반 캠프로 로그인했을 때 보이는 탭을 선택하세요. 관리자는 항상 전체를 봐요.")
        current_tab_perms = load_camp_tab_permissions()
        new_tab_perms = []
        for key, label in TAB_DEFS:
            display_label = label.split(": ", 1)[-1]
            checked = st.checkbox(
                display_label, value=(key in current_tab_perms), key=f"tabperm_{key}"
            )
            if checked:
                new_tab_perms.append(key)
        if st.button("캠프 탭 권한 저장", key="save_tab_perms_btn"):
            if not new_tab_perms:
                st.error("최소 1개는 선택해야 해요.", icon=":material/error:")
            else:
                save_camp_tab_permissions(new_tab_perms)
                st.success("캠프 탭 권한을 저장했습니다.", icon=":material/check_circle:")
                load_camp_tab_permissions.clear()
                st.rerun()

    with st.expander("🏭 물류창고 화면(탭) 권한 관리 (관리자 전용)"):
        st.caption("물류창고 계정으로 로그인했을 때 보이는 탭을 따로 선택하세요 (일반 캠프와 별개).")
        current_wh_perms = load_warehouse_tab_permissions()
        new_wh_perms = []
        for key, label in TAB_DEFS:
            display_label = label.split(": ", 1)[-1]
            checked = st.checkbox(
                display_label, value=(key in current_wh_perms), key=f"whtabperm_{key}"
            )
            if checked:
                new_wh_perms.append(key)
        if st.button("물류창고 탭 권한 저장", key="save_wh_tab_perms_btn"):
            if not new_wh_perms:
                st.error("최소 1개는 선택해야 해요.", icon=":material/error:")
            else:
                save_warehouse_tab_permissions(new_wh_perms)
                st.success("물류창고 탭 권한을 저장했습니다.", icon=":material/check_circle:")
                load_warehouse_tab_permissions.clear()
                st.rerun()

st.divider()


# ---------------- 파생 데이터 계산 ----------------

@st.cache_data(show_spinner=False)
def compute_camp_totals(data):
    totals = {c: {"qty": 0, "amt": 0} for c in data["campsOrder"]}
    for it in data["items"]:
        for camp, (q, a) in it["x"].items():
            if camp in totals:
                totals[camp]["qty"] += q
                totals[camp]["amt"] += a
    rows = []
    for c in data["campsOrder"]:
        rows.append({"캠프": c, "팀": data["campToTeam"].get(c, "-"), "재고 수량": totals[c]["qty"], "재고 금액": totals[c]["amt"]})
    return pd.DataFrame(rows)


camp_df = compute_camp_totals(data)
team_df = camp_df.groupby("팀", as_index=False)[["재고 수량", "재고 금액"]].sum().sort_values("재고 금액", ascending=False)
grand_qty = sum(it["q"] for it in data["items"])
grand_amt = sum(it["a"] for it in data["items"])
zero_camps = int((camp_df["재고 수량"] == 0).sum())

_cur_period_label = current_inventory_period()[2]
if st.session_state.get("_camp_value_snap_period") != _cur_period_label:
    try:
        save_inventory_value_snapshot("camp", build_camp_value_snapshot_rows(data))
        st.session_state["_camp_value_snap_period"] = _cur_period_label
    except Exception:
        pass  # 스냅샷 저장 실패해도 화면 표시는 계속 진행

ctx = TabContext(
    data=data,
    usage=usage,
    auth=auth,
    my_camp=my_camp,
    my_login_camp=my_login_camp,
    camp_df=camp_df,
    team_df=team_df,
    grand_qty=grand_qty,
    grand_amt=grand_amt,
    zero_camps=zero_camps,
)


# ---------------- 탭(화면 전환) ----------------
# 관리자는 전체 메뉴를 보고, 캠프/물류창고 로그인은 관리자가 허용한 메뉴만 본다 (기본값은
# 재고이관 2개). st.tabs() 대신 st.segmented_control()을 써서, 지금 선택된 화면이 뭔지
# 코드에서 직접 알 수 있게 하고 그 화면 코드만 실행한다 — 관리자를 포함해 누구든 안 보이는
# 화면의 코드(DB 조회 등)는 아예 돌지 않아서, 메뉴가 늘어나도 반응속도가 나빠지지 않는다.

if auth["role"] == "admin":
    _visible_tab_keys = [k for k, _ in TAB_DEFS]
elif auth["camp"] == "물류창고":
    _visible_tab_keys = load_warehouse_tab_permissions()
else:
    _visible_tab_keys = load_camp_tab_permissions()

_visible_tab_defs = [d for d in TAB_DEFS if d[0] in _visible_tab_keys]
if not _visible_tab_defs:
    _visible_tab_defs = TAB_DEFS  # 안전장치: 권한이 비어있으면 전체를 보여준다

_label_to_key = {label: key for key, label in _visible_tab_defs}
_key_to_label = {key: label for key, label in _visible_tab_defs}
_valid_labels = [label for _, label in _visible_tab_defs]

# 권한이 바뀌어 지금 선택된 메뉴가 더 이상 보이는 목록에 없으면(예: 관리자->캠프 재로그인),
# 위젯 상태를 지워서 첫 번째 메뉴로 안전하게 되돌아가게 한다.
if st.session_state.get("nav_control") not in _valid_labels:
    st.session_state.pop("nav_control", None)

_picked_label = st.segmented_control(
    "메뉴",
    _valid_labels,
    default=_valid_labels[0],
    key="nav_control",
    label_visibility="collapsed",
)
active_key = _label_to_key[_picked_label] if _picked_label else _label_to_key[_valid_labels[0]]
st.divider()

_TAB_RENDERERS = {
    "overview": tabs.overview.render,
    "camps": tabs.camps.render,
    "items": tabs.items.render,
    "rebalance": tabs.rebalance.render,
    "usage_amount": tabs.usage_amount.render,
    "category": tabs.category.render,
    "forecast": tabs.forecast.render,
    "total": tabs.total.render,
    "purchase": tabs.purchase.render,
    "warehouse": tabs.warehouse.render,
}
_TAB_RENDERERS[active_key](ctx)

st.divider()
st.caption("업로드한 데이터는 이 앱에 접속하는 모든 사람에게 공유됩니다. 재고 엑셀/사용량 JSON을 다시 올리면 바로 반영돼요.")