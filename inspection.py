"""입고검사 — 킥보드·자전거 본체를 QR(시리얼 번호)로 한 대씩 검사한다.

검사 건(inspection_batches) = 입고 리스트 한 묶음. 시리얼 목록을 같이 올리면 목록에 없는 번호를 걸러낸다.
항목이 하나라도 NG면 그 기기는 NG(기록만), 전부 OK면 입고로 본다 — 검사 건이 구매 줄과 연결돼 있으면
OK 한 대마다 purchase_receipts에 입고 1개(source='app')를 남긴다.
같은 시리얼을 다시 검사하면 행이 하나 더 쌓이고 최신 행이 그 기기의 결과다 (NG 후 재검사)."""

import io
import json
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pandas as pd
from PIL import Image, ImageOps
from sqlalchemy import text

from app_data import load_data, save_data
from db import get_db_connection
from formatting import KST
from procurement import load_purchase_lines

DEVICE_TYPES = ["킥보드", "자전거"]
DEFAULT_CHECKLIST = ["외관 검사", "IoT 작동 검사", "타이어 공기압 검사"]
RESULT_OK, RESULT_NG = "OK", "NG"
STATUS_OPEN, STATUS_DONE = "진행", "완료"
PHOTO_MAX_SIDE = 1280
# 갤럭시·아이폰 카메라는 설정에 따라 HEIC로 저장한다 → 업로드에서 받아서 JPEG로 바꾼다.
PHOTO_TYPES = ["jpg", "jpeg", "png", "webp", "heic", "heif"]

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass


def load_checklists():
    saved = load_data("inspection_checklists") or {}
    return {t: saved.get(t) or DEFAULT_CHECKLIST for t in DEVICE_TYPES}


def save_checklist(device_type, items):
    lists = load_checklists()
    lists[device_type] = items
    save_data("inspection_checklists", lists)


# 기기 QR은 주소 형태다: https://open.gbike.io/?b=08201632498 → 기기 번호는 b 값.
QR_SERIAL_PARAMS = ("b", "sn", "serial", "id")


def clean_serial(raw):
    """스캔·입력값 정리: 기기 QR 주소면 번호(b=…)만 꺼내고, 앞뒤 공백과 엑셀 숫자의 '.0'을 뗀다."""
    s = str(raw or "").strip()
    if re.match(r"https?://", s, re.I):
        url = urlparse(s)
        query = parse_qs(url.query)
        hit = next((query[k][0] for k in QR_SERIAL_PARAMS if query.get(k)), None)
        s = (hit or url.path.rstrip("/").rsplit("/", 1)[-1] or s).strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def parse_serials(text_blob="", file=None):
    """붙여넣은 글(줄바꿈·쉼표·공백 구분) 또는 엑셀/CSV에서 시리얼 목록을 뽑는다.
    파일은 '시리얼'·'serial'·'S/N' 이름이 들어간 열, 없으면 첫 번째 열을 쓴다."""
    vals = re.split(r"[\s,]+", text_blob or "")
    if file is not None:
        df = pd.read_csv(file, dtype=str) if file.name.lower().endswith(".csv") else pd.read_excel(file, dtype=str)
        col = next((c for c in df.columns if re.search(r"시리얼|serial|s/?n", str(c), re.I)), df.columns[0])
        vals += df[col].dropna().tolist()
    out = []
    for v in vals:
        s = clean_serial(v)
        if s and s not in out:
            out.append(s)
    return out


def _latest_sql():
    # 시리얼마다 가장 최근 검사 1건
    return """
        select distinct on (batch_id, serial) * from device_inspections
        order by batch_id, serial, inspected_at desc, id desc
    """


def list_batches():
    conn = get_db_connection()
    return conn.query(
        f"""
        select b.*, l.line_no, l.item_name as line_item_name,
               coalesce(e.n, 0) as listed,
               coalesce(x.inspected, 0) as inspected,
               coalesce(x.ok, 0) as ok, coalesce(x.ng, 0) as ng
        from inspection_batches b
        left join purchase_lines l on l.id = b.line_id
        left join (select batch_id, count(*) n from inspection_expected_serials group by 1) e on e.batch_id = b.id
        left join (
            select batch_id, count(*) inspected,
                   count(*) filter (where result = 'OK') ok, count(*) filter (where result = 'NG') ng
            from ({_latest_sql()}) t group by 1
        ) x on x.batch_id = b.id
        order by b.status, b.created_at desc
        """,
        ttl=0,
    )


def create_batch(name, device_type, model, po_no, line_id, expected_qty, serials, created_by):
    conn = get_db_connection()
    with conn.session as s:
        bid = s.execute(
            text(
                """
                insert into inspection_batches (name, device_type, model, po_no, line_id, expected_qty, created_by)
                values (:n, :t, :m, :po, :l, :q, :by) returning id
                """
            ),
            {"n": name, "t": device_type, "m": model or None, "po": po_no or None,
             "l": int(line_id) if line_id else None, "q": int(expected_qty) if expected_qty else None,
             "by": created_by},
        ).scalar()
        _insert_serials(s, bid, serials)
        s.commit()
    return bid


def _insert_serials(session, batch_id, serials):
    if serials:
        session.execute(
            text("insert into inspection_expected_serials (batch_id, serial) values (:b, :s) on conflict do nothing"),
            [{"b": int(batch_id), "s": x} for x in serials],
        )


def add_serials(batch_id, serials):
    conn = get_db_connection()
    with conn.session as s:
        _insert_serials(s, batch_id, serials)
        s.commit()


def set_batch_status(batch_id, status):
    conn = get_db_connection()
    with conn.session as s:
        s.execute(text("update inspection_batches set status = :st where id = :id"), {"st": status, "id": int(batch_id)})
        s.commit()


def delete_batch(batch_id):
    """검사 기록이 없는 검사 건만 지운다 (잘못 만든 경우)."""
    conn = get_db_connection()
    with conn.session as s:
        n = s.execute(text("select count(*) from device_inspections where batch_id = :b"), {"b": int(batch_id)}).scalar()
        if n:
            return False
        s.execute(text("delete from inspection_batches where id = :b"), {"b": int(batch_id)})
        s.commit()
    return True


def expected_serials(batch_id):
    conn = get_db_connection()
    df = conn.query("select serial from inspection_expected_serials where batch_id = :b order by serial",
                    params={"b": int(batch_id)}, ttl=0)
    return df["serial"].tolist()


def serial_history(serial):
    """그 시리얼의 모든 검사 (최신이 위)."""
    conn = get_db_connection()
    return conn.query(
        """
        select i.id, i.inspected_at, i.result, i.items, i.note, i.inspector, b.name as batch_name, b.id as batch_id,
               (select count(*) from inspection_photos p where p.inspection_id = i.id) as photos
        from device_inspections i join inspection_batches b on b.id = i.batch_id
        where i.serial = :s order by i.inspected_at desc, i.id desc
        """,
        params={"s": serial}, ttl=0,
    )


def list_inspections(batch_id):
    conn = get_db_connection()
    return conn.query(
        """
        select i.id, i.serial, i.result, i.items, i.note, i.inspector, i.account, i.inspected_at, i.receipt_id,
               (select count(*) from inspection_photos p where p.inspection_id = i.id) as photos
        from device_inspections i where i.batch_id = :b
        order by i.inspected_at desc, i.id desc
        """,
        params={"b": int(batch_id)}, ttl=0,
    )


def recent_inspectors(limit=30):
    conn = get_db_connection()
    df = conn.query(
        "select inspector from device_inspections group by inspector order by max(inspected_at) desc limit :n",
        params={"n": limit}, ttl=0,
    )
    return df["inspector"].tolist()


def shrink_photo(raw):
    """휴대폰 원본 사진을 긴 변 1280px JPEG로 줄인다 (보통 수 MB → 200KB 안팎)."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    im.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=75)
    return buf.getvalue()


def save_inspection(batch, serial, items, note, inspector, account, photos):
    """검사 1건 저장. items = [{"item", "result", "note"}], photos = [(항목, 원본 바이트)].
    이미 OK로 통과한 시리얼이면 저장하지 않고 ValueError. 반환: (검사 id, 결과)."""
    result = RESULT_NG if any(it["result"] == RESULT_NG for it in items) else RESULT_OK
    images = [(item, shrink_photo(raw)) for item, raw in photos]
    conn = get_db_connection()
    with conn.session as s:
        # 두 사람이 같은 번호를 동시에 저장해도 한 번만 통과하도록 시리얼 단위로 잠근다.
        s.execute(text("select pg_advisory_xact_lock(hashtext(:s))"), {"s": serial})
        passed = s.execute(
            text("select b.name from device_inspections i join inspection_batches b on b.id = i.batch_id "
                 "where i.serial = :s and i.result = 'OK' limit 1"),
            {"s": serial},
        ).scalar()
        if passed:
            raise ValueError(f"{serial}은(는) 이미 입고검사를 통과했어요 ({passed}).")
        receipt_id = None
        if result == RESULT_OK and pd.notna(batch.get("line_id")):
            receipt_id = s.execute(
                text(
                    """
                    insert into purchase_receipts (line_id, qty, received_on, location, source, entered_by)
                    select id, 1, :d, destination, 'app', :by from purchase_lines where id = :l returning id
                    """
                ),
                {"l": int(batch["line_id"]), "d": datetime.now(KST).date(), "by": f"{inspector} (입고검사 {serial})"},
            ).scalar()
        iid = s.execute(
            text(
                """
                insert into device_inspections (batch_id, serial, result, items, note, inspector, account, receipt_id)
                values (:b, :s, :r, cast(:items as jsonb), :note, :insp, :acc, :rc) returning id
                """
            ),
            {"b": int(batch["id"]), "s": serial, "r": result, "items": json.dumps(items, ensure_ascii=False),
             "note": note or None, "insp": inspector, "acc": account, "rc": receipt_id},
        ).scalar()
        for item, img in images:
            s.execute(text("insert into inspection_photos (inspection_id, item, image) values (:i, :item, :img)"),
                      {"i": iid, "item": item, "img": img})
        s.commit()
    if receipt_id:
        load_purchase_lines.clear()
    return iid, result


def delete_inspection(inspection_id):
    """잘못 저장한 검사 취소 — 그 검사로 남긴 입고 기록과 사진도 같이 지운다."""
    conn = get_db_connection()
    with conn.session as s:
        rc = s.execute(text("delete from device_inspections where id = :i returning receipt_id"),
                       {"i": int(inspection_id)}).scalar()
        if rc:
            s.execute(text("delete from purchase_receipts where id = :r and source = 'app'"), {"r": int(rc)})
        s.commit()
    if rc:
        load_purchase_lines.clear()


def load_photos(inspection_id):
    """[(항목, jpeg 바이트)]."""
    # conn.query는 결과를 pickle로 캐시하는데 bytea(memoryview)는 pickle이 안 돼서 세션으로 직접 읽는다.
    conn = get_db_connection()
    with conn.session as s:
        rows = s.execute(text("select item, image from inspection_photos where inspection_id = :i order by id"),
                         {"i": int(inspection_id)}).all()
    return [(item or "", bytes(img)) for item, img in rows]


def results_xlsx(batch, df, checklist):
    """검사 기록 엑셀 — 시리얼마다 최신 결과 1줄, 항목별 OK/NG 열."""
    latest = df.sort_values("inspected_at").drop_duplicates("serial", keep="last")
    rows = []
    for r in latest.itertuples():
        res = {it["item"]: it["result"] + (f" ({it['note']})" if it.get("note") else "") for it in (r.items or [])}
        rows.append({
            "시리얼": r.serial, "결과": r.result, **{c: res.get(c, "") for c in checklist},
            "메모": r.note or "", "검사자": r.inspector,
            "검사 일시": pd.Timestamp(r.inspected_at).tz_convert(KST).strftime("%Y-%m-%d %H:%M"), "사진": r.photos,
        })
    buf = io.BytesIO()
    sheet = re.sub(r"[\[\]:*?/\\]", " ", str(batch["name"])).strip()[:31] or "입고검사"
    pd.DataFrame(rows).to_excel(buf, index=False, sheet_name=sheet)
    return buf.getvalue()
