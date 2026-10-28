"""구매e2e 폴더의 E2E 통합이력 엑셀(가장 최근 수정 파일)을 purchase_lines/purchase_receipts에 반영한다.

    python scripts/import_purchase_e2e.py            # 미리보기 (DB 변경 없음)
    python scripts/import_purchase_e2e.py --apply    # 실제 반영
    python scripts/import_purchase_e2e.py --file 경로.xlsx
"""

import argparse
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # .streamlit/secrets.toml을 찾도록

from purchase_import import import_e2e  # noqa: E402

FOLDER = os.path.join(ROOT, "구매e2e")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 반영 (없으면 미리보기만)")
    ap.add_argument("--file", help="반영할 파일 (없으면 구매e2e 폴더의 가장 최근 파일)")
    args = ap.parse_args()

    path = args.file
    if not path:
        files = [os.path.join(FOLDER, f) for f in os.listdir(FOLDER)
                 if f.lower().endswith(".xlsx") and not f.startswith("~$")]
        if not files:
            print("구매e2e 폴더에 엑셀 파일이 없어요.")
            return
        path = max(files, key=os.path.getmtime)

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
    for x in r["renumbered"]:
        print(f"  관리번호 새로 부여: {x['엑셀 관리번호']} → {x['새 관리번호']} · {x['PO']} · {x['품명']}")


if __name__ == "__main__":
    main()
