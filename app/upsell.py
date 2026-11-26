"""Cross-sell engine. The backend decides *what* to suggest; Claude decides how to
say it. Returns at most `limit` available items not already in the cart.

Order of checks:
1. Pairing rules staff configured (upsell_rules), highest priority first.
2. Category gaps: a main with no drink / side / dessert -> most popular item there.
3. (Phase 2) items frequently bought together, from order history.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cart import Cart
from app.menu import all_items, money
from app.models import MenuItem, UpsellRule

GAP_ORDER = ("drink", "side", "dessert")


def suggest(session: Session, cart: Cart, limit: int = 2, exclude_allergens: list[str] | None = None) -> list[dict]:
    if cart.is_empty:
        return []
    exclude_allergens = [a.lower() for a in (exclude_allergens or [])]
    items = {i.id: i for i in all_items(session, cart.restaurant_id)}
    in_cart = cart.item_ids()
    kinds_in_cart = {items[i].category.kind for i in in_cart if i in items}

    def ok(item: MenuItem | None) -> bool:
        return (
            item is not None
            and item.is_available
            and item.id not in in_cart
            and not any(a.lower() in exclude_allergens for a in item.allergens)
        )

    picks: list[tuple[MenuItem, str, str]] = []  # (item, reason, pitch)

    rules = session.scalars(
        select(UpsellRule)
        .where(UpsellRule.restaurant_id == cart.restaurant_id, UpsellRule.active.is_(True))
        .order_by(UpsellRule.priority.desc())
    )
    for rule in rules:
        triggered = (rule.trigger_item_id in in_cart) or (
            rule.trigger_category_id is not None
            and any(items[i].category_id == rule.trigger_category_id for i in in_cart if i in items)
        )
        cand = items.get(rule.suggest_item_id)
        if triggered and ok(cand) and all(cand.id != p[0].id for p in picks):
            picks.append((cand, "pairing", rule.pitch))

    if "main" in kinds_in_cart:
        for kind in GAP_ORDER:
            if kind in kinds_in_cart or any(p[0].category.kind == kind for p in picks):
                continue
            pool = sorted((i for i in items.values() if i.category.kind == kind and ok(i)), key=lambda i: -i.popularity)
            if pool:
                picks.append((pool[0], f"no {kind} in order", ""))

    return [
        {
            "item_id": item.id,
            "name": item.name,
            "price": money(item.price_minor, cart.currency),
            "reason": reason,
            "pitch": pitch or None,
        }
        for item, reason, pitch in picks[:limit]
    ]
