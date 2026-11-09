import os
import copy
import sqlite3
import re
from uuid import uuid4
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


from storage import (
    get_settings,
    initialize_database,
    load_week,
    publish_week,
    save_draft,
    upcoming_sessions,
    update_setting,
)


ADMIN_ID = 7265018265
GYM_TIMEZONE = ZoneInfo("Asia/Tehran")
SETTING_LABELS = {
    "private_capacity": "Private session capacity",
    "public_capacity": "Public session capacity",
    "duration_minutes": "Default session duration in minutes",
}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is not None:
        await update.message.reply_text("Welcome to the gym booking bot! 🏋️")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is not None and update.effective_user is not None:
        await update.message.reply_text(
            f"Your Telegram user ID is {update.effective_user.id}"
        )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "You do not have access to the admin panel."
        )
        return

    context.user_data.pop("awaiting_times", None)
    context.user_data.pop("session_preview", None)
    context.user_data.pop("setting_edit", None)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Create / open week", callback_data="admin:create_week")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="admin:settings")],
        ]
    )
    await update.message.reply_text(
        "Welcome to the gym admin panel! 🏋️", reply_markup=keyboard
    )


def day_keyboard(week_start: date) -> InlineKeyboardMarkup:
    today = datetime.now(GYM_TIMEZONE).date()
    keyboard = []

    for offset in range(7):
        day_date = week_start + timedelta(days=offset)
        if day_date < today:
            continue
        keyboard.append(
            [InlineKeyboardButton(
                f"{day_date:%A} · {day_date:%Y-%m-%d}",
                callback_data=f"admin:day:{day_date.isoformat()}",
            )]
        )

    keyboard.append(
        [InlineKeyboardButton(
            "Publish week", callback_data=f"admin:publish:{week_start}"
        )]
    )
    keyboard.append(
        [InlineKeyboardButton(
            "Back · weeks", callback_data="admin:create_week"
        )]
    )
    return InlineKeyboardMarkup(keyboard)


def type_keyboard(selected_day: date) -> InlineKeyboardMarkup:
    defaults = get_settings(ADMIN_ID)
    week_start = selected_day - timedelta(days=(selected_day.weekday() - 5) % 7)
    saved = load_week(ADMIN_ID, str(week_start))
    has_sessions = bool(saved["days"].get(str(selected_day), {}).get("sessions"))
    back = f"admin:day:{selected_day}" if has_sessions else "admin:days"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔒 Private · {defaults['private_capacity']} people",
            callback_data=f"admin:type:{selected_day}:private",
        )],
        [InlineKeyboardButton(
            f"👥 Public · {defaults['public_capacity']} people",
            callback_data=f"admin:type:{selected_day}:public",
        )],
        [InlineKeyboardButton("Back", callback_data=back)],
    ])


def day_setup_keyboard(selected_day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Generate sessions", callback_data=f"admin:generate:{selected_day}")],
        [InlineKeyboardButton("Back · session type", callback_data=f"admin:types:{selected_day}")],
    ])


def day_summary(selected_day: date, day_settings: dict) -> str:
    return (
        f"Day · {selected_day:%A, %Y-%m-%d}\n"
        f"Session type · {day_settings['session_type'].title()}\n"
        f"Capacity per session · {day_settings['capacity']} people\n\n"
        "Continue to enter the day's hours. Default capacities can be changed in Settings."
    )



async def show_settings(query, context) -> None:
    context.user_data.pop("setting_edit", None)
    defaults = get_settings(ADMIN_ID)
    keyboard = [
        [InlineKeyboardButton(
            f"{label} · {defaults[field]}", callback_data=f"admin:setting:{field}"
        )]
        for field, label in SETTING_LABELS.items()
    ]
    keyboard.append([InlineKeyboardButton("Back · admin panel", callback_data="admin:home")])
    await query.edit_message_text(
        "Settings\n\nChoose a default to change.\n"
        "Changes apply when you choose a type for newly generated sessions.\n"
        "Saved sessions keep their existing capacity and times.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_setting_input(query, context, field: str) -> None:
    context.user_data["setting_edit"] = {"field": field, "stage": "input"}
    maximum = 1439 if field == "duration_minutes" else 2147483647
    await query.edit_message_text(
        f"{SETTING_LABELS[field]}\n\n"
        f"Send a whole number between 1 and {maximum}.\n"
        "You will review it before saving.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back · Settings", callback_data="admin:settings")]
        ]),
    )


async def show_time_input(query, context, selected_day: date, settings: dict) -> None:
    context.user_data["awaiting_times"] = str(selected_day)
    duration = settings.get("duration_minutes", get_settings(ADMIN_ID)["duration_minutes"])
    await query.edit_message_text(
        f"Generate sessions for {selected_day:%A, %Y-%m-%d}.\n\n"
        "Send opening and closing times using the 24-hour clock.\n"
        f"Example · 08:00 20:00\nDefault duration · {duration} minutes\n\n"
        "Or add a number of equal sessions.\nExample · 08:00 20:00 6\n\n"
        "End time must be later on the same day.\n"
        "Confirming replaces this day's draft time slots.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back · day setup", callback_data=f"admin:setup:{selected_day}")]
        ]),
    )


async def show_saved_day(query, selected_day: date, saved: dict, page: int = 0) -> None:
    sessions = saved["days"].get(str(selected_day), {}).get("sessions", [])
    page_count = max(1, (len(sessions) + 19) // 20)
    page = max(0, min(page, page_count - 1))
    lines = [
        f"Saved sessions · {selected_day:%A, %Y-%m-%d}",
        f"Status · {'Published' if saved['published'] else 'Draft'}",
        f"Total sessions · {len(sessions)}",
        "",
    ]
    for session in sessions[page * 20:(page + 1) * 20]:
        start = datetime.fromisoformat(session["start"])
        end = datetime.fromisoformat(session["end"])
        lines.append(
            f"{start:%H:%M}–{end:%H:%M} · {session['session_type'].title()} · "
            f"{session['capacity']} people"
        )
    keyboard = []
    if page_count > 1:
        lines.append(f"\nPage {page + 1} of {page_count}")
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton("Previous", callback_data=f"admin:view:{selected_day}:{page - 1}"))
        if page + 1 < page_count:
            navigation.append(InlineKeyboardButton("Next", callback_data=f"admin:view:{selected_day}:{page + 1}"))
        keyboard.append(navigation)
    if not saved["published"]:
        keyboard.append([InlineKeyboardButton(
            "Regenerate this day", callback_data=f"admin:types:{selected_day}"
        )])
    keyboard.append([InlineKeyboardButton(
        "Back · week", callback_data=f"admin:week:{saved['start_date']}"
    )])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_buttons(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer("Admin access required.", show_alert=True)
        return

    await query.answer()
    action = query.data or ""
    today = datetime.now(GYM_TIMEZONE).date()

    if action == "admin:home":
        context.user_data.pop("awaiting_times", None)
        context.user_data.pop("session_preview", None)
        context.user_data.pop("setting_edit", None)
        await query.edit_message_text(
            "Gym admin panel",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Create / open week", callback_data="admin:create_week")],
                [InlineKeyboardButton("⚙️ Settings", callback_data="admin:settings")],
            ]),
        )
        return

    if action == "admin:settings" or action.startswith("admin:setting"):
        context.user_data.pop("awaiting_times", None)
        context.user_data.pop("session_preview", None)
        if action == "admin:settings":
            await show_settings(query, context)
        elif action.startswith("admin:setting:"):
            field = action.removeprefix("admin:setting:")
            if field in SETTING_LABELS:
                await show_setting_input(query, context, field)
        elif action.startswith("admin:setting_back:"):
            field = action.removeprefix("admin:setting_back:")
            if field in SETTING_LABELS:
                await show_setting_input(query, context, field)
        elif action.startswith("admin:setting_confirm:"):
            pending = context.user_data.get("setting_edit")
            if pending is None or pending.get("id") != action.split(":")[-1]:
                await show_settings(query, context)
                return
            update_setting(ADMIN_ID, pending["field"], pending["value"])
            await show_settings(query, context)
        return

    context.user_data.pop("setting_edit", None)

    if action.startswith("admin:confirm:") or action.startswith("admin:back:"):
        preview = context.user_data.get("session_preview")
        if preview is None or action.split(":")[-1] != preview["id"]:
            await query.edit_message_text(
                "This preview has expired. Send /admin to continue."
            )
            return

        draft = context.user_data.get("week_draft")
        if draft is None or draft["start_date"] != preview["week_start"]:
            await query.edit_message_text("Your draft has expired. Send /admin again.")
            return

        selected_day = date.fromisoformat(preview["day"])
        day_settings = draft["days"][preview["day"]]
        if action.startswith("admin:confirm:"):
            first_start = datetime.fromisoformat(preview["sessions"][0]["start"])
            if first_start <= datetime.now(GYM_TIMEZONE):
                context.user_data.pop("session_preview", None)
                await query.edit_message_text(
                    "The first session has already started. Generate new times.",
                    reply_markup=day_setup_keyboard(selected_day),
                )
                return
            candidate = copy.deepcopy(draft)
            candidate["days"][preview["day"]]["sessions"] = preview["sessions"]
            try:
                candidate["revision"] = save_draft(ADMIN_ID, candidate)
            except ValueError as error:
                await query.edit_message_text(str(error))
                return
            except sqlite3.Error:
                await query.edit_message_text(
                    "Could not save the database. Check disk space and folder permissions, then reopen /admin."
                )
                return
            context.user_data["week_draft"] = candidate
        else:
            context.user_data.pop("session_preview", None)
            await show_time_input(query, context, selected_day, day_settings)
            return

        context.user_data.pop("session_preview", None)
        await show_saved_day(query, selected_day, load_week(ADMIN_ID, draft["start_date"]))
        return

    context.user_data.pop("awaiting_times", None)
    context.user_data.pop("session_preview", None)

    if action.startswith("admin:publish:") or action.startswith("admin:publish_ok:"):
        parts = action.split(":")
        week_start = parts[2]
        saved = load_week(ADMIN_ID, week_start)
        if saved["published"]:
            await query.edit_message_text(
                "This week is already published.",
                reply_markup=day_keyboard(date.fromisoformat(week_start)),
            )
            return
        sessions = upcoming_sessions(saved, datetime.now(GYM_TIMEZONE))
        if not sessions:
            await query.edit_message_text(
                "Confirm at least one day with future sessions before publishing.",
                reply_markup=day_keyboard(date.fromisoformat(week_start)),
            )
            return
        if parts[1] == "publish":
            total = sum(len(day.get("sessions", [])) for day in saved["days"].values())
            await query.edit_message_text(
                f"Publish week starting {week_start}?\n"
                f"Upcoming sessions · {len(sessions)}\n"
                f"Past or started sessions skipped · {total - len(sessions)}\n\n"
                "This publishes the confirmed, saved sessions.\n"
                "Published weeks are read-only in this version.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "Yes, publish week",
                        callback_data=f"admin:publish_ok:{week_start}:{saved['revision']}:{len(sessions)}",
                    )],
                    [InlineKeyboardButton("Back", callback_data=f"admin:week:{week_start}")],
                ]),
            )
            return
        try:
            count = publish_week(
                ADMIN_ID, week_start, int(parts[3]), int(parts[4]), datetime.now(GYM_TIMEZONE)
            )
        except ValueError as error:
            await query.edit_message_text(
                str(error), reply_markup=day_keyboard(date.fromisoformat(week_start))
            )
            return
        except sqlite3.Error:
            await query.edit_message_text("Publishing failed. Nothing was published. Reopen /admin to retry.")
            return
        context.user_data["week_draft"] = load_week(ADMIN_ID, week_start)
        await query.edit_message_text(
            f"Published {count} sessions.\nYou can reopen the week to view them.",
            reply_markup=day_keyboard(date.fromisoformat(week_start)),
        )
        return

    if action == "admin:create_week":
        days_since_saturday = (today.weekday() - 5) % 7
        this_saturday = today - timedelta(days=days_since_saturday)
        next_saturday = this_saturday + timedelta(days=7)
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(
                    f"This week · {this_saturday:%Y-%m-%d}",
                    callback_data=f"admin:week:{this_saturday}",
                )],
                [InlineKeyboardButton(
                    f"Next week · {next_saturday:%Y-%m-%d}",
                    callback_data=f"admin:week:{next_saturday}",
                )],
                [InlineKeyboardButton("Back · admin panel", callback_data="admin:home")],
            ]
        )
        await query.edit_message_text(
            "Choose the week you want to prepare.\n"
            "Dates are shown as year-month-day.",
            reply_markup=keyboard,
        )
        return

    if action.startswith("admin:week:"):
        week_start = date.fromisoformat(action.removeprefix("admin:week:"))
        week_end = week_start + timedelta(days=6)
        if week_end < today:
            await query.edit_message_text(
                "That week has already passed. Choose another week.",
                reply_markup=day_keyboard(week_start),
            )
            return

        saved = load_week(ADMIN_ID, week_start.isoformat())
        context.user_data["week_draft"] = saved
        saved_count = sum(len(day.get("sessions", [])) for day in saved["days"].values())
        status = "Published" if saved["published"] else "Draft"
        await query.edit_message_text(
            f"Week of {week_start:%Y-%m-%d} to {week_end:%Y-%m-%d}\n\n"
            f"Status · {status}\nSaved sessions · {saved_count}\n\n"
            "Choose a day to view or configure.",
            reply_markup=day_keyboard(week_start),
        )
        return

    draft = context.user_data.get("week_draft")
    if draft is None:
        await query.edit_message_text(
            "Your draft has expired. Send /admin to choose a week again."
        )
        return

    week_start = date.fromisoformat(draft["start_date"])
    if action == "admin:days":
        await query.edit_message_text(
            "Choose a day to configure.", reply_markup=day_keyboard(week_start)
        )
        return

    # Day-specific buttons carry their date to prevent edits to another day.
    parts = action.split(":")
    if len(parts) < 3 or parts[1] not in ("day", "types", "type", "setup", "generate", "view"):
        return

    selected_day = date.fromisoformat(parts[2])
    if not week_start <= selected_day < week_start + timedelta(days=7):
        await query.edit_message_text(
            "This day belongs to another week. Choose a day again.",
            reply_markup=day_keyboard(week_start),
        )
        return

    if selected_day < today:
        await query.edit_message_text(
            "That day has already passed. Choose an available day.",
            reply_markup=day_keyboard(week_start),
        )
        return

    selected_date = selected_day.isoformat()
    saved = load_week(ADMIN_ID, draft["start_date"])
    has_sessions = bool(saved["days"].get(selected_date, {}).get("sessions"))
    if parts[1] == "view" or (parts[1] == "day" and has_sessions):
        draft["selected_day"] = selected_date
        page = int(parts[3]) if parts[1] == "view" and len(parts) == 4 else 0
        await show_saved_day(query, selected_day, saved, page)
        return

    if saved["published"]:
        await query.edit_message_text(
            "This week is published and read-only. Choose a day to view it.",
            reply_markup=day_keyboard(week_start),
        )
        return

    if parts[1] in ("day", "types"):
        draft["selected_day"] = selected_date
        await query.edit_message_text(
            f"Selected day · {selected_day:%A, %Y-%m-%d}\n\n"
            "Choose the default session type for this day.",
            reply_markup=type_keyboard(selected_day),
        )
        return

    if selected_date != draft.get("selected_day"):
        await query.edit_message_text(
            "This button belongs to another day. Choose a day again.",
            reply_markup=day_keyboard(week_start),
        )
        return

    if parts[1] == "generate":
        if query.message is None or query.message.chat.type != "private":
            await query.edit_message_text("Open the bot in a private chat to enter times.")
            return
        day_settings = draft["days"].get(selected_date)
        if day_settings is None or "capacity" not in day_settings:
            await query.edit_message_text(
                "Choose the session type first.",
                reply_markup=type_keyboard(selected_day),
            )
            return
        await show_time_input(query, context, selected_day, day_settings)
        return

    if parts[1] == "setup":
        day_settings = draft["days"].get(selected_date)
        if day_settings is None or "capacity" not in day_settings:
            await query.edit_message_text("Choose a session type.", reply_markup=type_keyboard(selected_day))
            return
        await query.edit_message_text(day_summary(selected_day, day_settings), reply_markup=day_setup_keyboard(selected_day))
        return

    if len(parts) != 4:
        return

    if parts[1] != "type" or parts[3] not in ("private", "public"):
        return
    session_type = parts[3]
    defaults = get_settings(ADMIN_ID)
    day_settings = draft["days"].setdefault(selected_date, {})
    day_settings["session_type"] = session_type
    day_settings["capacity"] = defaults[f"{session_type}_capacity"]
    day_settings["duration_minutes"] = defaults["duration_minutes"]

    await query.edit_message_text(
        day_summary(selected_day, day_settings),
        reply_markup=day_setup_keyboard(selected_day),
    )



def generate_sessions(text: str, selected_day: date, settings: dict) -> list[dict]:
    fields = text.split()
    if len(fields) not in (2, 3):
        raise ValueError("Send two times and optionally a session count, such as 08:00 20:00 6.")

    minutes = []
    for value in fields[:2]:
        if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None:
            raise ValueError("Use times between 00:00 and 23:59, such as 08:00.")
        hour, minute = map(int, value.split(":"))
        minutes.append(hour * 60 + minute)

    opening, closing = minutes
    total_minutes = closing - opening
    if total_minutes <= 0:
        raise ValueError("Closing time must be later than opening time on the same day.")

    if len(fields) == 3:
        if not fields[2].isascii() or not fields[2].isdigit():
            raise ValueError("Session count must be a positive whole number.")
        count = int(fields[2])
        if count < 1 or count > total_minutes:
            raise ValueError("Each session must last at least one minute.")
        if total_minutes % count:
            raise ValueError("These hours cannot divide into that many equal sessions at minute precision. Choose another count or different times.")
        duration = total_minutes // count
    else:
        duration = settings.get("duration_minutes", 120)
        if total_minutes % duration:
            raise ValueError(f"These hours do not divide into {duration}-minute sessions. Add a session count or change the times.")
        count = total_minutes // duration

    midnight = datetime.combine(selected_day, datetime.min.time(), tzinfo=GYM_TIMEZONE)
    first_start = midnight + timedelta(minutes=opening)
    if first_start <= datetime.now(GYM_TIMEZONE):
        raise ValueError("The opening time must be in the future.")

    sessions = []
    for index in range(count):
        start = first_start + timedelta(minutes=index * duration)
        end = start + timedelta(minutes=duration)
        sessions.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "session_type": settings["session_type"],
            "capacity": settings["capacity"],
        })
    return sessions


async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.effective_user.id != ADMIN_ID:
        return
    if update.message is None or update.message.text is None:
        return

    pending = context.user_data.get("setting_edit")
    if pending is not None:
        if pending.get("stage") != "input":
            await update.message.reply_text("Use Confirm or Back on the settings preview.")
            return
        text = update.message.text.strip()
        maximum = 1439 if pending["field"] == "duration_minutes" else 2147483647
        if not text.isascii() or not text.isdigit() or len(text) > 10 or not 1 <= int(text) <= maximum:
            await update.message.reply_text(f"Send a whole number between 1 and {maximum}.")
            return
        pending.update(value=int(text), stage="preview", id=uuid4().hex[:12])
        await update.message.reply_text(
            f"{SETTING_LABELS[pending['field']]}\nNew default · {pending['value']}\n\nSave this change?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Confirm", callback_data=f"admin:setting_confirm:{pending['id']}")],
                [InlineKeyboardButton("Back", callback_data=f"admin:setting_back:{pending['field']}")],
            ]),
        )
        return

    selected_date = context.user_data.get("awaiting_times")
    draft = context.user_data.get("week_draft")
    if selected_date is None or draft is None:
        return

    settings = draft["days"].get(selected_date)
    if settings is None:
        return
    if load_week(ADMIN_ID, draft["start_date"])["published"]:
        context.user_data.pop("awaiting_times", None)
        await update.message.reply_text("This week is already published. Reopen /admin to view it.")
        return
    selected_day = date.fromisoformat(selected_date)
    try:
        sessions = generate_sessions(update.message.text, selected_day, settings)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    preview_id = uuid4().hex[:12]
    context.user_data.pop("awaiting_times", None)
    context.user_data["session_preview"] = {
        "id": preview_id,
        "week_start": draft["start_date"],
        "day": selected_date,
        "sessions": sessions,
    }

    lines = [f"Preview · {selected_day:%A, %Y-%m-%d}"]
    lines.append(f"{settings['session_type'].title()} · capacity {settings['capacity']} per session")
    lines.append(f"Total sessions · {len(sessions)}")
    for index, session in enumerate(sessions, start=1):
        start = datetime.fromisoformat(session["start"])
        end = datetime.fromisoformat(session["end"])
        lines.append(f"{index}. {start:%H:%M}–{end:%H:%M}")

    # Split long previews so every message fits Telegram's text limit.
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirm day", callback_data=f"admin:confirm:{preview_id}")],
        [InlineKeyboardButton("Back", callback_data=f"admin:back:{preview_id}")],
    ])
    await update.message.reply_text(chunk, reply_markup=keyboard)


def main() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path)

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Add BOT_TOKEN to your .env file.")

    initialize_database()
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", my_id))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(
        CallbackQueryHandler(admin_buttons, pattern=r"^admin:")
    )

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, admin_text)
    )

    print("Starting bot. Press Ctrl+C to stop.")
    application.run_polling()


if __name__ == "__main__":
    main()
