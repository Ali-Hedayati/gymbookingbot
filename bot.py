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

        # Monday is 0 and Saturday is 5 in Python.
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
        week_start = date.fromisoformat(
            query.data.removeprefix("admin:week:")
        )
        week_end = week_start + timedelta(days=6)

        context.user_data["week_draft"] = {
            "start_date": week_start.isoformat(),
            "days": {},
        }

        day_names = [
            "Saturday",
            "Sunday",
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
        ]

        keyboard = []

        for offset, day_name in enumerate(day_names):
            day_date = week_start + timedelta(days=offset)

            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{day_name} · {day_date:%Y-%m-%d}",
                        callback_data=f"admin:day:{day_date.isoformat()}",
                    )
                ]
            )

        await query.edit_message_text(
            f"Week of {week_start:%Y-%m-%d} "
            f"to {week_end:%Y-%m-%d}\n\n"
            "Choose a day to configure.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif query.data and query.data.startswith("admin:day:"):
        draft = context.user_data.get("week_draft")

        if draft is None:
            await query.edit_message_text(
                "Your draft has expired. Send /admin to choose a week again."
            )
            return

        selected_day = date.fromisoformat(
            query.data.removeprefix("admin:day:")
        )
        week_start = date.fromisoformat(draft["start_date"])

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

    elif query.data == "admin:days":
        draft = context.user_data.get("week_draft")

        if draft is None:
            await query.edit_message_text(
                "Your draft has expired. Send /admin to choose a week again."
            )
            return

        week_start = date.fromisoformat(draft["start_date"])
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

        await query.edit_message_text(
            "Choose a day to configure.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )