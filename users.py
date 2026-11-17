from uuid import uuid4

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from storage import (
    create_customer,
    customer_by_phone,
    customer_by_telegram,
    get_customer,
    normalize_phone,
    phone_digits,
    register_customer,
    search_customers,
)


def customer_summary(customer: dict) -> str:
    return (
        f"Customer #{customer['id']}\n"
        f"Name · {customer['name'] or 'Not provided'}\n"
        f"Phone · {customer['phone']}\n"
        f"Telegram ID · {customer['telegram_id'] or 'Not linked'}"
    )


async def customer_start(update, context) -> None:
    if update.message is None or update.effective_user is None:
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Open a private chat with this bot to register.")
        return
    customer = customer_by_telegram(update.effective_user.id)
    if customer:
        await update.message.reply_text(
            f"Welcome to the gym! 🏋️\n\n{customer_summary(customer)}",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await update.message.reply_text(
        "Welcome to the gym! 🏋️\n\nShare your own phone number to register.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Share my phone number", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


async def contact_registration(update, context) -> None:
    if update.message is None or update.effective_user is None:
        return
    if update.effective_chat.type != "private":
        return
    contact = update.message.contact
    if contact is None or contact.user_id != update.effective_user.id:
        await update.message.reply_text(
            "Please share your own phone number using the registration button. Send /start to see it."
        )
        return
    user = update.effective_user
    try:
        customer = register_customer(contact.phone_number, user.id, user.full_name, user.username)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return
    await update.message.reply_text(
        f"Your registration is complete.\n\n{customer_summary(customer)}",
        reply_markup=ReplyKeyboardRemove(),
    )


async def show_users_menu(query, context) -> None:
    context.user_data.pop("customer_flow", None)
    await query.edit_message_text(
        "Users\n\nChoose an action.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("List customers", callback_data="admin:users:list:0")],
            [InlineKeyboardButton("Search customers", callback_data="admin:users:search")],
            [InlineKeyboardButton("Add customer", callback_data="admin:users:add")],
            [InlineKeyboardButton("Back · admin panel", callback_data="admin:home")],
        ]),
    )


async def show_customer_results(edit, context, term: str, page: int) -> None:
    context.user_data.pop("customer_flow", None)
    customers, total = search_customers(term, page)
    page_count = max(1, (total + 7) // 8)
    page = min(max(0, page), page_count - 1)
    customers, total = search_customers(term, page)
    context.user_data["customer_results"] = {"term": term, "page": page}
    keyboard = []
    for customer in customers:
        label = customer["name"] or f"Customer #{customer['id']}"
        keyboard.append([InlineKeyboardButton(
            f"{label[:28]} · {customer['phone']}",
            callback_data=f"admin:users:detail:{customer['id']}",
        )])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("Previous", callback_data=f"admin:users:page:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Next", callback_data=f"admin:users:page:{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    keyboard.append([InlineKeyboardButton("Back · Users", callback_data="admin:users")])
    title = "Search results" if term else "Customers"
    await edit(
        f"{title}\nMatches · {total}\nPage {page + 1} of {page_count}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_customer_form(edit, context, stage: str) -> None:
    flow = context.user_data.setdefault("customer_flow", {"kind": "add"})
    flow["stage"] = stage
    flow.pop("confirmation_id", None)
    prompts = {
        "phone": "Add customer\n\nSend their phone number. Use +countrycode or an Iranian number starting with 09.",
        "name": "Send the customer's name, or use Skip.",
        "telegram_id": "Send their numeric Telegram user ID, or use Skip.\nUse the account ID, such as the number returned by /myid.",
    }
    back = {"phone": "admin:users", "name": "admin:users:form:phone", "telegram_id": "admin:users:form:name"}
    keyboard = []
    if stage != "phone":
        keyboard.append([InlineKeyboardButton("Skip", callback_data=f"admin:users:skip:{stage}")])
    keyboard.append([InlineKeyboardButton("Back", callback_data=back[stage])])
    await edit(prompts[stage], reply_markup=InlineKeyboardMarkup(keyboard))


async def show_customer_preview(message, context, edit: bool = False) -> None:
    flow = context.user_data["customer_flow"]
    flow.update(stage="confirm", confirmation_id=uuid4().hex[:12])
    text = (
        "Create this customer?\n\n"
        f"Phone · {flow['phone']}\n"
        f"Name · {flow.get('name') or 'Not provided'}\n"
        f"Telegram ID · {flow.get('telegram_id') or 'Not linked'}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirm", callback_data=f"admin:users:confirm:{flow['confirmation_id']}")],
        [InlineKeyboardButton("Back", callback_data="admin:users:form:telegram_id")],
    ])
    if edit:
        await message.edit_message_text(text, reply_markup=keyboard)
    else:
        await message.reply_text(text, reply_markup=keyboard)


async def handle_users_button(update, context) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[2] if len(parts) > 2 else "menu"
    if action == "menu":
        await show_users_menu(query, context)
    elif action == "list":
        await show_customer_results(query.edit_message_text, context, "", 0)
    elif action in ("results", "page"):
        previous = context.user_data.get("customer_results", {"term": "", "page": 0})
        page = int(parts[3]) if action == "page" else previous["page"]
        await show_customer_results(query.edit_message_text, context, previous["term"], page)
    elif action == "detail":
        customer = get_customer(int(parts[3]))
        if customer is None:
            await show_users_menu(query, context)
            return
        context.user_data.pop("customer_flow", None)
        await query.edit_message_text(
            customer_summary(customer),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back · results", callback_data="admin:users:results")]
            ]),
        )
    elif action == "search":
        context.user_data["customer_flow"] = {"kind": "search"}
        await query.edit_message_text(
            "Send a name, phone number, or numeric Telegram ID to search.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Back · Users", callback_data="admin:users")]
            ]),
        )
    elif action == "add":
        context.user_data["customer_flow"] = {"kind": "add"}
        await show_customer_form(query.edit_message_text, context, "phone")
    else:
        flow = context.user_data.get("customer_flow")
        if flow is None or flow.get("kind") != "add":
            await show_users_menu(query, context)
            return
        if action == "form":
            stage = parts[3]
            if stage not in ("phone", "name", "telegram_id") or (stage != "phone" and "phone" not in flow):
                await show_users_menu(query, context)
                return
            await show_customer_form(query.edit_message_text, context, stage)
        elif action == "skip" and flow.get("stage") == parts[3]:
            if parts[3] == "name":
                flow["name"] = None
                await show_customer_form(query.edit_message_text, context, "telegram_id")
            elif parts[3] == "telegram_id":
                flow["telegram_id"] = None
                await show_customer_preview(query, context, edit=True)
        elif action == "confirm" and flow.get("confirmation_id") == parts[3]:
            try:
                customer = create_customer(flow["phone"], flow.get("name"), flow.get("telegram_id"))
            except ValueError as error:
                await query.edit_message_text(
                    str(error), reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back · Users", callback_data="admin:users")]
                    ]),
                )
                return
            context.user_data.pop("customer_flow", None)
            await query.edit_message_text(
                f"Customer created.\n\n{customer_summary(customer)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back · Users", callback_data="admin:users")]
                ]),
            )


async def handle_customer_text(update, context) -> None:
    flow = context.user_data["customer_flow"]
    message = update.message
    text = message.text.strip()
    if flow["kind"] == "search":
        if not text or len(text) > 150:
            await message.reply_text("Enter a search of 1 to 150 characters.")
            return
        # Send a bot message so results use the same navigation as button requests.
        result_message = await message.reply_text("Searching customers…")
        await show_customer_results(result_message.edit_text, context, text, 0)
        return
    stage = flow["stage"]
    if stage == "phone":
        try:
            phone = normalize_phone(text)
        except ValueError as error:
            await message.reply_text(str(error))
            return
        existing = customer_by_phone(phone)
        if existing:
            context.user_data.pop("customer_flow", None)
            await message.reply_text(
                f"This phone number is already registered.\n\n{customer_summary(existing)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back · Users", callback_data="admin:users")]
                ]),
            )
            return
        flow["phone"] = phone
        form_message = await message.reply_text("Phone number accepted.")
        await show_customer_form(form_message.edit_text, context, "name")
    elif stage == "name":
        if not text or len(text) > 150:
            await message.reply_text("Enter a name of 1 to 150 characters, or press Skip.")
            return
        flow["name"] = text
        form_message = await message.reply_text("Name entered.")
        await show_customer_form(form_message.edit_text, context, "telegram_id")
    elif stage == "telegram_id":
        digits = phone_digits(text)
        if not digits.isascii() or not digits.isdigit() or len(digits) > 19 or not 1 <= int(digits) <= 9223372036854775807:
            await message.reply_text("Send a positive numeric Telegram ID, or press Skip.")
            return
        flow["telegram_id"] = int(digits)
        await show_customer_preview(message, context)
    else:
        await message.reply_text("Use Confirm or Back on the customer preview.")
