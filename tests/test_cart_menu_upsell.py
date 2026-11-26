import pytest

from app import menu, upsell
from app.cart import Cart, CartError
from app.models import Modifier


def make_cart(restaurant):
    return Cart(restaurant_id=restaurant.id, currency=restaurant.currency, tax_rate_bp=restaurant.tax_rate_bp)


def test_search_handles_synonyms_and_speech_errors(db, restaurant):
    assert menu.find_items(db, restaurant.id, "coke")[0].name == "Coca-Cola"
    assert menu.find_items(db, restaurant.id, "butter chiken")[0].name == "Butter Chicken"
    names = {i.name for i in menu.find_items(db, restaurant.id, "drinks")}
    assert {"Mango Lassi", "Coca-Cola"} <= names
    assert menu.find_items(db, restaurant.id, "sushi") == []


def test_money_formatting():
    assert menu.money(34000) == "Rs 340"
    assert menu.money(1250, "USD") == "$12.50"


def test_required_option_enforced_and_priced(db, restaurant, item_id):
    cart = make_cart(restaurant)
    bc = item_id("Butter Chicken")
    with pytest.raises(CartError, match="portion"):
        cart.add(db, bc, 1)
    half = db.query(Modifier).filter_by(name="Half").one()
    line = cart.add(db, bc, 2, [half.id])
    assert line.unit_price_minor == 34000 - 12000
    assert cart.subtotal_minor == 2 * 22000
    assert cart.tax_minor == round(44000 * 0.05)
    assert cart.total_minor == 44000 + 2200


def test_rejects_sold_out_foreign_option_and_bad_quantity(db, restaurant, item_id):
    cart = make_cart(restaurant)
    with pytest.raises(CartError, match="sold out"):
        cart.add(db, item_id("Kulfi"), 1)
    egg = db.query(Modifier).filter_by(name="Boiled egg").one()
    with pytest.raises(CartError, match="isn't available"):
        cart.add(db, item_id("Dal Makhani"), 1, [egg.id])
    with pytest.raises(CartError, match="between"):
        cart.add(db, item_id("Dal Makhani"), 0)
    with pytest.raises(CartError, match="not on our menu"):
        cart.add(db, 99999, 1)


def test_update_and_remove(db, restaurant, item_id):
    cart = make_cart(restaurant)
    line = cart.add(db, item_id("Butter Naan"), 2)
    cart.update(db, line.line_id, quantity=4)
    assert cart.subtotal_minor == 4 * 6000
    cart.update(db, line.line_id, quantity=0)
    assert cart.is_empty


def test_upsell_rule_first_then_category_gaps(db, restaurant, item_id):
    cart = make_cart(restaurant)
    cart.add(db, item_id("Dal Makhani"), 1)
    picks = upsell.suggest(db, cart)
    assert [p["name"] for p in picks] == ["Jeera Rice", "Mango Lassi"]  # rule, then top drink
    assert picks[0]["reason"] == "pairing"


def test_upsell_skips_items_in_cart_unavailable_and_allergens(db, restaurant, item_id):
    cart = make_cart(restaurant)
    cart.add(db, item_id("Paneer Tikka Masala"), 1)
    cart.add(db, item_id("Mango Lassi"), 1)
    cart.add(db, item_id("Garlic Naan"), 1)
    # Has main + drink + side, so only dessert gap; Kulfi is sold out -> Gulab Jamun.
    assert [p["name"] for p in upsell.suggest(db, cart)] == ["Gulab Jamun"]
    # Gulab Jamun contains gluten.
    assert upsell.suggest(db, cart, exclude_allergens=["gluten"]) == []


def test_no_upsell_for_empty_cart(db, restaurant):
    assert upsell.suggest(db, make_cart(restaurant)) == []
