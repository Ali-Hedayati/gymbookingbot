import os
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
)


ADMIN_ID = 7265018265
GYM_TIMEZONE = ZoneInfo("Asia/Tehran")


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is not None:
        await update.message.reply_text(
            "Welcome to the gym booking bot! 🏋️"
        )


async def my_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is not None and update.effective_user is not None:
        await update.message.reply_text(
            f"Your Telegram user ID is {update.effective_user.id}"
        )


async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None or update.effective_user is None:
        return

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "You do not have access to the admin panel."
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "📅 Create week",
                callback_data="admin:create_week",
            )
        ]
    ]

    await update.message.reply_text(
        "Welcome to the gym admin panel! 🏋️",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def day_keyboard(week_start: date) -> InlineKeyboardMarkup:
    keyboard = []

    for offset in range(7):
        day_date = week_start + timedelta(days=offset)
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{day_date:%A} · {day_date:%Y-%m-%d}",
                    callback_data=f"admin:day:{day_date.isoformat()}",
                )
            ]
        )

    return InlineKeyboardMarkup(keyboard)


async def admin_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer("Admin access required.", show_alert=True)
        return

    await query.answer()
    action = query.data or ""

    if action == "admin:create_week":
        today = datetime.now(GYM_TIMEZONE).date()

        # Python counts Monday as 0 and Saturday as 5.
        days_since_saturday = (today.weekday() - 5) % 7
        this_saturday = today - timedelta(days=days_since_saturday)
        next_saturday = this_saturday + timedelta(days=7)

        keyboard = [
            [
                InlineKeyboardButton(
                    f"This week · {this_saturday:%Y-%m-%d}",
                    callback_data=f"admin:week:{this_saturday.isoformat()}",
                )
            ],
            [
                InlineKeyboardButton(
                    f"Next week · {next_saturday:%Y-%m-%d}",
                    callback_data=f"admin:week:{next_saturday.isoformat()}",
                )
            ],
        ]

        await query.edit_message_text(
            "Choose the week you want to prepare.\n"
            "Dates are shown as year-month-day.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action.startswith("admin:week:"):
        week_start = date.fromisoformat(
            action.removeprefix("admin:week:")
        )
        week_end = week_start + timedelta(days=6)

        context.user_data["week_draft"] = {
            "start_date": week_start.isoformat(),
            "days": {},
        }

        await query.edit_message_text(
            f"Week of {week_start:%Y-%m-%d} "
            f"to {week_end:%Y-%m-%d}\n\n"
            "Choose a day to configure.",
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
            "Choose a day to configure.",
            reply_markup=day_keyboard(week_start),
        )
        return

    if action.startswith("admin:day:"):
        selected_day = date.fromisoformat(
            action.removeprefix("admin:day:")
        )

        if not week_start <= selected_day < week_start + timedelta(days=7):
            await query.edit_message_text(
                "This day belongs to another week. "
                "Send /admin to choose your week again."
            )
            return

        draft["selected_day"] = selected_day.isoformat()

        keyboard = [
            [
                InlineKeyboardButton(
                    "Choose another day",
                    callback_data="admin:days",
                )
            ]
        ]

        await query.edit_message_text(
            f"Selected day · {selected_day:%A, %Y-%m-%d}\n\n"
            "This is the day we will configure sessions for.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


def main() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path)

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Add BOT_TOKEN to your .env file.")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", my_id))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(
        CallbackQueryHandler(admin_buttons, pattern=r"^admin:")
    )

    print("Starting bot. Press Ctrl+C to stop.")
    application.run_polling()


if __name__ == "__main__":
    main()