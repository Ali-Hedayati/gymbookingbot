"""Persistent phase-one booking rules. All capacity changes use SQLite transactions."""

import json
import sqlite3
import time
from datetime import datetime, timedelta

from storage import database, load_week, save_draft, normalize_phone, phone_digits

ACTIVE = ("held", "pending", "confirmed")
CONFIG_DEFAULTS = {
    "card_number": "",
    "admin_phone": "",
    "private_price": 0,
    "public_price": 0,
    "currency": "Toman",
}


def initialize():
    with database() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        for name, definition in (
            ("cancelled", "INTEGER NOT NULL DEFAULT 0"),
            ("version", "INTEGER NOT NULL DEFAULT 0"),
            ("price", "INTEGER"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS booking_config (
                admin_id INTEGER PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                status TEXT NOT NULL CHECK(status IN ('held','pending','confirmed','expired','rejected','cancelled')),
                created_at REAL NOT NULL, expires_at REAL,
                amount INTEGER NOT NULL, currency TEXT NOT NULL, card_number TEXT NOT NULL,
                is_paid INTEGER NOT NULL DEFAULT 0,
                receipt_file TEXT, receipt_kind TEXT, receipt_unique TEXT,
                tracking_code TEXT, note TEXT,
                decided_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking
                ON bookings(session_id, customer_id) WHERE status IN ('held','pending','confirmed');
            CREATE INDEX IF NOT EXISTS booking_session_status ON bookings(session_id,status);
            CREATE INDEX IF NOT EXISTS booking_customer ON bookings(customer_id,id);
            CREATE INDEX IF NOT EXISTS booking_expiry ON bookings(status,expires_at);
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, payload TEXT NOT NULL,
                event_key TEXT NOT NULL UNIQUE, delivered_at REAL,
                next_attempt REAL NOT NULL DEFAULT 0, claimed_until REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            );
        """)


def config(admin_id):
    with database() as db:
        return _config(db, admin_id)


def _config(db, admin_id):
    row = db.execute(
        "SELECT data FROM booking_config WHERE admin_id=?", (admin_id,)
    ).fetchone()
    return {**CONFIG_DEFAULTS, **(json.loads(row["data"]) if row else {})}


def set_config(admin_id, field, value):
    if field not in CONFIG_DEFAULTS:
        raise ValueError("Unknown setting.")
    if field == "card_number":
        value = "".join(phone_digits(str(value)).split()).replace("-", "")
        if not value.isascii() or not value.isdigit() or len(value) != 16:
            raise ValueError("Enter the 16 digits of the payment card.")
    elif field == "admin_phone":
        value = normalize_phone(str(value))
    elif field.endswith("_price"):
        text = phone_digits(str(value)).strip()
        if not text.isascii() or not text.isdigit() or len(text) > 12:
            raise ValueError(
                "Enter a nonnegative whole-number price of up to 12 digits."
            )
        value = int(text)
    elif field == "currency":
        value = str(value).strip()
        if not 1 <= len(value) <= 20:
            raise ValueError("Use a currency label of 1 to 20 characters.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        settings = _config(db, admin_id)
        settings[field] = value
        db.execute(
            "INSERT INTO booking_config VALUES (?,?) ON CONFLICT(admin_id) DO UPDATE SET data=excluded.data",
            (admin_id, json.dumps(settings)),
        )


def _notify(db, target, key, text, **extra):
    if target:
        db.execute(
            "INSERT OR IGNORE INTO notification_outbox(target_id,event_key,payload) VALUES (?,?,?)",
            (target, key, json.dumps({"text": text, **extra})),
        )


def _booking(db, booking_id):
    row = db.execute(
        """SELECT b.*, s.admin_id, s.starts_at, s.ends_at, s.session_type,
        s.capacity, s.cancelled AS session_cancelled, c.name, c.phone, c.telegram_id
        FROM bookings b JOIN sessions s ON s.id=b.session_id JOIN customers c ON c.id=b.customer_id
        WHERE b.id=?""",
        (booking_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Booking not found.")
    return dict(row)


def booking(booking_id):
    with database() as db:
        _expire(db, time.time())
        return _booking(db, booking_id)


def _expire(db, now):
    candidates = db.execute(
        "SELECT b.id,b.status,b.expires_at,s.starts_at FROM bookings b "
        "JOIN sessions s ON s.id=b.session_id WHERE b.status IN ('held','pending')"
    ).fetchall()
    for row in candidates:
        hold_expired = row["status"] == "held" and row["expires_at"] <= now
        session_started = datetime.fromisoformat(row["starts_at"]).timestamp() <= now
        if not hold_expired and not session_started:
            continue
        item = _booking(db, row["id"])
        if item["status"] not in ("held", "pending"):
            continue
        status = "expired" if item["status"] == "held" else "rejected"
        reason = (
            "Payment hold expired."
            if status == "expired"
            else "The session started before payment review was completed. Contact the admin about your payment."
        )
        changed = db.execute(
            "UPDATE bookings SET status=?, note=?, decided_at=? WHERE id=? AND status=?",
            (status, reason, now, item["id"], item["status"]),
        )
        if changed.rowcount:
            contact = _config(db, item["admin_id"])["admin_phone"]
            _notify(
                db,
                item["telegram_id"],
                f"expire:{item['id']}",
                f"Booking #{item['id']}: {reason}\nAdmin · {contact or 'Contact the gym'}",
            )


def expire(now=None):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, time.time() if now is None else now)


def _session(db, session_id, admin_id):
    row = db.execute(
        "SELECT * FROM sessions WHERE id=? AND admin_id=?", (session_id, admin_id)
    ).fetchone()
    if row is None:
        raise ValueError("Session not found.")
    item = dict(row)
    item["occupied"] = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE session_id=? AND status IN ('held','pending','confirmed')",
        (session_id,),
    ).fetchone()[0]
    item["available"] = max(0, item["capacity"] - item["occupied"])
    settings = _config(db, admin_id)
    item["amount"] = (
        item["price"]
        if item["price"] is not None
        else settings[f"{item['session_type']}_price"]
    )
    item["currency"] = settings["currency"]
    return item


def session(session_id, admin_id):
    with database() as db:
        _expire(db, time.time())
        return _session(db, session_id, admin_id)


def sessions(admin_id, day=None, future=True, include_cancelled=False):
    now = time.time()
    with database() as db:
        _expire(db, now)
        rows = db.execute(
            """SELECT s.*, COUNT(b.id) AS occupied FROM sessions s
            LEFT JOIN bookings b ON b.session_id=s.id AND b.status IN ('held','pending','confirmed')
            WHERE s.admin_id=? GROUP BY s.id ORDER BY s.starts_at""",
            (admin_id,),
        ).fetchall()
        settings = _config(db, admin_id)
        result = []
        for row in rows:
            item = dict(row)
            start = datetime.fromisoformat(item["starts_at"])
            if day is not None and start.date().isoformat() != str(day):
                continue
            if future and start.timestamp() <= now:
                continue
            if item["cancelled"] and not include_cancelled:
                continue
            item["available"] = max(0, item["capacity"] - item["occupied"])
            item["amount"] = (
                item["price"]
                if item["price"] is not None
                else settings[item["session_type"] + "_price"]
            )
            item["currency"] = settings["currency"]
            result.append(item)
        return result


def bookings_for(admin_id, customer_id=None, session_id=None, pending=False):
    with database() as db:
        _expire(db, time.time())
        condition = "s.admin_id=?"
        values = [admin_id]
        if customer_id is not None:
            condition += " AND b.customer_id=?"
            values.append(customer_id)
        if session_id is not None:
            condition += " AND b.session_id=?"
            values.append(session_id)
        if pending:
            condition += " AND b.status='pending'"
        rows = db.execute(
            "SELECT b.id FROM bookings b JOIN sessions s ON s.id=b.session_id WHERE "
            + condition
            + " ORDER BY b.id DESC",
            values,
        ).fetchall()
        return [_booking(db, row["id"]) for row in rows]


def hold(session_id, customer_id, admin_id, now=None, expected_version=None):
    now = time.time() if now is None else now
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, now)
        item = _session(db, session_id, admin_id)
        if expected_version is not None and item["version"] != expected_version:
            raise ValueError("This session changed. Reopen it before reserving.")
        if (
            item["cancelled"]
            or datetime.fromisoformat(item["starts_at"]).timestamp() <= now
        ):
            raise ValueError("This session is no longer available.")
        existing = db.execute(
            "SELECT id FROM bookings WHERE session_id=? AND customer_id=? AND status IN ('held','pending','confirmed')",
            (session_id, customer_id),
        ).fetchone()
        if existing:
            return _booking(db, existing["id"])
        if item["available"] <= 0:
            raise ValueError("The session is full. Please choose another session.")
        settings = _config(db, admin_id)
        if (
            not settings["card_number"]
            or not settings["admin_phone"]
            or item["amount"] <= 0
        ):
            raise ValueError(
                "Payment details are being configured. Please contact the gym admin."
            )
        deadline = min(
            now + 15 * 60, datetime.fromisoformat(item["starts_at"]).timestamp()
        )
        cursor = db.execute(
            """INSERT INTO bookings(session_id,customer_id,status,created_at,expires_at,amount,currency,card_number)
            VALUES (?,?,'held',?,?,?,?,?)""",
            (
                session_id,
                customer_id,
                now,
                deadline,
                item["amount"],
                settings["currency"],
                settings["card_number"],
            ),
        )
        return _booking(db, cursor.lastrowid)


def submit_receipt(booking_id, customer_id, file_id, file_kind, unique_id, now=None):
    now = time.time() if now is None else now
    if file_kind not in ("photo", "document"):
        raise ValueError("Send a payment receipt image.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, now)
        item = _booking(db, booking_id)
        if item["customer_id"] != customer_id:
            raise ValueError("This booking belongs to another customer.")
        if item["status"] == "pending":
            return item
        if item["status"] != "held" or item["expires_at"] <= now:
            raise ValueError(
                "This payment hold is no longer active. Contact the admin if you already paid."
            )
        db.execute(
            "UPDATE bookings SET status='pending',receipt_file=?,receipt_kind=?,receipt_unique=? WHERE id=?",
            (file_id, file_kind, unique_id, booking_id),
        )
        item = _booking(db, booking_id)
        caption = (
            f"Payment request #{booking_id}\nCustomer · {item['name'] or 'Unnamed'}\nPhone · {item['phone']}\n"
            f"Session · {item['starts_at']}\nType · {item['session_type']}\nAmount · {item['amount']} {item['currency']}"
        )
        _notify(
            db,
            item["admin_id"],
            f"receipt:{booking_id}",
            caption,
            kind="receipt",
            booking_id=booking_id,
            file_id=file_id,
            file_kind=file_kind,
        )
        return item


def decide(booking_id, admin_id, accept, note="", tracking="", now=None):
    now = time.time() if now is None else now
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, now)
        item = _booking(db, booking_id)
        if item["admin_id"] != admin_id:
            raise ValueError("Admin access required.")
        if item["status"] != "pending":
            return item
        status = "confirmed" if accept else "rejected"
        db.execute(
            "UPDATE bookings SET status=?,is_paid=?,note=?,tracking_code=?,decided_at=? WHERE id=?",
            (status, int(accept), note[:300], tracking[:100], now, booking_id),
        )
        contact = _config(db, admin_id)["admin_phone"]
        text = (
            f"Booking #{booking_id} {'confirmed' if accept else 'rejected'}.\nSession · {item['starts_at']}\n"
            f"{note[:300]}\nAdmin · {contact or 'Contact the gym'}"
        )
        _notify(db, item["telegram_id"], f"decision:{booking_id}", text)
        return _booking(db, booking_id)


def cancel_booking(booking_id, actor_id, admin_id, now=None):
    now = time.time() if now is None else now
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, now)
        item = _booking(db, booking_id)
        if item["admin_id"] != admin_id or (
            actor_id != admin_id and actor_id != item["telegram_id"]
        ):
            raise ValueError("Access denied.")
        if actor_id != admin_id and item["status"] != "held":
            raise ValueError(
                "Contact the admin to cancel a submitted or confirmed booking."
            )
        if item["status"] not in ACTIVE:
            return item
        db.execute(
            "UPDATE bookings SET status='cancelled',decided_at=? WHERE id=?",
            (now, booking_id),
        )
        _notify(
            db,
            item["telegram_id"],
            f"cancel:{booking_id}",
            f"Booking #{booking_id} was cancelled.\nSession · {item['starts_at']}\nContact the admin about any payment or refund.",
        )
        return _booking(db, booking_id)


def assign(
    session_id,
    customer_id,
    admin_id,
    is_paid,
    tracking="",
    now=None,
    expected_version=None,
):
    now = time.time() if now is None else now
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, now)
        item = _session(db, session_id, admin_id)
        if expected_version is not None and item["version"] != expected_version:
            raise ValueError("This session changed. Reopen it before reserving.")
        if (
            item["cancelled"]
            or datetime.fromisoformat(item["starts_at"]).timestamp() <= now
        ):
            raise ValueError("This session is no longer available.")
        existing = db.execute(
            "SELECT id FROM bookings WHERE session_id=? AND customer_id=? AND status IN ('held','pending','confirmed')",
            (session_id, customer_id),
        ).fetchone()
        if existing:
            return _booking(db, existing["id"])
        if item["available"] <= 0:
            raise ValueError("The session is full.")
        settings = _config(db, admin_id)
        cursor = db.execute(
            """INSERT INTO bookings(session_id,customer_id,status,created_at,amount,currency,card_number,is_paid,tracking_code,decided_at)
            VALUES (?,?,'confirmed',?,?,?,?,?,?,?)""",
            (
                session_id,
                customer_id,
                now,
                item["amount"],
                settings["currency"],
                settings["card_number"],
                int(is_paid),
                tracking[:100],
                now,
            ),
        )
        booking_id = cursor.lastrowid
        result = _booking(db, booking_id)
        _notify(
            db,
            result["telegram_id"],
            f"assign:{booking_id}",
            f"The gym admin reserved booking #{booking_id} for you.\nSession · {result['starts_at']}\nPaid · {'Yes' if is_paid else 'No'}",
        )
        return result


def mark_paid(booking_id, admin_id, tracking=""):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        item = _booking(db, booking_id)
        if item["admin_id"] != admin_id or item["status"] != "confirmed":
            raise ValueError("Select a confirmed booking.")
        db.execute(
            "UPDATE bookings SET is_paid=1,tracking_code=? WHERE id=?",
            (tracking[:100], booking_id),
        )
        if not item["is_paid"]:
            _notify(
                db,
                item["telegram_id"],
                f"paid:{booking_id}",
                f"Payment recorded for booking #{booking_id}.",
            )


def _sync_week(db, admin_id, week_start):
    rows = db.execute(
        "SELECT * FROM sessions WHERE admin_id=? AND week_start=? ORDER BY starts_at",
        (admin_id, week_start),
    ).fetchall()
    days = {}
    for row in rows:
        if row["cancelled"]:
            continue
        key = datetime.fromisoformat(row["starts_at"]).date().isoformat()
        settings = days.setdefault(
            key,
            {
                "session_type": row["session_type"],
                "capacity": row["capacity"],
                "sessions": [],
            },
        )
        settings["sessions"].append(
            {
                "start": row["starts_at"],
                "end": row["ends_at"],
                "session_type": row["session_type"],
                "capacity": row["capacity"],
            }
        )
    db.execute(
        "UPDATE weeks SET draft_json=?,revision=revision+1 WHERE admin_id=? AND week_start=?",
        (json.dumps({"start_date": week_start, "days": days}), admin_id, week_start),
    )


def _validate_times(start, end, week_start, require_future=True):
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if (
        first.tzinfo is None
        or last.tzinfo is None
        or last <= first
        or first.date() != last.date()
    ):
        raise ValueError(
            "Use a start and end on the same day, with end later than start."
        )
    if require_future and first.timestamp() <= time.time():
        raise ValueError("The session must start in the future.")
    week = datetime.fromisoformat(week_start).date()
    if not week <= first.date() < week + timedelta(days=7):
        raise ValueError("The session must stay in its week.")


def edit_session(session_id, admin_id, version, changes):
    allowed = {"session_type", "capacity", "starts_at", "ends_at", "cancelled", "price"}
    if not changes or set(changes) - allowed:
        raise ValueError("Invalid session change.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, time.time())
        original = _session(db, session_id, admin_id)
        if original["version"] != version:
            raise ValueError("This session changed. Reopen it before editing.")
        if datetime.fromisoformat(original["starts_at"]).timestamp() <= time.time():
            raise ValueError("Past or started sessions cannot be edited.")
        candidate = {**original, **changes}
        if (
            candidate["session_type"] not in ("private", "public")
            or not isinstance(candidate["capacity"], int)
            or candidate["capacity"] < 1
        ):
            raise ValueError("Use a valid type and positive capacity.")
        if candidate["price"] is not None and (
            not isinstance(candidate["price"], int) or candidate["price"] < 0
        ):
            raise ValueError("Use a nonnegative whole-number price.")
        if candidate["capacity"] < original["occupied"]:
            raise ValueError(
                "Capacity cannot be lower than the number of held, submitted, or confirmed places."
            )
        significant = any(
            candidate[key] != original[key]
            for key in ("starts_at", "ends_at", "session_type", "price")
        )
        if original["occupied"] and significant and not candidate["cancelled"]:
            raise ValueError(
                "This session has active bookings. Cancel those bookings before changing its time, type, or price."
            )
        _validate_times(
            candidate["starts_at"], candidate["ends_at"], candidate["week_start"]
        )
        if not candidate["cancelled"]:
            others = db.execute(
                "SELECT starts_at,ends_at FROM sessions WHERE admin_id=? AND week_start=? AND id<>? AND cancelled=0",
                (admin_id, candidate["week_start"], session_id),
            ).fetchall()
            start, end = (
                datetime.fromisoformat(candidate["starts_at"]),
                datetime.fromisoformat(candidate["ends_at"]),
            )
            if any(
                start < datetime.fromisoformat(row["ends_at"])
                and end > datetime.fromisoformat(row["starts_at"])
                for row in others
            ):
                raise ValueError("This change would overlap another session.")
        duplicate = db.execute(
            "SELECT id FROM sessions WHERE admin_id=? AND starts_at=? AND id<>?",
            (admin_id, candidate["starts_at"], session_id),
        ).fetchone()
        if duplicate:
            raise ValueError(
                "Another session already uses this start time, including cancelled sessions."
            )
        db.execute(
            """UPDATE sessions SET session_type=?,capacity=?,starts_at=?,ends_at=?,cancelled=?,price=?,version=version+1
            WHERE id=?""",
            (
                candidate["session_type"],
                candidate["capacity"],
                candidate["starts_at"],
                candidate["ends_at"],
                candidate["cancelled"],
                candidate["price"],
                session_id,
            ),
        )
        if candidate["cancelled"]:
            for row in db.execute(
                "SELECT id FROM bookings WHERE session_id=? AND status IN ('held','pending','confirmed')",
                (session_id,),
            ).fetchall():
                booking_item = _booking(db, row["id"])
                db.execute(
                    "UPDATE bookings SET status='cancelled',decided_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
                _notify(
                    db,
                    booking_item["telegram_id"],
                    f"session_cancel:{row['id']}",
                    f"Session cancelled by the gym. Booking #{row['id']}.\nContact the admin about any payment or refund.",
                )
        _sync_week(db, admin_id, candidate["week_start"])


def edit_draft(admin_id, day, index, revision, changes=None, new_session=None):
    week = day - timedelta(days=(day.weekday() - 5) % 7)
    draft = load_week(admin_id, str(week))
    if draft["published"] or draft["revision"] != revision:
        raise ValueError("This week changed or was published. Reopen the day.")
    settings = draft["days"].setdefault(str(day), {"sessions": []})
    slots = settings.setdefault("sessions", [])
    if new_session is not None:
        _validate_times(new_session["start"], new_session["end"], str(week))
        slots.append(new_session)
    else:
        if not 0 <= index < len(slots):
            raise ValueError("This session no longer exists.")
        if changes is None:
            slots.pop(index)
        else:
            candidate = {**slots[index], **changes}
            _validate_times(candidate["start"], candidate["end"], str(week))
            slots[index] = candidate
    slots.sort(key=lambda item: item["start"])
    previous_end = None
    for slot in slots:
        _validate_times(slot["start"], slot["end"], str(week), require_future=False)
        start, end = (
            datetime.fromisoformat(slot["start"]),
            datetime.fromisoformat(slot["end"]),
        )
        if start.date() != day:
            raise ValueError("The session must stay on the selected day.")
        if previous_end is not None and start < previous_end:
            raise ValueError("These sessions overlap.")
        if (
            slot["session_type"] not in ("private", "public")
            or not isinstance(slot["capacity"], int)
            or slot["capacity"] < 1
        ):
            raise ValueError("Use a valid type and positive capacity.")
        previous_end = end
    save_draft(admin_id, draft)


def edit_customer(customer_id, field, value):
    if field not in ("name", "phone", "telegram_id"):
        raise ValueError("Invalid customer field.")
    if field == "phone":
        value = normalize_phone(value)
    elif field == "name":
        value = value.strip()
        if not 1 <= len(value) <= 150:
            raise ValueError("Use a name of 1 to 150 characters.")
    else:
        text = phone_digits(value).strip()
        if (
            not text.isascii()
            or not text.isdigit()
            or len(text) > 19
            or not 1 <= int(text) <= 9223372036854775807
        ):
            raise ValueError("Use a positive numeric Telegram ID.")
        value = int(text)
    try:
        with database() as db:
            if field == "name":
                db.execute(
                    "UPDATE customers SET name=?,name_search=? WHERE id=?",
                    (value, value.casefold(), customer_id),
                )
            else:
                db.execute(
                    f"UPDATE customers SET {field}=? WHERE id=?", (value, customer_id)
                )
    except sqlite3.IntegrityError as error:
        raise ValueError(
            "That phone or Telegram ID already belongs to another customer."
        ) from error


def outbox_batch(now=None):
    now = time.time() if now is None else now
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT * FROM notification_outbox WHERE delivered_at IS NULL AND next_attempt<=? AND claimed_until<=? ORDER BY id LIMIT 10",
            (now, now),
        ).fetchall()
        for row in rows:
            db.execute(
                "UPDATE notification_outbox SET claimed_until=? WHERE id=?",
                (now + 120, row["id"]),
            )
    return [dict(row) for row in rows]


def outbox_done(event_id, success):
    with database() as db:
        if success:
            db.execute(
                "UPDATE notification_outbox SET delivered_at=?,claimed_until=0 WHERE id=?",
                (time.time(), event_id),
            )
        else:
            db.execute(
                "UPDATE notification_outbox SET attempts=attempts+1,next_attempt=?,claimed_until=0 WHERE id=?",
                (time.time() + 60, event_id),
            )


def add_session(admin_id, day, revision, slot):
    week = day - timedelta(days=(day.weekday() - 5) % 7)
    _validate_times(slot["start"], slot["end"], str(week))
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM weeks WHERE admin_id=? AND week_start=?",
            (admin_id, str(week)),
        ).fetchone()
        if row is None or not row["published"] or row["revision"] != revision:
            raise ValueError("This week changed. Reopen the day.")
        start, end = (
            datetime.fromisoformat(slot["start"]),
            datetime.fromisoformat(slot["end"]),
        )
        others = db.execute(
            "SELECT starts_at,ends_at FROM sessions WHERE admin_id=? AND week_start=? AND cancelled=0",
            (admin_id, str(week)),
        ).fetchall()
        if any(
            start < datetime.fromisoformat(item["ends_at"])
            and end > datetime.fromisoformat(item["starts_at"])
            for item in others
        ):
            raise ValueError("This slot overlaps another session.")
        if db.execute(
            "SELECT id FROM sessions WHERE admin_id=? AND starts_at=?",
            (admin_id, slot["start"]),
        ).fetchone():
            raise ValueError(
                "A session already uses this start time, including cancelled sessions. Choose another time."
            )
        db.execute(
            "INSERT INTO sessions(admin_id,week_start,starts_at,ends_at,session_type,capacity) VALUES (?,?,?,?,?,?)",
            (
                admin_id,
                str(week),
                slot["start"],
                slot["end"],
                slot["session_type"],
                slot["capacity"],
            ),
        )
        _sync_week(db, admin_id, str(week))
