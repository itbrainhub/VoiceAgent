"""Menu lookups. Every fact the agent says about the menu comes from here."""

import difflib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import MenuCategory, MenuItem, ModifierGroup


def money(minor: int, currency: str = "INR") -> str:
    symbol = {"INR": "Rs ", "USD": "$", "GBP": "£", "EUR": "€"}.get(currency, f"{currency} ")
    major = minor / 100
    return f"{symbol}{major:.0f}" if minor % 100 == 0 else f"{symbol}{major:.2f}"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def all_items(session: Session, restaurant_id: int) -> list[MenuItem]:
    return list(
        session.scalars(
            select(MenuItem)
            .join(MenuCategory)
            .where(MenuCategory.restaurant_id == restaurant_id)
            .options(
                selectinload(MenuItem.category),
                selectinload(MenuItem.modifier_groups).selectinload(ModifierGroup.modifiers),
            )
            .order_by(MenuCategory.sort_order, MenuItem.id)
        )
    )


def find_items(session: Session, restaurant_id: int, query: str, limit: int = 5) -> list[MenuItem]:
    """Match a spoken item name, synonym, category or ingredient. Tolerates small
    speech-to-text errors via fuzzy matching."""
    q = _norm(query)
    items = all_items(session, restaurant_id)
    if not q:
        return items[:limit]

    scored: list[tuple[float, MenuItem]] = []
    for item in items:
        names = [_norm(item.name), *(_norm(s) for s in item.synonyms)]
        best = 0.0
        for n in names:
            if q == n:
                best = max(best, 1.0)
            elif q in n or n in q:
                best = max(best, 0.9)
            else:
                best = max(best, difflib.SequenceMatcher(None, q, n).ratio())
        if q in _norm(item.category.name) or _norm(item.category.kind) == q.rstrip("s"):
            best = max(best, 0.8)
        if any(q in _norm(ing) for ing in item.ingredients):
            best = max(best, 0.7)
        if q in ("veg", "vegetarian") and item.is_veg:
            best = max(best, 0.8)
        if best >= 0.65:
            scored.append((best, item))

    scored.sort(key=lambda t: (-t[0], -t[1].popularity))
    return [item for _, item in scored[:limit]]


def describe_item(item: MenuItem, currency: str = "INR") -> dict:
    return {
        "item_id": item.id,
        "name": item.name,
        "category": item.category.name,
        "price": money(item.price_minor, currency),
        "available": item.is_available,
        "vegetarian": item.is_veg,
        "description": item.description,
        "ingredients": item.ingredients,
        "allergens": item.allergens,
        "spice_level": ["not spicy", "mild", "medium", "hot"][min(item.spice_level, 3)],
        "options": [
            {
                "group": g.name,
                "required": g.min_select > 0,
                "choose_up_to": g.max_select,
                "choices": [
                    {
                        "modifier_id": m.id,
                        "name": m.name,
                        "price_change": money(m.price_delta_minor, currency) if m.price_delta_minor else "no change",
                    }
                    for m in g.modifiers
                    if m.is_available
                ],
            }
            for g in item.modifier_groups
        ],
    }


def menu_overview(session: Session, restaurant_id: int, currency: str = "INR") -> list[dict]:
    """A compact menu (names, prices, availability) for the system prompt."""
    out: dict[str, list[str]] = {}
    for item in all_items(session, restaurant_id):
        tag = "" if item.is_available else " (sold out)"
        veg = " [veg]" if item.is_veg else ""
        out.setdefault(item.category.name, []).append(
            f"{item.name}{veg} {money(item.price_minor, currency)}{tag} (id {item.id})"
        )
    return [{"category": k, "items": v} for k, v in out.items()]
