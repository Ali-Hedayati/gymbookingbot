"""Phase-one customer booking and admin management screens."""

import asyncio
import contextlib
import json
import logging
from datetime import datetime, date, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import bookings as data
from storage import (
    get_settings,
    load_week,
    get_customer,
    customer_by_telegram,
    search_customers,
    create_customer,
    normalize_phone,
    phone_digits,
)

TZ = ZoneInfo("Asia/Tehran")
LOGGER = logging.getLogger(__name__)
admin_router = None
CONFIG_LABELS = {
    "card_number": "Payment card number",
    "admin_phone": "Admin contact phone",
    "private_price": "Private price per person",
    "public_price": "Public price per person",
    "currency": "Currency label",
}


def button(text, action):
    return InlineKeyboardButton(text, callback_data=action)


def markup(rows):
    return InlineKeyboardMarkup(rows)


def stamp(value):
    return datetime.fromisoformat(value).astimezone(TZ).strftime("%a %Y-%m-%d %H:%M")


def slot_label(item):
    return f"{'🟢' if item['available'] else '🔴'} {datetime.fromisoformat(item['starts_at']):%H:%M} · {item['session_type'].title()} · {item['available']}/{item['capacity']} free"


def slot_text(item):
    return (
        f"Session #{item['id']}\n{stamp(item['starts_at'])}–{datetime.fromisoformat(item['ends_at']):%H:%M}\n"
        f"Type · {item['session_type'].title()}\nCapacity · {item['capacity']}\n"
        f"Occupied · {item['occupied']}\nAvailable · {item['available']}\n"
        f"Price per person · {item['amount']} {item['currency']}\n"
        f"Status · {'Cancelled' if item['cancelled'] else 'Open'}"
    )


def booking_text(item, admin=False):
    status = {
        "held": "Pre-reserved",
        "pending": "Awaiting payment review",
        "confirmed": "Reserved",
        "expired": "Expired",
        "rejected": "Rejected",
        "cancelled": "Cancelled",
    }[item["status"]]
    text = (
        f"Booking #{item['id']} · {status}\n{stamp(item['starts_at'])}–{datetime.fromisoformat(item['ends_at']):%H:%M}\n"
        f"Type · {item['session_type'].title()}\nAmount · {item['amount']} {item['currency']}\n"
        f"Paid · {'Yes' if item['is_paid'] else 'No'}"
    )
    if admin:
        text += f"\nCustomer #{item['customer_id']} · {item['name'] or 'Unnamed'}\nPhone · {item['phone']}\nTelegram ID · {item['telegram_id'] or 'Not linked'}"
    if item["tracking_code"]:
        text += f"\nTracking code · {item['tracking_code']}"
    if item["note"]:
        text += f"\nMessage · {item['note']}"
    return text


async def render(update, text, rows):
    query = update.callback_query
    if query is not None:
        message = query.message
        if message.photo or message.document:
            return await message.reply_text(text, reply_markup=markup(rows))
        # Repeated delivery of a callback can legitimately reach the same screen.
        if message.text == text and message.reply_markup == markup(rows):
            return
        return await query.edit_message_text(text, reply_markup=markup(rows))
    return await update.message.reply_text(text, reply_markup=markup(rows))


async def send_customer_menu(message):
    await message.reply_text(
        "Gym booking\n\nChoose an action.",
        reply_markup=markup(
            [
                [button("📅 Book a session", "p:days:0")],
                [button("My bookings", "p:mine:0")],
            ]
        ),
    )


async def ask(update, context, kind, text, details, back, skip=False):
    context.user_data.pop("phase_confirm", None)
    context.user_data["phase_flow"] = {"kind": kind, "details": details, "back": back}
    rows = [[button("Skip", "p:skip")]] if skip else []
    rows.append([button("Back", "p:back")])
    await render(update, text, rows)


async def confirm(update, context, kind, details, text, back):
    context.user_data.pop("phase_flow", None)
    token = uuid4().hex[:12]
    context.user_data["phase_confirm"] = {
        "kind": kind,
        "details": details,
        "back": back,
        "id": token,
    }
    await render(
        update,
        text,
        [[button("Confirm", f"p:confirm:{token}")], [button("Back", "p:back")]],
    )


async def customer_booking(update, context, item, customer):
    if item["customer_id"] != customer["id"]:
        raise ValueError("This booking belongs to another customer.")
    rows = []
    text = booking_text(item)
    if item["status"] == "held":
        deadline = datetime.fromtimestamp(item["expires_at"], TZ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        text += (
            f"\n\nPay to card · {item['card_number']}\nReceipt deadline · {deadline} Tehran time\n"
            "Upload your payment receipt image before this deadline. Your place stays held during admin review."
        )
        context.user_data["receipt_booking_id"] = item["id"]
        rows.extend(
            [
                [button("Upload receipt for this booking", f"p:receipt:{item['id']}")],
                [button("Release my hold", f"p:release:{item['id']}")],
            ]
        )
    rows.append([button("Back · my bookings", "p:mine:0")])
    await render(update, text, rows)


async def admin_booking(update, context, item):
    rows = []
    if item["status"] == "pending":
        rows.append(
            [
                button("Accept", f"p:accept:{item['id']}"),
                button("Reject", f"p:reject:{item['id']}"),
            ]
        )
    if item["status"] == "confirmed" and not item["is_paid"]:
        rows.append([button("Mark paid", f"p:paid:{item['id']}")])
    if item["status"] in data.ACTIVE:
        rows.append([button("Cancel booking", f"p:cancelbooking:{item['id']}")])
    rows.append([button("Customer", f"p:customer:{item['customer_id']}")])
    rows.append([button("Back · session", f"p:aslot:{item['session_id']}")])
    await render(update, booking_text(item, True), rows)
    if item["receipt_file"]:
        sender = (
            context.bot.send_photo
            if item["receipt_kind"] == "photo"
            else context.bot.send_document
        )
        key = "photo" if item["receipt_kind"] == "photo" else "document"
        await sender(
            chat_id=update.effective_chat.id,
            caption=f"Receipt for booking #{item['id']}",
            **{key: item["receipt_file"]},
        )


async def page_bookings(update, items, title, back, prefix, page=0, return_to=None):
    page = max(0, page)
    page_count = max(1, (len(items) + 7) // 8)
    page = min(page, page_count - 1)
    rows = []
    for item in items[page * 8 : (page + 1) * 8]:
        rows.append(
            [
                button(
                    f"#{item['id']} · {stamp(item['starts_at'])} · {item['status']}",
                    f"{prefix}:{item['id']}",
                )
            ]
        )
    nav = []
    if page > 0:
        nav.append(button("Previous", f"{back}:{page - 1}"))
    if page + 1 < page_count:
        nav.append(button("Next", f"{back}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            button(
                "Back",
                return_to or ("p:home" if prefix == "p:booking" else "admin:home"),
            )
        ]
    )
    await render(
        update, f"{title}\nTotal · {len(items)}\nPage {page + 1} of {page_count}", rows
    )


async def show_live_day(update, context, admin_id, day, page=0):
    items = data.sessions(admin_id, day, future=False, include_cancelled=True)
    page_count = max(1, (len(items) + 11) // 12)
    page = max(0, min(page, page_count - 1))
    rows = [
        [
            button(
                slot_label(item) + (" · cancelled" if item["cancelled"] else ""),
                f"p:aslot:{item['id']}",
            )
        ]
        for item in items[page * 12 : (page + 1) * 12]
    ]
    nav = []
    if page > 0:
        nav.append(button("Previous", f"p:aday:{day}:{page - 1}"))
    if page + 1 < page_count:
        nav.append(button("Next", f"p:aday:{day}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("Add a session", f"p:manual:{day}")])
    week = day - timedelta(days=(day.weekday() - 5) % 7)
    rows.append([button("Back · week", f"admin:week:{week}")])
    await render(
        update,
        f"Published day · {day}\nSessions · {len(items)}\nPage {page + 1} of {page_count}",
        rows,
    )


async def route(update, context, admin_id, action):
    parts = action.split(":")
    command = parts[1]
    customer = customer_by_telegram(update.effective_user.id)
    is_admin = update.effective_user.id == admin_id
    customer_commands = {
        "home",
        "days",
        "day",
        "slot",
        "hold",
        "mine",
        "booking",
        "receipt",
        "release",
    }
    if (
        command not in customer_commands
        and command not in ("back", "confirm", "skip")
        and not is_admin
    ):
        raise ValueError("Admin access required.")
    if command in customer_commands and customer is None:
        raise ValueError(
            "Send /start and share your own phone number to register first."
        )
    if command not in ("back", "confirm", "skip", "pick", "assignpaid", "newcustomer"):
        context.user_data.pop("phase_flow", None)
        context.user_data.pop("phase_confirm", None)
    if command == "home":
        await render(
            update,
            "Gym booking",
            [
                [button("Book a session", "p:days:0")],
                [button("My bookings", "p:mine:0")],
            ],
        )
    elif command in ("days", "aslots"):
        admin_view = command == "aslots"
        items = data.sessions(admin_id)
        days = sorted(
            {
                datetime.fromisoformat(item["starts_at"]).date().isoformat()
                for item in items
            }
        )
        page = max(0, int(parts[2]))
        rows = [
            [button(day, ("p:aday:" if admin_view else "p:day:") + day)]
            for day in days[page * 10 : (page + 1) * 10]
        ]
        nav = []
        if page > 0:
            nav.append(button("Previous", f"p:{command}:{page - 1}"))
        if (page + 1) * 10 < len(days):
            nav.append(button("Next", f"p:{command}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("Back", "admin:home" if admin_view else "p:home")])
        await render(
            update,
            "Choose a day." if days else "No published upcoming sessions yet.",
            rows,
        )
    elif command in ("day", "aday"):
        day = date.fromisoformat(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        if command == "aday":
            await show_live_day(update, context, admin_id, day, page)
            return
        items = data.sessions(admin_id, day)
        rows = [
            [button(slot_label(item), f"p:slot:{item['id']}")]
            for item in items[page * 12 : (page + 1) * 12]
        ]
        nav = []
        if page > 0:
            nav.append(button("Previous", f"p:day:{day}:{page - 1}"))
        if (page + 1) * 12 < len(items):
            nav.append(button("Next", f"p:day:{day}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("Back · days", "p:days:0")])
        await render(
            update, f"Sessions · {day}\nEach booking reserves one place.", rows
        )
    elif command in ("slot", "aslot"):
        item = data.session(int(parts[2]), admin_id)
        if command == "slot":
            rows = []
            if (
                item["available"]
                and not item["cancelled"]
                and datetime.fromisoformat(item["starts_at"]).timestamp()
                > data.time.time()
            ):
                rows.append(
                    [
                        button(
                            "Pre-reserve one place",
                            f"p:hold:{item['id']}:{item['version']}",
                        )
                    ]
                )
            rows.append(
                [
                    button(
                        "Back · day",
                        f"p:day:{datetime.fromisoformat(item['starts_at']).date()}",
                    )
                ]
            )
        else:
            rows = [[button("Bookings / customers", f"p:abookings:{item['id']}:0")]]
            if not item["cancelled"]:
                rows.extend(
                    [
                        [button("Assign customer", f"p:assign:{item['id']}")],
                        [
                            button(
                                "Switch Private / Public",
                                f"p:edit:{item['id']}:type:{item['version']}",
                            )
                        ],
                        [
                            button(
                                "Edit capacity",
                                f"p:edit:{item['id']}:capacity:{item['version']}",
                            ),
                            button(
                                "Edit times",
                                f"p:edit:{item['id']}:times:{item['version']}",
                            ),
                        ],
                        [
                            button(
                                "Edit price",
                                f"p:edit:{item['id']}:price:{item['version']}",
                            )
                        ],
                        [
                            button(
                                "Cancel session",
                                f"p:edit:{item['id']}:cancel:{item['version']}",
                            )
                        ],
                    ]
                )
            rows.append(
                [
                    button(
                        "Back · day",
                        f"p:aday:{datetime.fromisoformat(item['starts_at']).date()}",
                    )
                ]
            )
        await render(update, slot_text(item), rows)
    elif command == "hold":
        item = data.hold(
            int(parts[2]), customer["id"], admin_id, expected_version=int(parts[3])
        )
        await customer_booking(update, context, item, customer)
    elif command == "mine":
        await page_bookings(
            update,
            data.bookings_for(admin_id, customer_id=customer["id"]),
            "My bookings",
            "p:mine",
            "p:booking",
            int(parts[2]),
        )
    elif command == "booking":
        await customer_booking(update, context, data.booking(int(parts[2])), customer)
    elif command == "receipt":
        item = data.booking(int(parts[2]))
        if item["customer_id"] != customer["id"] or item["status"] != "held":
            raise ValueError("Choose one of your active payment holds.")
        context.user_data["receipt_booking_id"] = item["id"]
        await render(
            update,
            f"Upload the receipt image for booking #{item['id']} now.\nAmount · {item['amount']} {item['currency']}",
            [[button("Back", f"p:booking:{item['id']}")]],
        )
    elif command == "release":
        item = data.booking(int(parts[2]))
        if item["customer_id"] != customer["id"] or item["status"] != "held":
            raise ValueError("Choose one of your active payment holds.")
        await confirm(
            update,
            context,
            "cancelbooking",
            {"id": item["id"]},
            f"Release booking #{item['id']}?",
            f"p:booking:{item['id']}",
        )
    elif command == "requests":
        await page_bookings(
            update,
            data.bookings_for(admin_id, pending=True),
            "Payment requests",
            "p:requests",
            "p:review",
            int(parts[2]),
        )
    elif command in ("review", "abooking"):
        item = data.booking(int(parts[2]))
        if item["admin_id"] != admin_id:
            raise ValueError("Access denied.")
        await admin_booking(update, context, item)
    elif command == "abookings":
        session_id = int(parts[2])
        await page_bookings(
            update,
            data.bookings_for(admin_id, session_id=session_id),
            "Session bookings",
            f"p:abookings:{session_id}",
            "p:abooking",
            int(parts[3]),
            f"p:aslot:{session_id}",
        )
    elif command in ("accept", "reject", "paid"):
        item = data.booking(int(parts[2]))
        if item["admin_id"] != admin_id:
            raise ValueError("Access denied.")
        expected = "confirmed" if command == "paid" else "pending"
        if item["status"] != expected:
            await admin_booking(update, context, item)
            return
        prompt = (
            "Send a rejection reason (up to 300 characters), or Skip."
            if command == "reject"
            else "Send the payment tracking code (up to 100 characters), or Skip."
        )
        await ask(
            update,
            context,
            command,
            prompt,
            {"id": item["id"]},
            f"p:abooking:{item['id']}",
            True,
        )
    elif command == "cancelbooking":
        item = data.booking(int(parts[2]))
        if item["admin_id"] != admin_id:
            raise ValueError("Access denied.")
        await confirm(
            update,
            context,
            "cancelbooking",
            {"id": item["id"]},
            booking_text(item, True)
            + "\n\nCancel this booking? Arrange any paid refund with the customer.",
            f"p:abooking:{item['id']}",
        )
    elif command == "edit":
        item = data.session(int(parts[2]), admin_id)
        field = parts[3]
        if int(parts[4]) != item["version"]:
            raise ValueError("This session changed. Reopen it before editing.")
        details = {"id": item["id"], "version": item["version"]}
        back = f"p:aslot:{item['id']}"
        if field == "type":
            new_type = "private" if item["session_type"] == "public" else "public"
            capacity = get_settings(admin_id)[new_type + "_capacity"]
            details["changes"] = {"session_type": new_type, "capacity": capacity}
            await confirm(
                update,
                context,
                "session_edit",
                details,
                f"Switch session #{item['id']} to {new_type} with capacity {capacity}?",
                back,
            )
        elif field == "cancel":
            details["changes"] = {"cancelled": 1}
            await confirm(
                update,
                context,
                "session_edit",
                details,
                "Cancel this session and its active bookings?\nCustomers will be notified. Arrange refunds for paid bookings.",
                back,
            )
        elif field in ("capacity", "times", "price"):
            details["field"] = field
            prompt = {
                "capacity": "Send the new capacity.",
                "times": "Send start and end times, such as 08:00 10:00.",
                "price": "Send the new whole-number price.",
            }[field]
            await ask(update, context, "session_edit", prompt, details, back)
    elif command in ("ds", "de"):
        day = date.fromisoformat(parts[2])
        index = int(parts[3])
        week = day - timedelta(days=(day.weekday() - 5) % 7)
        saved = load_week(admin_id, str(week))
        if saved["published"]:
            await show_live_day(update, context, admin_id, day)
            return
        slots = saved["days"].get(str(day), {}).get("sessions", [])
        if not 0 <= index < len(slots):
            raise ValueError("This session changed. Reopen the day.")
        expected_revision = int(parts[4] if command == "ds" else parts[5])
        if expected_revision != saved["revision"]:
            raise ValueError("This day changed. Reopen it before editing a slot.")
        slot = slots[index]
        back = f"p:ds:{day}:{index}:{saved['revision']}"
        details = {"day": str(day), "index": index, "revision": saved["revision"]}
        if command == "ds":
            await render(
                update,
                f"{stamp(slot['start'])}–{datetime.fromisoformat(slot['end']):%H:%M}\nType · {slot['session_type']}\nCapacity · {slot['capacity']}",
                [
                    [
                        button(
                            "Switch Private / Public",
                            f"p:de:{day}:{index}:type:{saved['revision']}",
                        )
                    ],
                    [
                        button(
                            "Edit capacity",
                            f"p:de:{day}:{index}:capacity:{saved['revision']}",
                        ),
                        button(
                            "Edit times",
                            f"p:de:{day}:{index}:times:{saved['revision']}",
                        ),
                    ],
                    [
                        button(
                            "Remove slot",
                            f"p:de:{day}:{index}:remove:{saved['revision']}",
                        )
                    ],
                    [button("Back · day", f"admin:day:{day}")],
                ],
            )
        elif parts[4] == "type":
            new_type = "private" if slot["session_type"] == "public" else "public"
            details["changes"] = {
                "session_type": new_type,
                "capacity": get_settings(admin_id)[new_type + "_capacity"],
            }
            await confirm(
                update,
                context,
                "draft_edit",
                details,
                f"Switch this slot to {new_type}?\nCapacity · {details['changes']['capacity']}",
                back,
            )
        elif parts[4] == "remove":
            details["changes"] = None
            await confirm(
                update, context, "draft_edit", details, "Remove this draft slot?", back
            )
        else:
            details["field"] = parts[4]
            details["slot"] = slot
            prompt = (
                "Send the new capacity."
                if parts[4] == "capacity"
                else "Send start and end times, such as 08:00 10:00."
            )
            await ask(update, context, "draft_edit", prompt, details, back)
    elif command == "manual":
        day = date.fromisoformat(parts[2])
        week = day - timedelta(days=(day.weekday() - 5) % 7)
        saved = load_week(admin_id, str(week))
        await ask(
            update,
            context,
            "manual",
            "Send start time, end time, and type.\nExample · 08:00 10:00 public\nUse private or public. Default capacity is applied.",
            {
                "day": str(day),
                "revision": saved["revision"],
                "published": saved["published"],
            },
            f"admin:day:{day}",
        )
    elif command == "customer":
        item = get_customer(int(parts[2]))
        if item is None:
            raise ValueError("Customer not found.")
        await render(
            update,
            f"Customer #{item['id']}\n{item['name'] or 'Unnamed'}\nPhone · {item['phone']}\nTelegram ID · {item['telegram_id'] or 'Not linked'}",
            [
                [button("Bookings", f"p:customerbookings:{item['id']}:0")],
                [
                    button("Edit name", f"p:customeredit:{item['id']}:name"),
                    button("Edit phone", f"p:customeredit:{item['id']}:phone"),
                ],
                [
                    button(
                        "Edit Telegram ID", f"p:customeredit:{item['id']}:telegram_id"
                    )
                ],
                [button("Back · Users", "admin:users")],
            ],
        )
    elif command == "customerbookings":
        cid = int(parts[2])
        await page_bookings(
            update,
            data.bookings_for(admin_id, customer_id=cid),
            "Customer bookings",
            f"p:customerbookings:{cid}",
            "p:abooking",
            int(parts[3]),
            f"p:customer:{cid}",
        )
    elif command == "customeredit":
        cid = int(parts[2])
        field = parts[3]
        if field not in ("name", "phone", "telegram_id"):
            raise ValueError("Unknown customer field.")
        await ask(
            update,
            context,
            "customer_edit",
            f"Send the new {field.replace('_', ' ')}.",
            {"id": cid, "field": field},
            f"p:customer:{cid}",
        )
    elif command == "assign":
        item = data.session(int(parts[2]), admin_id)
        await ask(
            update,
            context,
            "assign_search",
            "Search for the customer by name, phone, or Telegram ID.",
            {"session_id": item["id"], "version": item["version"]},
            f"p:aslot:{item['id']}",
        )
    elif command in ("pick", "newcustomer"):
        flow = context.user_data.get("phase_flow")
        if not flow or flow["kind"] != "assign_search":
            raise ValueError("Reopen Assign customer.")
        if command == "newcustomer":
            item = create_customer(flow["details"]["new_phone"])
            cid = item["id"]
        else:
            cid = int(parts[2])
        if get_customer(cid) is None:
            raise ValueError("Customer not found.")
        flow["details"]["customer_id"] = cid
        flow["kind"] = "assign_payment"
        await render(
            update,
            "Has this customer paid?",
            [
                [button("Paid", "p:assignpaid:1"), button("Unpaid", "p:assignpaid:0")],
                [button("Back", flow["back"])],
            ],
        )
    elif command == "assignpaid":
        flow = context.user_data.get("phase_flow")
        if not flow or flow["kind"] != "assign_payment":
            raise ValueError("Reopen Assign customer.")
        details = flow["details"]
        details["is_paid"] = parts[2] == "1"
        await ask(
            update,
            context,
            "assign_tracking",
            "Send a tracking code, or Skip.",
            details,
            flow["back"],
            True,
        )
    elif command == "config":
        settings = data.config(admin_id)
        rows = [
            [button(f"{label} · {settings[field] or 'Not set'}", f"p:cf:{field}")]
            for field, label in CONFIG_LABELS.items()
        ]
        rows.append([button("Back · Settings", "admin:settings")])
        await render(
            update,
            "Payment settings\n\nSet the card, contact, prices, and currency before opening customer bookings.\nPayment holds last 15 minutes.",
            rows,
        )
    elif command == "cf":
        field = parts[2]
        if field not in CONFIG_LABELS:
            raise ValueError("Unknown setting.")
        await ask(
            update,
            context,
            "config",
            f"Send the new {CONFIG_LABELS[field].lower()}.",
            {"field": field},
            "p:config",
        )
    elif command == "back":
        state = context.user_data.get("phase_flow") or context.user_data.get(
            "phase_confirm"
        )
        back = state["back"] if state else ("admin:home" if is_admin else "p:home")
        context.user_data.pop("phase_flow", None)
        context.user_data.pop("phase_confirm", None)
        if back.startswith("p:"):
            await route(update, context, admin_id, back)
        else:
            await admin_router(update, context, action_override=back, answered=True)
    elif command == "skip":
        flow = context.user_data.get("phase_flow")
        if not flow or flow["kind"] not in (
            "accept",
            "reject",
            "paid",
            "assign_tracking",
        ):
            raise ValueError("This prompt expired.")
        await finish_text(update, context, admin_id, "", flow)
    elif command == "confirm":
        state = context.user_data.get("phase_confirm")
        if not state or state["id"] != parts[2]:
            raise ValueError("This confirmation expired. Reopen the screen.")
        kind = state["kind"]
        details = state["details"]
        if kind != "cancelbooking" and not is_admin:
            raise ValueError("Admin access required.")
        if kind == "cancelbooking":
            item = data.cancel_booking(
                details["id"], update.effective_user.id, admin_id
            )
        elif kind == "session_edit":
            data.edit_session(
                details["id"], admin_id, details["version"], details["changes"]
            )
        elif kind == "draft_edit":
            data.edit_draft(
                admin_id,
                date.fromisoformat(details["day"]),
                details["index"],
                details["revision"],
                details["changes"],
            )
        elif kind == "manual":
            if details["published"]:
                data.add_session(
                    admin_id,
                    date.fromisoformat(details["day"]),
                    details["revision"],
                    details["slot"],
                )
            else:
                data.edit_draft(
                    admin_id,
                    date.fromisoformat(details["day"]),
                    0,
                    details["revision"],
                    new_session=details["slot"],
                )
        elif kind == "customer_edit":
            data.edit_customer(details["id"], details["field"], details["value"])
        elif kind == "config":
            data.set_config(admin_id, details["field"], details["value"])
        elif kind == "assign":
            item = data.assign(
                details["session_id"],
                details["customer_id"],
                admin_id,
                details["is_paid"],
                details["tracking"],
                expected_version=details["version"],
            )
        context.user_data.pop("phase_confirm", None)
        if kind in ("cancelbooking", "assign"):
            if is_admin:
                await admin_booking(update, context, item)
            else:
                await customer_booking(update, context, item, customer)
        else:
            back = (
                f"admin:day:{details['day']}"
                if kind in ("draft_edit", "manual")
                else state["back"]
            )
            await render(update, "Saved successfully.", [[button("Back", back)]])


async def handle_callback(update, context, admin_id):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if query.message is None or query.message.chat.type != "private":
        return
    try:
        await route(update, context, admin_id, query.data)
    except (ValueError, KeyError, IndexError) as error:
        await render(
            update,
            str(error) or "This screen expired. Reopen the menu.",
            [
                [
                    button(
                        "Back · menu",
                        "admin:home" if query.from_user.id == admin_id else "p:home",
                    )
                ]
            ],
        )


def parse_times(text, day):
    import re

    fields = phone_digits(text).split()
    if len(fields) != 2 or any(
        re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None
        for value in fields
    ):
        raise ValueError("Use two 24-hour times, such as 08:00 10:00.")
    return [
        datetime.combine(day, datetime.strptime(value, "%H:%M").time(), TZ).isoformat()
        for value in fields
    ]


async def finish_text(update, context, admin_id, text, flow):
    kind = flow["kind"]
    details = dict(flow["details"])
    back = flow["back"]
    if kind in ("accept", "reject", "paid"):
        maximum = 300 if kind == "reject" else 100
        if len(text) > maximum:
            raise ValueError(f"Use {maximum} characters or fewer.")
        if kind == "paid":
            data.mark_paid(details["id"], admin_id, text)
            item = data.booking(details["id"])
        else:
            item = data.decide(
                details["id"],
                admin_id,
                kind == "accept",
                note=text if kind == "reject" else "",
                tracking=text if kind == "accept" else "",
            )
        context.user_data.pop("phase_flow", None)
        await admin_booking(update, context, item)
        return
    if kind == "assign_tracking":
        if len(text) > 100:
            raise ValueError("Use a tracking code of up to 100 characters.")
        details["tracking"] = text
        item = get_customer(details["customer_id"])
        await confirm(
            update,
            context,
            "assign",
            details,
            slot_text(data.session(details["session_id"], admin_id))
            + f"\n\nReserve for {item['name'] or item['phone']}?\nPaid · {'Yes' if details['is_paid'] else 'No'}",
            back,
        )
        return
    if kind == "assign_search":
        if not 1 <= len(text) <= 150:
            raise ValueError("Use a search of 1 to 150 characters.")
        rows, total = search_customers(text)
        buttons = [
            [
                button(
                    f"{item['name'] or 'Unnamed'} · {item['phone']}",
                    f"p:pick:{item['id']}",
                )
            ]
            for item in rows
        ]
        if not rows:
            try:
                details["new_phone"] = normalize_phone(text)
                flow["details"] = details
                buttons.append(
                    [button("Create customer with this phone", "p:newcustomer")]
                )
            except ValueError:
                pass
        buttons.append([button("Back", back)])
        await render(
            update,
            f"Matches · {total}\nChoose a customer, or send a more specific search.",
            buttons,
        )
        return
    if kind == "config":
        if not 1 <= len(text) <= 100:
            raise ValueError("Enter a value of 1 to 100 characters.")
        details["value"] = text
        await confirm(
            update,
            context,
            kind,
            details,
            f"Save {CONFIG_LABELS[details['field']]}?\n{text}",
            back,
        )
        return
    if kind == "customer_edit":
        if not 1 <= len(text) <= 150:
            raise ValueError("Use 1 to 150 characters.")
        details["value"] = text
        await confirm(
            update,
            context,
            kind,
            details,
            f"Update customer #{details['id']} · {details['field']}\n{text}",
            back,
        )
        return
    if kind == "manual":
        fields = text.split()
        if len(fields) != 3 or fields[2].lower() not in ("private", "public"):
            raise ValueError("Example · 08:00 10:00 public. Use private or public.")
        start, end = parse_times(
            " ".join(fields[:2]), date.fromisoformat(details["day"])
        )
        new_type = fields[2].lower()
        details["slot"] = {
            "start": start,
            "end": end,
            "session_type": new_type,
            "capacity": get_settings(admin_id)[new_type + "_capacity"],
        }
        await confirm(
            update,
            context,
            kind,
            details,
            f"Add session {stamp(start)}–{datetime.fromisoformat(end):%H:%M}?\nType · {new_type}\nCapacity · {details['slot']['capacity']}",
            back,
        )
        return
    if kind in ("session_edit", "draft_edit"):
        field = details["field"]
        if field == "times":
            if kind == "session_edit":
                item = data.session(details["id"], admin_id)
                day = datetime.fromisoformat(item["starts_at"]).date()
            else:
                day = date.fromisoformat(details["day"])
            start, end = parse_times(text, day)
            details["changes"] = (
                {"starts_at": start, "ends_at": end}
                if kind == "session_edit"
                else {"start": start, "end": end}
            )
        else:
            digits = phone_digits(text).strip()
            minimum = 0 if field == "price" else 1
            if (
                not digits.isascii()
                or not digits.isdigit()
                or len(digits) > 12
                or int(digits) < minimum
            ):
                raise ValueError(
                    f"Use a whole number of at least {minimum}, up to 12 digits."
                )
            details["changes"] = {field: int(digits)}
        await confirm(
            update,
            context,
            kind,
            details,
            "Save this session change?\n"
            + "\n".join(
                f"{key.replace('_', ' ').title()} · {value}"
                for key, value in details["changes"].items()
            ),
            back,
        )


async def handle_text(update, context, admin_id):
    flow = context.user_data.get("phase_flow")
    if flow is None:
        return False
    if update.effective_user.id != admin_id:
        context.user_data.pop("phase_flow", None)
        return False
    try:
        await finish_text(update, context, admin_id, update.message.text.strip(), flow)
    except ValueError as error:
        await update.message.reply_text(str(error))
    return True


async def receipt_image(update, context, admin_id):
    customer = customer_by_telegram(update.effective_user.id)
    if customer is None:
        await update.message.reply_text(
            "Send /start and register before submitting a receipt."
        )
        return
    booking_id = context.user_data.get("receipt_booking_id")
    if booking_id is None:
        await update.message.reply_text(
            "Open My bookings and select the payment hold this receipt belongs to."
        )
        return
    message = update.message
    if message.photo:
        file = message.photo[-1]
        kind = "photo"
    else:
        file = message.document
        kind = "document"
    if file is None or (
        kind == "document" and not (file.mime_type or "").startswith("image/")
    ):
        await message.reply_text("Send a receipt photo or an image file.")
        return
    try:
        item = data.submit_receipt(
            booking_id, customer["id"], file.file_id, kind, file.file_unique_id
        )
    except ValueError as error:
        contact = data.config(admin_id)["admin_phone"]
        await message.reply_text(
            str(error) + (f"\nAdmin · {contact}" if contact else "")
        )
        return
    context.user_data.pop("receipt_booking_id", None)
    await message.reply_text(
        f"Receipt received for booking #{item['id']}. Your place is held while the admin reviews it.",
        reply_markup=markup([[button("My bookings", "p:mine:0")]]),
    )


async def maintenance(application):
    while True:
        try:
            await asyncio.to_thread(data.expire)
            events = await asyncio.to_thread(data.outbox_batch)
            for event in events:
                payload = json.loads(event["payload"])
                try:
                    if payload.get("kind") == "receipt":
                        keyboard = markup(
                            [
                                [
                                    button(
                                        "Accept", f"p:accept:{payload['booking_id']}"
                                    ),
                                    button(
                                        "Reject", f"p:reject:{payload['booking_id']}"
                                    ),
                                ]
                            ]
                        )
                        if payload["file_kind"] == "photo":
                            await application.bot.send_photo(
                                event["target_id"],
                                payload["file_id"],
                                caption=payload["text"][:1000],
                                reply_markup=keyboard,
                            )
                        else:
                            await application.bot.send_document(
                                event["target_id"],
                                payload["file_id"],
                                caption=payload["text"][:1000],
                                reply_markup=keyboard,
                            )
                    else:
                        await application.bot.send_message(
                            event["target_id"], payload["text"]
                        )
                except TelegramError:
                    LOGGER.exception(
                        "Notification delivery failed for event %s", event["id"]
                    )
                    await asyncio.to_thread(data.outbox_done, event["id"], False)
                else:
                    await asyncio.to_thread(data.outbox_done, event["id"], True)
        except Exception:
            LOGGER.exception("Booking maintenance failed; retrying shortly")
        await asyncio.sleep(15)


async def post_init(application):
    application.bot_data["phase_maintenance"] = asyncio.create_task(
        maintenance(application)
    )


async def post_shutdown(application):
    task = application.bot_data.get("phase_maintenance")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
