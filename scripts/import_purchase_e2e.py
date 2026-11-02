"""E2E 통합이력 엑셀(가장 최근 수정 파일)을 purchase_lines/purchase_receipts에 반영한다.
폴더는 secrets.toml의 [purchase] e2e_dir(구글 드라이브 경로)과 e2e_prefix(파일명 앞부분),
없으면 프로젝트의 구매e2e 폴더.

    python scripts/import_purchase_e2e.py            # 미리보기 (DB 변경 없음)
    python scripts/import_purchase_e2e.py --apply    # 실제 반영
    python scripts/import_purchase_e2e.py --file 경로.xlsx
"""

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # .streamlit/secrets.toml을 찾도록

import streamlit as st  # noqa: E402

from purchase_import import import_e2e  # noqa: E402

LOCAL_FOLDER = os.path.join(ROOT, "구매e2e")


def source():
    """(폴더, 파일명 앞부분). 드라이브 폴더에는 E2E 파일이 여러 개라 e2e_prefix로 원본을 고정한다."""
    try:
        cfg = st.secrets["purchase"]
        return cfg["e2e_dir"], cfg.get("e2e_prefix", "")
    except (KeyError, FileNotFoundError):
        return LOCAL_FOLDER, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 반영 (없으면 미리보기만)")
    ap.add_argument("--file", help="반영할 파일 (없으면 폴더의 가장 최근 E2E 파일)")
    args = ap.parse_args()

    path = args.file
    if not path:
        folder, prefix = source()
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith(".xlsx") and "E2E" in f and f.startswith(prefix) and not f.startswith("~$")]
        if not files:
            print(f"{folder} 폴더에 E2E 엑셀 파일이 없어요.")
            return
        path = max(files, key=os.path.getmtime)
        if len(files) > 1:
            print("E2E 파일이 여러 개예요 — 가장 최근 수정 파일을 읽어요:")
            for f in sorted(files, key=os.path.getmtime, reverse=True):
                print(f"  {datetime.fromtimestamp(os.path.getmtime(f)):%Y-%m-%d %H:%M}  {os.path.basename(f)}")

    # 엑셀/원드라이브가 파일을 잡고 있어도 읽을 수 있게 임시 복사본을 쓴다.
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "e2e.xlsx")
        shutil.copyfile(path, copy)
        r = import_e2e(copy, dry_run=not args.apply)

    print(f"[{'반영' if args.apply else '미리보기'}] {os.path.basename(path)}")
    print(f"  엑셀 {r['rows']:,}줄 → 새 줄 {r['new_lines']:,} · 바뀐 줄 {r['updated_lines']:,} · "
          f"그대로 {r['unchanged_lines']:,}")
    print(f"  입고 기록 변경 {r['receipt_changes']:,}줄 · 앱 입고가 있어 엑셀 입고값을 건너뛴 줄 {r['kept_app_receipts']:,}")
    if r["changed_fields"]:
        print("  바뀐 항목: " + ", ".join(f"{k} {v}" for k, v in r["changed_fields"].most_common()))
    if r["db_only_lines"]:
        print(f"  DB에만 있고 이 엑셀엔 없는 줄 {r['db_only_lines']:,} (지우지 않음)")
    for x in r["relabeled"]:
        print(f"  관리번호를 엑셀에 맞춤: {x['이전 관리번호']} → {x['새 관리번호']} · {x['PO']} · {x['품명']}")
    for x in r["renumbered"]:
        print(f"  관리번호 새로 부여: {x['엑셀 관리번호']} → {x['새 관리번호']} · {x['PO']} · {x['품명']}")


if __name__ == "__main__":
    main()
