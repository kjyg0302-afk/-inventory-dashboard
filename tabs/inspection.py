"""입고검사 탭 — 휴대폰으로 QR(시리얼)을 스캔하고 항목별 OK/NG + 사진을 남긴다.
검사는 물류창고 등 탭 권한이 있는 계정 누구나, 검사 건 만들기·취소·항목 관리는 관리자만."""

import pandas as pd
import streamlit as st

from formatting import KST
from inspection import (
    DEVICE_TYPES, RESULT_NG, RESULT_OK, STATUS_DONE, STATUS_OPEN,
    add_serials, clean_serial, create_batch, delete_batch, delete_inspection, expected_serials, list_batches,
    list_inspections, load_checklists, load_photos, parse_serials, recent_inspectors, results_xlsx, save_checklist,
    save_inspection, serial_history, set_batch_status,
)
from procurement import load_purchase_lines
from qr_scanner import qr_scanner

N_KEY = "insp_n"  # 저장할 때마다 1씩 올려서 입력칸을 새로 비운다


def _k(name, bid):
    return f"insp_{name}_{bid}_{st.session_state.get(N_KEY, 0)}"


def _fmt_time(ts):
    return pd.Timestamp(ts).tz_convert(KST).strftime("%m-%d %H:%M")


def _total(b):
    """검사할 대수: 입고 예정 대수, 없으면 시리얼 목록 개수."""
    return int(b["expected_qty"]) if pd.notna(b["expected_qty"]) and b["expected_qty"] else int(b["listed"])


def _ng_items(items):
    return ", ".join(it["item"] for it in (items or []) if it.get("result") == RESULT_NG)


# ---------------- 관리자: 검사 건 만들기 ----------------

def _render_create(account):
    st.caption("입고 리스트(시리얼 목록)를 올리면 목록에 없는 번호는 검사할 때 경고해요. 목록 없이 만들어도 돼요.")
    c1, c2 = st.columns([2, 1])
    name = c1.text_input("검사 건 이름", placeholder="예: 2026-11 MAX 킥보드 1차 300대", key="insp_new_name")
    dtype = c2.selectbox("종류", DEVICE_TYPES, key="insp_new_type")
    c3, c4, c5 = st.columns(3)
    model = c3.text_input("모델 (선택)", key="insp_new_model")
    po_no = c4.text_input("PO번호 (선택)", key="insp_new_po")
    qty = c5.number_input("입고 예정 대수 (선택)", min_value=0, step=1, key="insp_new_qty")

    lines = load_purchase_lines()
    open_lines = lines[lines["open_qty"] > 0]
    line_opts = {None: "연결 안 함"} | {
        int(r.id): f"{r.po_no or '-'} · {r.item_name} · 미입고 {r.open_qty:,.0f}개 ({r.line_no})"
        for r in open_lines.itertuples()
    }
    line_id = st.selectbox(
        "구매 줄 연결 (선택)", list(line_opts), format_func=line_opts.get, key="insp_new_line",
        help="연결하면 OK 한 대마다 그 구매 줄에 입고 1개가 기록돼요. 구매 E2E에 없는 구매면 연결 안 함으로 두세요.",
    )
    f1, f2 = st.columns(2)
    file = f1.file_uploader("시리얼 목록 엑셀/CSV", type=["xlsx", "xls", "csv"], key="insp_new_file",
                            help="'시리얼'·'serial'·'S/N' 열을 찾고, 없으면 첫 번째 열을 써요.")
    pasted = f2.text_area("또는 붙여넣기 (줄바꿈·쉼표 구분)", key="insp_new_paste", height=120)
    try:
        serials = parse_serials(pasted, file)
    except Exception as e:
        st.error(f"시리얼 목록을 읽지 못했어요: {e}")
        return
    if serials:
        st.caption(f"시리얼 {len(serials):,}개: {', '.join(serials[:8])}{' …' if len(serials) > 8 else ''}")

    if st.button("검사 건 만들기", type="primary", key="insp_new_btn"):
        if not name.strip():
            st.error("검사 건 이름을 적어 주세요.")
            return
        expected = qty or (len(serials) or None)
        if line_id and not qty:
            expected = expected or int(open_lines.loc[open_lines["id"] == line_id, "open_qty"].iloc[0])
        create_batch(name.strip(), dtype, model.strip(), po_no.strip(), line_id, expected, serials, account)
        for k in ["insp_new_name", "insp_new_model", "insp_new_po", "insp_new_paste"]:
            st.session_state.pop(k, None)
        st.toast(f"검사 건을 만들었어요: {name.strip()}", icon=":material/check:")
        st.rerun()


def _render_checklist_admin():
    lists = load_checklists()
    dtype = st.selectbox("종류", DEVICE_TYPES, key="insp_cl_type")
    raw = st.text_area("검사 항목 (한 줄에 하나)", "\n".join(lists[dtype]), key=f"insp_cl_{dtype}", height=140)
    if st.button("항목 저장", key="insp_cl_save"):
        items = [x.strip() for x in raw.splitlines() if x.strip()]
        if not items:
            st.error("항목이 하나 이상 있어야 해요.")
        else:
            save_checklist(dtype, items)
            st.toast("검사 항목을 저장했어요.", icon=":material/check:")
    st.caption("바꾼 항목은 다음 검사부터 적용돼요. 이미 저장된 검사는 그때 항목 그대로 남아요.")


# ---------------- 검사하기 ----------------

def _on_scan(bid):
    value = clean_serial(st.session_state[f"insp_scanner_{bid}"].scanned)
    if value:
        st.session_state[_k("serial", bid)] = value


def _all_ok(bid, checklist):
    for i in range(len(checklist)):
        st.session_state[_k(f"item{i}", bid)] = RESULT_OK


def _render_inspect(batch, checklist, account):
    bid = int(batch["id"])
    names = recent_inspectors()
    inspector = st.selectbox(
        "검사자 이름", names, index=None, accept_new_options=True, key="insp_inspector",
        placeholder="이름을 고르거나 새로 입력",
    )

    qr_scanner(f"insp_scanner_{bid}", lambda: _on_scan(bid))
    serial = clean_serial(st.text_input(
        "시리얼 번호", key=_k("serial", bid),
        placeholder="QR 스캔 · 블루투스 스캐너 · 직접 입력",
    ))

    listed = expected_serials(bid)
    off_list = False
    if serial:
        hist = serial_history(serial)
        passed = hist[hist["result"] == RESULT_OK]
        if not passed.empty:
            p = passed.iloc[0]
            st.error(f"**{serial}**은(는) 이미 통과했어요 — {p['batch_name']}, {_fmt_time(p['inspected_at'])}, {p['inspector']}")
            return
        if not hist.empty:
            h = hist.iloc[0]
            st.warning(f"재검사예요. 지난 검사 {_fmt_time(h['inspected_at'])} NG ({_ng_items(h['items']) or '-'}) · {h['note'] or ''}")
        if listed and serial not in listed:
            off_list = True
            st.warning(f"**{serial}**은(는) 이 검사 건의 시리얼 목록에 없어요.")

    st.button("전부 OK", key=_k("allok", bid), on_click=_all_ok, args=(bid, checklist), icon=":material/done_all:")
    results = []
    for i, item in enumerate(checklist):
        results.append(st.segmented_control(item, [RESULT_OK, RESULT_NG], key=_k(f"item{i}", bid)))
    any_ng = RESULT_NG in results
    note = st.text_input("메모" + (" (NG 내용 필수)" if any_ng else ""), key=_k("note", bid))
    photos = st.file_uploader(
        "사진 (필요할 때 · 휴대폰에서는 카메라로 바로 찍을 수 있어요)", type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True, key=_k("photos", bid),
    )
    confirm_off = st.checkbox("목록에 없지만 저장", key=_k("offlist", bid)) if off_list else True

    if st.button("저장", type="primary", key=_k("save", bid), width="stretch", icon=":material/save:"):
        problems = []
        if not inspector or not str(inspector).strip():
            problems.append("검사자 이름을 골라 주세요.")
        if not serial:
            problems.append("시리얼 번호를 스캔하거나 입력해 주세요.")
        if None in results:
            problems.append("OK/NG를 고르지 않은 항목: " + ", ".join(c for c, r in zip(checklist, results) if r is None))
        if any_ng and not note.strip():
            problems.append("NG 항목이 있으면 메모를 적어 주세요.")
        if not confirm_off:
            problems.append("목록에 없는 시리얼이에요. 맞으면 '목록에 없지만 저장'에 체크해 주세요.")
        if problems:
            for p in problems:
                st.error(p)
            return
        items = [{"item": c, "result": r} for c, r in zip(checklist, results)]
        try:
            _, result = save_inspection(batch, serial, items, note.strip(), str(inspector).strip(), account,
                                        [f.getvalue() for f in photos or []])
        except ValueError as e:
            st.error(str(e))
            return
        st.session_state[N_KEY] = st.session_state.get(N_KEY, 0) + 1
        msg = f"{serial} 저장 — {result}" + (" (입고 처리)" if result == RESULT_OK else "")
        st.toast(msg, icon=":material/check_circle:" if result == RESULT_OK else ":material/error:")
        st.rerun()


# ---------------- 기록 ----------------

def _render_records(batch, checklist, is_admin):
    bid = int(batch["id"])
    df = list_inspections(bid)
    if df.empty:
        st.caption("아직 검사 기록이 없어요.")
    else:
        flt = st.segmented_control("보기", ["전체", RESULT_OK, RESULT_NG], default="전체", key=f"insp_flt_{bid}",
                                   label_visibility="collapsed")
        view = df if flt in (None, "전체") else df[df["result"] == flt]
        table = pd.DataFrame({
            "검사 일시": view["inspected_at"].map(_fmt_time), "시리얼": view["serial"], "결과": view["result"],
            "NG 항목": view["items"].map(_ng_items), "메모": view["note"].fillna(""),
            "검사자": view["inspector"], "사진": view["photos"],
        })
        st.dataframe(table, hide_index=True, width="stretch",
                     column_config={"사진": st.column_config.NumberColumn(format="%d장")})
        st.download_button(
            "검사 결과 엑셀", results_xlsx(batch, df, checklist), file_name=f"입고검사_{batch['name']}.xlsx",
            icon=":material/download:", key=f"insp_dl_{bid}",
        )

        with_detail = df[(df["result"] == RESULT_NG) | (df["photos"] > 0)]
        if not with_detail.empty:
            with st.expander(f"NG · 사진 보기 ({len(with_detail)}건)", icon=":material/photo_library:"):
                opts = {int(r.id): f"{r.serial} · {r.result} · {_fmt_time(r.inspected_at)} · {r.inspector}"
                        for r in with_detail.itertuples()}
                pick = st.selectbox("검사", list(opts), format_func=opts.get, key=f"insp_pick_{bid}")
                row = with_detail[with_detail["id"] == pick].iloc[0]
                st.markdown("  \n".join(
                    f"{':red[NG]' if it['result'] == RESULT_NG else ':green[OK]'} {it['item']}" for it in row["items"]
                ))
                if row["note"]:
                    st.caption(f"메모: {row['note']}")
                imgs = load_photos(pick)
                if imgs:
                    st.image(imgs, width=320)

    listed = expected_serials(bid)
    if listed:
        done = set(df["serial"]) if not df.empty else set()
        todo = [s for s in listed if s not in done]
        with st.expander(f"아직 검사 안 한 시리얼 ({len(todo):,}개)", icon=":material/pending:"):
            st.write(", ".join(todo) if todo else "목록의 시리얼을 모두 검사했어요.")

    if is_admin:
        with st.expander("검사 건 관리", icon=":material/settings:"):
            if not df.empty:
                opts = {int(r.id): f"{r.serial} · {r.result} · {_fmt_time(r.inspected_at)} · {r.inspector}"
                        for r in df.itertuples()}
                c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
                kill = c1.selectbox("검사 취소 (잘못 저장한 경우)", list(opts), format_func=opts.get,
                                    key=f"insp_kill_{bid}")
                if c2.button("취소", key=f"insp_kill_btn_{bid}"):
                    delete_inspection(kill)
                    st.toast("검사를 취소했어요. 입고 기록과 사진도 같이 지웠어요.", icon=":material/undo:")
                    st.rerun()
            more = st.text_area("시리얼 추가 (줄바꿈·쉼표 구분)", key=f"insp_more_{bid}", height=80)
            if st.button("시리얼 추가", key=f"insp_more_btn_{bid}"):
                add = parse_serials(more)
                if add:
                    add_serials(bid, add)
                    st.toast(f"시리얼 {len(add)}개를 추가했어요.", icon=":material/check:")
                    st.rerun()
            b1, b2 = st.columns(2)
            if batch["status"] == STATUS_OPEN:
                if b1.button("검사 완료로 닫기", key=f"insp_close_{bid}"):
                    set_batch_status(bid, STATUS_DONE)
                    st.rerun()
            elif b1.button("다시 열기", key=f"insp_reopen_{bid}"):
                set_batch_status(bid, STATUS_OPEN)
                st.rerun()
            if df.empty and b2.button("검사 건 삭제", key=f"insp_del_{bid}"):
                if delete_batch(bid):
                    st.rerun()


# ---------------- 탭 ----------------

def render(ctx):
    is_admin = ctx.auth["role"] == "admin"
    account = "관리자" if is_admin else ctx.auth.get("camp")
    batches = list_batches()

    if is_admin:
        with st.expander("검사 건 만들기 (입고 리스트 올리기)", icon=":material/add_box:", expanded=batches.empty):
            _render_create(account)
        with st.expander("검사 항목 관리", icon=":material/checklist:"):
            _render_checklist_admin()

    if batches.empty:
        st.info("검사 건이 없어요. 관리자가 입고 리스트로 검사 건을 만들면 여기서 검사할 수 있어요.")
        return

    show_done = st.toggle("완료된 검사 건도 보기", key="insp_show_done")
    pool = batches if show_done else batches[batches["status"] == STATUS_OPEN]
    if pool.empty:
        st.info("진행 중인 검사 건이 없어요.")
        return

    def _label(bid):
        b = pool[pool["id"] == bid].iloc[0]
        total = _total(b)
        done = " · 완료" if b["status"] == STATUS_DONE else ""
        return f"{b['name']} ({b['device_type']}) · 검사 {b['inspected']:,}" + (f"/{total:,}" if total else "") + done

    bid = st.selectbox("검사 건", pool["id"].tolist(), format_func=_label, key="insp_batch")
    batch = pool[pool["id"] == bid].iloc[0].to_dict()
    checklist = load_checklists()[batch["device_type"]]

    total = _total(batch)
    m1, m2, m3 = st.columns(3)
    m1.metric("검사", f"{batch['inspected']:,}" + (f" / {total:,}대" if total else "대"), border=True)
    m2.metric("OK (입고)", f"{batch['ok']:,}대", border=True)
    m3.metric("NG", f"{batch['ng']:,}대", border=True)
    if total:
        st.progress(min(batch["inspected"] / total, 1.0))
    extra = []
    if batch.get("po_no"):
        extra.append(f"PO {batch['po_no']}")
    if batch.get("model"):
        extra.append(batch["model"])
    if pd.notna(batch.get("line_id")):
        extra.append(f"구매 줄 {batch['line_no']} ({batch['line_item_name']})에 OK마다 입고 1개 기록")
    if extra:
        st.caption(" · ".join(extra))

    if batch["status"] == STATUS_OPEN:
        st.subheader("검사하기", divider="gray")
        _render_inspect(batch, checklist, account)
    st.subheader("검사 기록", divider="gray")
    _render_records(batch, checklist, is_admin)

    with st.expander("시리얼 조회", icon=":material/search:"):
        q = clean_serial(st.text_input("시리얼 번호", key="insp_lookup"))
        if q:
            h = serial_history(q)
            if h.empty:
                st.caption("검사 기록이 없어요.")
            else:
                st.dataframe(pd.DataFrame({
                    "검사 일시": h["inspected_at"].map(_fmt_time), "검사 건": h["batch_name"], "결과": h["result"],
                    "NG 항목": h["items"].map(_ng_items), "메모": h["note"].fillna(""), "검사자": h["inspector"],
                    "사진": h["photos"],
                }), hide_index=True, width="stretch")
