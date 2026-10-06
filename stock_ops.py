"""in-memory 캠프 재고(data["items"]) 조회/증감 헬퍼."""


def find_item_by_code(inventory, code):
    for it in inventory["items"]:
        if it["c"] == code:
            return it
    return None


def deduct_camp_stock(item, camp, qty):
    """캠프 재고에서 qty만큼 차감하고, 차감된 금액을 반환한다 (재고 부족 시 ValueError)."""
    pair = item["x"].get(camp)
    have = pair[0] if pair else 0
    if have < qty:
        raise ValueError(f"{camp}의 재고가 부족합니다. (보유 {have}개, 요청 {qty}개)")
    q, a = pair
    unit_amt = a / q if q else 0
    moved_amt = round(unit_amt * qty)
    new_q, new_a = q - qty, a - moved_amt
    if new_q <= 0:
        del item["x"][camp]
    else:
        item["x"][camp] = [new_q, new_a]
    return moved_amt


def add_camp_stock(item, camp, qty, amt):
    """캠프 재고에 qty/amt만큼 더한다 (해당 캠프에 재고가 없었다면 새로 만든다)."""
    pair = item["x"].get(camp)
    if pair:
        item["x"][camp] = [pair[0] + qty, pair[1] + amt]
    else:
        item["x"][camp] = [qty, amt]
