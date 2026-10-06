"""사용량(usage) 데이터 기반 계산 헬퍼. 전역 상태를 참조하지 않고 usage를 명시적으로 받는다."""

from datetime import datetime, timedelta


def get_usage_for_code(usage, code):
    if not usage or not code:
        return None
    return usage["items"].get(str(code).strip())


def weekly_usage_rate(usage, code):
    """해당 SKU의 전체 캠프 합산 주 평균 사용량 (사용량 데이터 없으면 None)."""
    item_usage = get_usage_for_code(usage, code)
    if not item_usage:
        return None
    total_avg = sum(u.get("avg", 0) for u in item_usage.values())
    return total_avg if total_avg > 0 else None


def estimate_depletion(usage, qty, code):
    """qty(재고 수량)와 SKU의 주 평균 사용량으로 소진까지 남은 주 수와 예상 소진일을 계산."""
    rate = weekly_usage_rate(usage, code)
    if not rate:
        return None, None
    weeks = qty / rate
    depletion_date = (datetime.now() + timedelta(weeks=weeks)).date()
    return round(weeks, 1), depletion_date
