"""Load a restaurant menu from JSON. Prices in the JSON are in major units (rupees,
dollars) and are converted to minor units on load.

Usage: python -m app.seed [path/to/menu.json]
"""

import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import init_db, session_scope
from app.models import (
    MenuCategory,
    MenuItem,
    Modifier,
    ModifierGroup,
    Restaurant,
    UpsellRule,
)

DEMO_MENU = Path(__file__).parent / "data" / "demo_menu.json"


def _minor(major: float) -> int:
    return round(major * 100)


def load_menu(session: Session, data: dict) -> Restaurant:
    r = data["restaurant"]
    existing = session.scalar(select(Restaurant).where(Restaurant.phone_number == r.get("phone_number")))
    if existing:
        return existing

    restaurant = Restaurant(
        name=r["name"],
        phone_number=r.get("phone_number"),
        timezone=r.get("timezone", "UTC"),
        currency=r.get("currency", "INR"),
        tax_rate_bp=r.get("tax_rate_bp", 0),
        hours=r.get("hours", {}),
    )
    session.add(restaurant)
    by_name: dict[str, MenuItem] = {}

    for sort_order, c in enumerate(data["categories"]):
        cat = MenuCategory(restaurant=restaurant, name=c["name"], kind=c.get("kind", "other"), sort_order=sort_order)
        session.add(cat)
        for i in c["items"]:
            item = MenuItem(
                category=cat,
                name=i["name"],
                description=i.get("description", ""),
                price_minor=_minor(i["price"]),
                is_available=i.get("available", True),
                is_veg=i.get("veg", False),
                dietary_tags=i.get("dietary_tags", []),
                allergens=i.get("allergens", []),
                ingredients=i.get("ingredients", []),
                spice_level=i.get("spice_level", 0),
                synonyms=i.get("synonyms", []),
                popularity=i.get("popularity", 0),
            )
            session.add(item)
            by_name[item.name.lower()] = item
            for g in i.get("modifier_groups", []):
                group = ModifierGroup(item=item, name=g["name"], min_select=g.get("min", 0), max_select=g.get("max", 1))
                session.add(group)
                for o in g["options"]:
                    session.add(Modifier(group=group, name=o["name"], price_delta_minor=_minor(o.get("delta", 0))))

    session.flush()
    for rule in data.get("upsell_rules", []):
        session.add(
            UpsellRule(
                restaurant_id=restaurant.id,
                trigger_item_id=by_name[rule["trigger_item"].lower()].id if rule.get("trigger_item") else None,
                suggest_item_id=by_name[rule["suggest_item"].lower()].id,
                pitch=rule.get("pitch", ""),
                priority=rule.get("priority", 0),
            )
        )
    session.flush()
    return restaurant


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEMO_MENU
    init_db()
    with session_scope() as s:
        restaurant = load_menu(s, json.loads(path.read_text()))
        print(f"Loaded menu for {restaurant.name} (id={restaurant.id})")


if __name__ == "__main__":
    main()
