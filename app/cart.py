"""In-call cart. Lives in the call session (Redis in production) until the caller
confirms; every change is validated against the menu so the agent can never add an
item, option or price that does not exist."""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.menu import money
from app.models import LineSource, MenuItem, Modifier


class CartError(ValueError):
    """A change the caller asked for is not possible; message is safe to say aloud."""


@dataclass
class CartLine:
    line_id: int
    item_id: int
    name: str
    quantity: int
    unit_price_minor: int
    modifiers: list[dict] = field(default_factory=list)
    special_instructions: str = ""
    source: LineSource = LineSource.CUSTOMER

    @property
    def line_total_minor(self) -> int:
        return self.unit_price_minor * self.quantity


@dataclass
class Cart:
    restaurant_id: int
    currency: str = "INR"
    tax_rate_bp: int = 0
    lines: list[CartLine] = field(default_factory=list)
    _next_id: int = 1

    MAX_QTY_PER_LINE = 20

    # ---- mutations -------------------------------------------------------
    def add(
        self,
        session: Session,
        item_id: int,
        quantity: int = 1,
        modifier_ids: list[int] | None = None,
        special_instructions: str = "",
        source: LineSource = LineSource.CUSTOMER,
    ) -> CartLine:
        item = session.get(MenuItem, item_id)
        if item is None or item.category.restaurant_id != self.restaurant_id:
            raise CartError("That item is not on our menu.")
        if not item.is_available:
            raise CartError(f"Sorry, {item.name} is sold out right now.")
        self._check_qty(quantity)
        mods = self._validate_modifiers(session, item, modifier_ids or [])
        unit = item.price_minor + sum(m["price_delta_minor"] for m in mods)
        line = CartLine(
            line_id=self._next_id,
            item_id=item.id,
            name=item.name,
            quantity=quantity,
            unit_price_minor=unit,
            modifiers=mods,
            special_instructions=special_instructions.strip()[:300],
            source=source,
        )
        self._next_id += 1
        self.lines.append(line)
        return line

    def update(
        self,
        session: Session,
        line_id: int,
        quantity: int | None = None,
        modifier_ids: list[int] | None = None,
        special_instructions: str | None = None,
    ) -> CartLine:
        line = self._line(line_id)
        if quantity is not None:
            if quantity == 0:
                self.remove(line_id)
                return line
            self._check_qty(quantity)
            line.quantity = quantity
        if modifier_ids is not None:
            item = session.get(MenuItem, line.item_id)
            line.modifiers = self._validate_modifiers(session, item, modifier_ids)
            line.unit_price_minor = item.price_minor + sum(m["price_delta_minor"] for m in line.modifiers)
        if special_instructions is not None:
            line.special_instructions = special_instructions.strip()[:300]
        return line

    def remove(self, line_id: int) -> CartLine:
        line = self._line(line_id)
        self.lines.remove(line)
        return line

    # ---- reads -----------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def subtotal_minor(self) -> int:
        return sum(l.line_total_minor for l in self.lines)

    @property
    def tax_minor(self) -> int:
        return round(self.subtotal_minor * self.tax_rate_bp / 10_000)

    @property
    def total_minor(self) -> int:
        return self.subtotal_minor + self.tax_minor

    def item_ids(self) -> set[int]:
        return {l.item_id for l in self.lines}

    def summary(self) -> dict:
        c = self.currency
        return {
            "lines": [
                {
                    "line_id": l.line_id,
                    "item": l.name,
                    "quantity": l.quantity,
                    "options": [m["name"] for m in l.modifiers],
                    "special_instructions": l.special_instructions or None,
                    "line_total": money(l.line_total_minor, c),
                }
                for l in self.lines
            ],
            "subtotal": money(self.subtotal_minor, c),
            "tax": money(self.tax_minor, c),
            "total": money(self.total_minor, c),
        }

    # ---- helpers ---------------------------------------------------------
    def _line(self, line_id: int) -> CartLine:
        for l in self.lines:
            if l.line_id == line_id:
                return l
        raise CartError("I couldn't find that item in your order.")

    def _check_qty(self, quantity: int) -> None:
        if quantity < 1 or quantity > self.MAX_QTY_PER_LINE:
            raise CartError(f"Quantity must be between 1 and {self.MAX_QTY_PER_LINE}.")

    @staticmethod
    def _validate_modifiers(session: Session, item: MenuItem, modifier_ids: list[int]) -> list[dict]:
        chosen: list[Modifier] = []
        for mid in dict.fromkeys(modifier_ids):  # dedupe, keep order
            m = session.get(Modifier, mid)
            if m is None or m.group.item_id != item.id:
                raise CartError(f"That option isn't available for {item.name}.")
            if not m.is_available:
                raise CartError(f"Sorry, {m.name} is not available right now.")
            chosen.append(m)
        for g in item.modifier_groups:
            n = sum(1 for m in chosen if m.group_id == g.id)
            if n < g.min_select:
                options = ", ".join(m.name for m in g.modifiers if m.is_available)
                raise CartError(f"Please choose a {g.name.lower()} for {item.name}: {options}.")
            if n > g.max_select:
                raise CartError(f"You can choose up to {g.max_select} {g.name.lower()} for {item.name}.")
        return [{"id": m.id, "name": m.name, "price_delta_minor": m.price_delta_minor} for m in chosen]
