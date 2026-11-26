"""Talk to the agent in your terminal, no phone needed. Uses the same session code
as real calls. Needs ANTHROPIC_API_KEY.

  python -m app.simulator                  # inbound order call
  python -m app.simulator --feedback 12    # feedback call for order 12
"""

import argparse
import asyncio

from sqlalchemy import select

from app.agent.session import new_feedback_session, new_order_session, order_greeting
from app.db import init_db, session_scope
from app.models import Restaurant
from app.seed import DEMO_MENU, load_menu


async def chat(sess, greeting: str) -> None:
    print(f"\nAGENT: {greeting}")
    while not sess.ended:
        try:
            text = input("YOU:   ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        reply = await sess.respond(text)
        print(f"AGENT: {reply}")
        if getattr(sess.state, "transfer_reason", None):
            print(f"[transfer to human: {sess.state.transfer_reason}]")
            break
    st = sess.state
    if getattr(st, "placed_order_id", None):
        print(f"[order {st.placed_order_id} placed]")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--feedback", type=int, metavar="ORDER_ID")
    p.add_argument("--phone", default="+15550123456", help="simulated caller ID")
    args = p.parse_args()

    import json

    init_db()
    with session_scope() as s:
        r = s.scalar(select(Restaurant).limit(1)) or load_menu(s, json.loads(DEMO_MENU.read_text()))
        rid, name = r.id, r.name

    if args.feedback:
        sess, greeting = new_feedback_session(args.feedback)
    else:
        sess, greeting = new_order_session(rid, caller_phone=args.phone), order_greeting(name)
    asyncio.run(chat(sess, greeting))


if __name__ == "__main__":
    main()
