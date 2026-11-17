import json
import sqlite3
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "gym_bot.sqlite3"


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database() -> None:
    with database() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS gym_settings (
                admin_id INTEGER PRIMARY KEY,
                private_capacity INTEGER NOT NULL DEFAULT 2 CHECK (private_capacity > 0),
                public_capacity INTEGER NOT NULL DEFAULT 6 CHECK (public_capacity > 0),
                duration_minutes INTEGER NOT NULL DEFAULT 120 CHECK (duration_minutes BETWEEN 1 AND 1439)
            );
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL UNIQUE,
                name TEXT,
                name_search TEXT NOT NULL DEFAULT '',
                telegram_id INTEGER UNIQUE,
                username TEXT
            );
            CREATE TABLE IF NOT EXISTS weeks (
                admin_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                draft_json TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1)),
                PRIMARY KEY (admin_id, week_start)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                session_type TEXT NOT NULL CHECK (session_type IN ('private', 'public')),
                capacity INTEGER NOT NULL CHECK (capacity > 0),
                UNIQUE (admin_id, starts_at),
                FOREIGN KEY (admin_id, week_start)
                    REFERENCES weeks (admin_id, week_start)
            );
        """)


def get_settings(admin_id: int) -> dict:
    with database() as connection:
        row = connection.execute(
            "SELECT private_capacity, public_capacity, duration_minutes FROM gym_settings WHERE admin_id = ?",
            (admin_id,),
        ).fetchone()
    if row is None:
        return {"private_capacity": 2, "public_capacity": 6, "duration_minutes": 120}
    return dict(row)


def update_setting(admin_id: int, field: str, value: int) -> None:
    fields = {"private_capacity", "public_capacity", "duration_minutes"}
    maximum = 1439 if field == "duration_minutes" else 2147483647
    if field not in fields or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError("The setting value is outside its allowed range.")
    with database() as connection:
        connection.execute("INSERT OR IGNORE INTO gym_settings (admin_id) VALUES (?)", (admin_id,))
        # The column name is validated against the fixed field set above.
        connection.execute(f"UPDATE gym_settings SET {field} = ? WHERE admin_id = ?", (value, admin_id))


def phone_digits(value: str) -> str:
    return value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def normalize_phone(value: str) -> str:
    value = re.sub(r"[\s()\-]", "", phone_digits(value))
    if value.startswith("00"):
        value = "+" + value[2:]
    if value.startswith("09") and len(value) == 11:
        value = "+98" + value[1:]
    if not value.startswith("+"):
        value = "+" + value
    if re.fullmatch(r"\+[1-9][0-9]{6,14}", value) is None:
        raise ValueError("Enter an international phone number, such as +989123456789, or an Iranian mobile number starting with 09.")
    return value


def get_customer(customer_id: int) -> dict | None:
    with database() as connection:
        row = connection.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return dict(row) if row else None


def customer_by_telegram(telegram_id: int) -> dict | None:
    with database() as connection:
        row = connection.execute("SELECT * FROM customers WHERE telegram_id = ?", (telegram_id,)).fetchone()
    return dict(row) if row else None


def customer_by_phone(phone: str) -> dict | None:
    with database() as connection:
        row = connection.execute("SELECT * FROM customers WHERE phone = ?", (normalize_phone(phone),)).fetchone()
    return dict(row) if row else None


def search_customers(query: str = "", page: int = 0) -> tuple[list[dict], int]:
    name_term = query.strip().casefold()
    number_term = re.sub(r"[\s()+\-]", "", phone_digits(query.strip()))
    if number_term.startswith("09"):
        number_term = "98" + number_term[1:]
    if number_term.startswith("0098"):
        number_term = number_term[2:]
    condition = (
        "(? = '' OR instr(name_search, ?) > 0 OR instr(phone, ?) > 0 "
        "OR instr(COALESCE(CAST(telegram_id AS TEXT), ''), ?) > 0)"
    )
    # A nonnumeric name query must not turn into an empty phone search.
    number_term = number_term or name_term
    parameters = (name_term, name_term, number_term, number_term)
    with database() as connection:
        total = connection.execute("SELECT COUNT(*) FROM customers WHERE " + condition, parameters).fetchone()[0]
        rows = connection.execute(
            "SELECT * FROM customers WHERE " + condition + " ORDER BY id DESC LIMIT 8 OFFSET ?",
            (*parameters, max(0, page) * 8),
        ).fetchall()
    return [dict(row) for row in rows], total


def create_customer(phone: str, name: str | None = None, telegram_id: int | None = None) -> dict:
    phone = normalize_phone(phone)
    if telegram_id is not None and not 1 <= telegram_id <= 9223372036854775807:
        raise ValueError("Telegram ID must be a positive numeric ID.")
    name = name.strip() if name else None
    if name and len(name) > 150:
        raise ValueError("Use a name of 150 characters or fewer.")
    try:
        with database() as connection:
            cursor = connection.execute(
                "INSERT INTO customers (phone, name, name_search, telegram_id) VALUES (?, ?, ?, ?)",
                (phone, name, (name or "").casefold(), telegram_id),
            )
            customer_id = cursor.lastrowid
    except sqlite3.IntegrityError as error:
        raise ValueError("That phone number or Telegram ID already belongs to a customer. Search for the existing record.") from error
    return get_customer(customer_id)


def register_customer(phone: str, telegram_id: int, name: str, username: str | None) -> dict:
    phone = normalize_phone(phone)
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        by_id = connection.execute("SELECT * FROM customers WHERE telegram_id = ?", (telegram_id,)).fetchone()
        by_phone = connection.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
        if by_id and by_id["phone"] != phone:
            raise ValueError("Your Telegram account is linked to a different phone number. Please contact the gym admin.")
        if by_phone and by_phone["telegram_id"] not in (None, telegram_id):
            raise ValueError("This phone number is linked to another Telegram account. Please contact the gym admin.")
        if by_phone:
            customer_id = by_phone["id"]
            saved_name = by_phone["name"] or name[:150]
            connection.execute(
                "UPDATE customers SET telegram_id = ?, name = ?, name_search = ?, username = ? WHERE id = ?",
                (telegram_id, saved_name, saved_name.casefold(), username, customer_id),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO customers (phone, name, name_search, telegram_id, username) VALUES (?, ?, ?, ?, ?)",
                (phone, name[:150], name[:150].casefold(), telegram_id, username),
            )
            customer_id = cursor.lastrowid
    return get_customer(customer_id)


def load_week(admin_id: int, week_start: str) -> dict:
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM weeks WHERE admin_id = ? AND week_start = ?",
            (admin_id, week_start),
        ).fetchone()
    if row is None:
        return {"start_date": week_start, "days": {}, "revision": 0, "published": False}
    draft = json.loads(row["draft_json"])
    draft.update(revision=row["revision"], published=bool(row["published"]))
    return draft


def save_draft(admin_id: int, draft: dict) -> int:
    # Only confirmed sessions enter the saved draft. Editing defaults is temporary.
    days = {}
    for day, settings in draft["days"].items():
        sessions = settings.get("sessions", [])
        if sessions:
            days[day] = {
                "session_type": sessions[0]["session_type"],
                "capacity": sessions[0]["capacity"],
                "sessions": sessions,
            }
    payload = json.dumps({"start_date": draft["start_date"], "days": days})
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO weeks (admin_id, week_start, draft_json) VALUES (?, ?, ?)",
            (admin_id, draft["start_date"], json.dumps({"start_date": draft["start_date"], "days": {}})),
        )
        result = connection.execute(
            "UPDATE weeks SET draft_json = ?, revision = revision + 1 "
            "WHERE admin_id = ? AND week_start = ? AND revision = ? AND published = 0",
            (payload, admin_id, draft["start_date"], draft.get("revision", 0)),
        )
        if result.rowcount != 1:
            raise ValueError("This week changed or was published. Reopen it through /admin before editing.")
    return draft.get("revision", 0) + 1


def upcoming_sessions(draft: dict, now: datetime) -> list[dict]:
    sessions = [
        session
        for settings in draft["days"].values()
        for session in settings.get("sessions", [])
        if datetime.fromisoformat(session["start"]) > now
    ]
    return sorted(sessions, key=lambda session: session["start"])


def publish_week(admin_id: int, week_start: str, revision: int, expected_count: int, now: datetime) -> int:
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM weeks WHERE admin_id = ? AND week_start = ?",
            (admin_id, week_start),
        ).fetchone()
        if row is None:
            raise ValueError("Confirm at least one day before publishing.")
        if row["published"]:
            return connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE admin_id = ? AND week_start = ?",
                (admin_id, week_start),
            ).fetchone()[0]
        if row["revision"] != revision:
            raise ValueError("The saved draft changed. Review Publish week again.")

        draft = json.loads(row["draft_json"])
        sessions = upcoming_sessions(draft, now)
        if not sessions or len(sessions) != expected_count:
            raise ValueError("Available times changed. Review Publish week again.")

        previous_end = None
        week_date = datetime.fromisoformat(week_start).date()
        for session in sessions:
            start = datetime.fromisoformat(session["start"])
            end = datetime.fromisoformat(session["end"])
            if not week_date <= start.date() < week_date + timedelta(days=7):
                raise ValueError("A session falls outside this week.")
            if end <= start or (previous_end is not None and start < previous_end):
                raise ValueError("Sessions overlap or have invalid times.")
            connection.execute(
                "INSERT INTO sessions (admin_id, week_start, starts_at, ends_at, session_type, capacity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (admin_id, week_start, session["start"], session["end"],
                 session["session_type"], session["capacity"]),
            )
            previous_end = end
        published_days = {}
        for session in sessions:
            day = datetime.fromisoformat(session["start"]).date().isoformat()
            settings = published_days.setdefault(day, {
                "session_type": session["session_type"],
                "capacity": session["capacity"],
                "sessions": [],
            })
            settings["sessions"].append(session)
        payload = json.dumps({"start_date": week_start, "days": published_days})
        connection.execute(
            "UPDATE weeks SET published = 1, draft_json = ? WHERE admin_id = ? AND week_start = ?",
            (payload, admin_id, week_start),
        )
    return len(sessions)
