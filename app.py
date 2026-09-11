"""
캠프 재고 현황판 (Streamlit)
- 태블로에서 받은 재고 엑셀(피벗 형식)과, scm_data.db에서 export_weekly_usage.py로
  뽑은 주단위 사용량 JSON을 업로드하면 대시보드가 채워집니다.
- 업로드한 데이터는 data/ 폴더에 저장되어, 다시 접속하는 모든 사람에게 그대로 보입니다.
  (Streamlit Cloud에서 앱을 재배포하면 초기화될 수 있어요 — 정식 DB 연동 전까지의 임시 저장 방식입니다)
"""

import json
import os
from datetime import datetime

import pandas as pd
import streamlit as st

st.set_page_config(page_title="캠프 재고 현황판", layout="wide")

st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header[data-testid="stHeader"] {background: transparent;}

    html, body, [class*="css"]  {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic",
                     "Apple SD Gothic Neo", sans-serif !important;
    }

    .block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1300px;}

    /* KPI 카드 (st.metric) */
    div[data-testid="stMetric"] {
        background: #171D27;
        border: 1px solid #262F3D;
        border-radius: 10px;
        padding: 14px 16px;
    }
    div[data-testid="stMetricLabel"] { color: #8A96A8; font-size: 12.5px; }
    div[data-testid="stMetricValue"] { color: #E9EDF3; font-variant-numeric: tabular-nums; }
    div[data-testid="stMetricDelta"] { font-size: 11.5px; }

    /* 탭 */
    button[data-testid="stTab"] { font-weight: 600; font-size: 14px; }
    div[data-testid="stTabs"] button[aria-selected="true"] {
        color: #5B8DEF !important;
        border-bottom-color: #5B8DEF !important;
    }

    /* 버튼 */
    div.stButton > button, div.stDownloadButton > button {
        border-radius: 7px;
        border: 1px solid #262F3D;
        font-weight: 600;
    }

    /* 파일 업로더 */
    section[data-testid="stFileUploaderDropzone"] {
        background: #171D27;
        border: 1px dashed #3A4658;
        border-radius: 10px;
    }
    div[data-testid="stFileUploader"] label { font-weight: 600; font-size: 13px; }

    /* 표 */
    div[data-testid="stDataFrame"], div[data-testid="stTable"] {
        border: 1px solid #262F3D;
        border-radius: 10px;
        overflow: hidden;
    }

    /* expander (품목 카드) */
    details[data-testid="stExpander"] {
        background: #171D27;
        border: 1px solid #262F3D !important;
        border-radius: 8px;
        margin-bottom: 6px;
    }
    summary { font-weight: 600; font-size: 13.5px; }

    /* 알림 배너 */
    div[data-testid="stAlertContainer"] {
        border-radius: 8px;
    }

    h1, h2, h3 { letter-spacing: -0.01em; }
    </style>
    """,
    unsafe_allow_html=True,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
INVENTORY_PATH = os.path.join(DATA_DIR, "inventory.json")
USAGE_PATH = os.path.join(DATA_DIR, "usage.json")


# ---------------- 데이터 파싱 ----------------

def parse_inventory_excel(file) -> dict:
    """태블로 재고 내역 엑셀(피벗 구조)을 읽어 내부 JSON 구조로 변환."""
    df = pd.read_excel(file, header=None)
    team_row = df.iloc[0].tolist()
    camp_row = df.iloc[1].tolist()
    width = max(len(team_row), len(camp_row))

    camps_order, camp_to_team, teams = [], {}, []
    cur_team = None
    for i in range(4, width, 2):
        t = team_row[i] if i < len(team_row) else None
        c = camp_row[i] if i < len(camp_row) else None
        if isinstance(t, str) and t.strip():
            cur_team = t
            if t not in teams:
                teams.append(t)
        if isinstance(c, str) and c.strip():
            camps_order.append(c)
            camp_to_team[c] = cur_team

    if not camps_order:
        raise ValueError("캠프/팀 헤더를 찾을 수 없습니다. 원본 태블로 내보내기 형식인지 확인해주세요.")

    items = []
    for r in range(3, len(df)):
        row = df.iloc[r]
        name = row[0]
        if pd.isna(name) or str(name).strip() in ("", "총합계"):
            continue
        code = row[1]
        code = "" if pd.isna(code) else str(code).strip()
        tot_qty = 0 if pd.isna(row[2]) else int(row[2])
        tot_amt = 0 if pd.isna(row[3]) else int(row[3])
        camps = {}
        ci = 4
        for camp in camps_order:
            q = row[ci] if ci < len(row) else None
            a = row[ci + 1] if ci + 1 < len(row) else None
            q = 0 if pd.isna(q) else int(q)
            a = 0 if pd.isna(a) else int(a)
            if q != 0 or a != 0:
                camps[camp] = [q, a]
            ci += 2
        items.append({"n": str(name), "c": code, "q": tot_qty, "a": tot_amt, "x": camps})

    if not items:
        raise ValueError("품목 데이터를 찾을 수 없습니다.")

    return {
        "updatedAt": datetime.now().isoformat(),
        "teams": teams,
        "campToTeam": camp_to_team,
        "campsOrder": camps_order,
        "items": items,
    }


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def fmt_int(n):
    return f"{round(n or 0):,}"


def fmt_won(n):
    return f"₩{round(n or 0):,}"


# ---------------- 세션 상태 로드 ----------------

if "inventory" not in st.session_state:
    st.session_state.inventory = load_json(INVENTORY_PATH)
if "usage" not in st.session_state:
    st.session_state.usage = load_json(USAGE_PATH)

data = st.session_state.inventory
usage = st.session_state.usage


# ---------------- 헤더 ----------------

col_title, col_upload1, col_upload2 = st.columns([3, 1, 1])
with col_title:
    st.markdown(
        """
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:2px;">
            <div style="width:38px;height:38px;border-radius:8px;background:rgba(91,141,239,0.14);
                        border:1px solid rgba(91,141,239,0.3);display:flex;align-items:center;
                        justify-content:center;font-size:18px;">📦</div>
            <div style="font-size:20px;font-weight:700;letter-spacing:-0.01em;">캠프 재고 현황판</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    updated = data["updatedAt"][:16].replace("T", " ") if data else "-"
    st.caption(f"전 캠프 재고 데이터 · 마지막 업데이트 {updated}")

with col_upload1:
    inv_file = st.file_uploader("재고 엑셀로 갱신", type=["xlsx", "xls"], key="inv_upload")
    if inv_file is not None:
        try:
            parsed = parse_inventory_excel(inv_file)
            save_json(INVENTORY_PATH, parsed)
            st.session_state.inventory = parsed
            data = parsed
            st.success(f"업데이트 완료 · 품목 {len(parsed['items']):,}개 · 캠프 {len(parsed['campsOrder'])}곳")
        except Exception as e:
            st.error(f"파일을 읽는 중 문제가 발생했습니다: {e}")

with col_upload2:
    usage_file = st.file_uploader("사용량 데이터 갱신 (JSON)", type=["json"], key="usage_upload")
    if usage_file is not None:
        try:
            parsed_usage = json.load(usage_file)
            if "items" not in parsed_usage:
                raise ValueError("사용량 JSON 형식이 올바르지 않습니다.")
            save_json(USAGE_PATH, parsed_usage)
            st.session_state.usage = parsed_usage
            usage = parsed_usage
            st.success(f"사용량 갱신 완료 · 부품 {len(parsed_usage['items']):,}종")
        except Exception as e:
            st.error(f"사용량 파일을 읽는 중 문제가 발생했습니다: {e}")

if usage and usage.get("weekRange"):
    wr = usage["weekRange"]
    st.info(f"📈 사용량 데이터 기준: {wr['from'][0]}-{wr['from'][1]}주 ~ {wr['to'][0]}-{wr['to'][1]}주 ({wr['count']}주)")

st.divider()

if not data:
    st.warning("아직 업로드된 재고 데이터가 없어요. 위에서 재고 엑셀을 업로드해주세요.")
    st.stop()


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


def get_usage_for_code(code):
    if not usage or not code:
        return None
    return usage["items"].get(str(code).strip())


# ---------------- 탭 ----------------

tab_overview, tab_camps, tab_items, tab_rebalance = st.tabs(
    ["개요", "캠프별 현황", "품목 검색", "재분배 도우미"]
)

with tab_overview:
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("총 품목 수", f"{len(data['items']):,}종")
    k2.metric("총 재고 수량", f"{fmt_int(grand_qty)}개")
    k3.metric("총 재고 금액", fmt_won(grand_amt))
    k4.metric("운영 캠프 수", f"{len(data['campsOrder'])}곳", delta=(f"재고 0인 캠프 {zero_camps}곳" if zero_camps else None), delta_color="inverse")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("RS팀별 재고 금액")
        st.bar_chart(team_df.set_index("팀")["재고 금액"])
    with c2:
        st.subheader("재고 금액 상위 캠프 TOP 8")
        top8 = camp_df.sort_values("재고 금액", ascending=False).head(8)
        st.bar_chart(top8.set_index("캠프")["재고 금액"])

with tab_camps:
    sort_col = st.selectbox("정렬 기준", ["재고 금액", "재고 수량", "캠프", "팀"], index=0)
    ascending = st.checkbox("오름차순", value=False)
    st.dataframe(
        camp_df.sort_values(sort_col, ascending=ascending),
        use_container_width=True,
        hide_index=True,
        column_config={
            "재고 수량": st.column_config.NumberColumn(format="%d개"),
            "재고 금액": st.column_config.NumberColumn(format="₩%d"),
        },
    )

with tab_items:
    search = st.text_input("부품명 또는 부품 번호로 검색", "")
    items = data["items"]
    if search.strip():
        q = search.strip().lower()
        items = [it for it in items if q in it["n"].lower() or q in str(it["c"]).lower()]
    st.caption(f"{len(items):,}개 품목 중 최대 50개 표시")
    for it in items[:50]:
        camp_usage = get_usage_for_code(it["c"])
        with st.expander(f"{it['n']}  ·  {it['c']}  ·  총 {fmt_int(it['q'])}개  ·  {fmt_won(it['a'])}"):
            if not it["x"]:
                st.caption("보유 캠프 없음")
            else:
                rows = []
                for camp, (q, a) in sorted(it["x"].items(), key=lambda kv: -kv[1][0]):
                    avg = camp_usage.get(camp, {}).get("avg") if camp_usage else None
                    rows.append({"캠프": camp, "재고 수량": q, "주 평균 사용량": avg if avg is not None else "-"})
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

with tab_rebalance:
    st.caption("품목을 선택하면 캠프별 재고 편차를 확인할 수 있어요")
    query = st.text_input("재분배를 검토할 품목명 또는 번호 입력", "", key="rebalance_query")
    selected = None
    if query.strip():
        q = query.strip().lower()
        candidates = [it for it in data["items"] if q in it["n"].lower() or q in str(it["c"]).lower()][:8]
        if candidates:
            options = {f"{it['n']} ({it['c']})": it for it in candidates}
            picked_label = st.radio("검색 결과", list(options.keys()), index=0)
            selected = options[picked_label]
        else:
            st.info("일치하는 품목이 없습니다.")

    if selected:
        item_usage = get_usage_for_code(selected["c"])
        rows = []
        for camp in data["campsOrder"]:
            pair = selected["x"].get(camp)
            qty = pair[0] if pair else 0
            u = item_usage.get(camp) if item_usage else None
            avg = u["avg"] if u else None
            weeks_of_stock = round(qty / avg, 1) if avg and avg > 0 else None
            rows.append(
                {
                    "팀": data["campToTeam"].get(camp, "-"),
                    "캠프": camp,
                    "재고 수량": qty,
                    "주 평균 사용량": avg if avg is not None else "-",
                    "소진 예상(주)": weeks_of_stock if weeks_of_stock is not None else "-",
                }
            )
        df_rows = pd.DataFrame(rows).sort_values("재고 수량", ascending=False)

        # 재분배 제안
        shortages = df_rows[(df_rows["재고 수량"] == 0) & (df_rows["주 평균 사용량"] != "-") & (df_rows["주 평균 사용량"] > 0)]
        if not shortages.empty:
            donors = df_rows[(df_rows["재고 수량"] > 1)]
            donors = donors[(donors["소진 예상(주)"] == "-") | (donors["소진 예상(주)"] > 4)]
            donors = donors.sort_values("재고 수량", ascending=False)
            if not donors.empty:
                top = donors.iloc[0]
                move_qty = max(1, int(top["재고 수량"] // 2))
                to_camps = ", ".join(shortages["캠프"].head(3).tolist())
                st.warning(
                    f"**{top['캠프']}**에 {fmt_int(top['재고 수량'])}개 보유 중인 반면, **{to_camps}**에는 재고가 없습니다. "
                    f"약 {move_qty}개 이동을 검토해보세요. (최근 사용량 데이터를 반영한 제안)"
                )
        else:
            with_stock = df_rows[df_rows["재고 수량"] > 0]
            empty = df_rows[df_rows["재고 수량"] == 0]
            if not with_stock.empty and not empty.empty and with_stock.iloc[0]["재고 수량"] >= 2:
                top = with_stock.iloc[0]
                move_qty = max(1, int(top["재고 수량"] // 2))
                to_camps = ", ".join(empty["캠프"].head(3).tolist())
                st.warning(
                    f"**{top['캠프']}**에 {fmt_int(top['재고 수량'])}개 보유 중인 반면, **{to_camps}**에는 재고가 없습니다. "
                    f"약 {move_qty}개 이동을 검토해보세요. (사용량 데이터가 없어 재고량만 기준으로 한 참고용 제안)"
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
                ).set_index("주차")
                st.subheader("전체 캠프 합산 · 최근 12주 사용량 추이")
                st.bar_chart(trend_df)
        else:
            st.caption("이 품목의 사용량 데이터가 아직 없습니다. (재고 수량만으로 비교합니다)")

        st.dataframe(
            df_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "재고 수량": st.column_config.NumberColumn(format="%d개"),
            },
        )

st.divider()
st.caption("업로드한 데이터는 이 앱에 접속하는 모든 사람에게 공유됩니다. 재고 엑셀/사용량 JSON을 다시 올리면 바로 반영돼요.")