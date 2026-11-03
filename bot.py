import os
from datetime import datetime, timedelta
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
        [InlineKeyboardButton("📅 Create week", callback_data="admin:create_week")]
    ]

    await update.message.reply_text(
        "Welcome to the gym admin panel! 🏋️",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


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

    if query.data == "admin:create_week":
        today = datetime.now(GYM_TIMEZONE).date()

        # Python numbers weekdays from Monday=0 to Sunday=6.
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

    elif query.data and query.data.startswith("admin:week:"):
        week_start = query.data.removeprefix("admin:week:")

        context.user_data["week_draft"] = {
            "start_date": week_start,
            "days": {},
        }

        await query.edit_message_text(
            f"Selected week starting Saturday, {week_start}.\n\n"
            "Your week draft is ready for configuring its days."
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