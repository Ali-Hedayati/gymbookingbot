# Gym booking bot — phase one

Python 3.10 or newer. Tested with python-telegram-bot 22.8. The gym uses Tehran time and a Saturday–Friday week.

## Install or update

Stop the running bot with Ctrl+C. For an existing installation, make a database backup while the bot is stopped.

```bash
cp gym_bot.sqlite3 "gym_bot.backup-$(date +%Y%m%d-%H%M%S).sqlite3"
```

Extract the update into the project folder. The code consists of `bot.py`, `storage.py`, `users.py`, `bookings.py`, and `phase_one.py`. Install the dependencies.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Keep the bot token in a local `.env` file. For a new installation, copy `.env.example` to `.env` and enter the real token. Set `ADMIN_ID` to the gym admin's numeric Telegram account ID. Existing installations default to the original admin ID if the variable is absent.

Start the bot.

```bash
.venv/bin/python bot.py
```

The database upgrades automatically at startup. Saved customers and schedules use the existing `gym_bot.sqlite3`. Store a backup before every update. The database and receipt references are local to the computer running the bot. Run one polling instance per bot token.

If this terminal needs Throne's local proxy, keep Throne connected and export its actual proxy address before starting Python. On the laptop setup used previously, the commands were as follows.

```bash
export HTTP_PROXY=http://127.0.0.1:2080
export HTTPS_PROXY=http://127.0.0.1:2080
.venv/bin/python bot.py
```

## Configure payments first

Open a private chat with the bot and send `/admin`. Open Settings → Payment settings and enter the payment card, admin contact number, private/public prices, and currency label. Enter prices as whole numbers in the selected currency. A customer can reserve when the selected session has a positive price and the card and contact are configured.

The regular Settings panel holds private capacity, public capacity, and default session duration. Initial values are 2 people, 6 people, and 120 minutes. Updated defaults apply when choosing a type for new session generation. Saved sessions retain their own capacity and times.

Both Private and Public use individual places in this phase. One customer books one place. Another customer may book the remaining place in a capacity-two private session.

## Design a week

1. Send `/admin` and choose Create / open week.
2. Choose this week or next week, then a future day. A day with saved slots opens its session list first.
3. For a new day, select the initial session type and press Generate sessions.
4. Choose Use default duration or Set number of sessions.
5. Enter `08:00 20:00` for the configured duration, or `08:00 20:00 6` for six equal slots.
6. In the preview, tap a slot to switch only that slot between Private and Public. Its capacity follows the configured default for the new type.
7. Confirm the day to save the draft. Open individual saved slots to change type, capacity, times, or remove a slot. Add a session manually with an input such as `20:00 21:00 private`.
8. Configure other days, return to the week, and choose Publish week. Review the count and confirm.

Days without slots have no bookable sessions. Publishing includes future slots. Publishing repeatedly creates one set of sessions. Back discards the current unconfirmed change.

## Customer booking and payment

1. Send `/start` and use Share my phone number. The contact must belong to the sender. An admin-created customer record with the same phone is linked to that Telegram account when it is available for linking.
2. Open Book a session, choose the day, and select a published slot. Each slot shows available places and its price.
3. Choose Pre-reserve one place. The bot shows the payment amount, card, and exact deadline. The hold lasts 15 minutes, or until session start if sooner.
4. Upload a receipt photo or an image document while that booking is selected. When holding more than one session, open My bookings and select the correct hold before each upload. After a restart, select the booking again before uploading.
5. The admin receives the receipt with customer and session details. A submitted receipt keeps the place reserved while review is pending.
6. Approval confirms the booking and records payment. Rejection frees the place and sends the customer the reason and admin contact.

Customers can release an unpaid hold. Submitted and confirmed bookings are cancelled through the admin. Payment amounts and card details are captured when the hold is created.

A hold expires automatically if no receipt arrives. If a request is still pending when its session starts, it is rejected and the customer is told to contact the admin about the payment. An expired hold is never restored by a late receipt.

## Admin management

- Payment requests lists pending receipts. Open a request, inspect the image, then Accept or Reject. Approval offers an optional tracking code. Rejection offers an optional reason. Skip completes the chosen action.
- Sessions and bookings shows published sessions, capacity, active occupancy, and the customers booked into each slot.
- Open a session to switch type, edit capacity, times, or price, assign a customer, or cancel the session.
- For a booked session, capacity must accommodate all active holds, submitted requests, and confirmed bookings. Changing its type, time, or price requires cancelling the active bookings first. Default prices can be updated for new bookings; existing booking amounts remain recorded.
- Cancelling a session cancels its active bookings and queues customer notifications. Arrange any refunds separately with paid customers.
- Assign customer searches by name, phone, or Telegram ID. If a valid phone has no matching record, create a customer from that phone. Choose Paid or Unpaid, optionally enter a tracking code, and confirm.
- Users supports listing, searching, creating customer records, and opening customer bookings. Open Bookings and edit customer to update a customer's name, phone, or numeric Telegram ID.

Phone input accepts international format and Iranian mobile numbers starting with 09. Persian and Arabic digits are accepted for customer phone numbers.

## Notifications and restart recovery

Holds, receipts, decisions, and notifications are stored in SQLite. A background worker checks expiry and attempts notification delivery every 15 seconds. Failed delivery is retried. If the admin bot chat is unavailable, the request remains accessible under Payment requests. The bot must be running to deliver messages and process uploads.

Telegram messages may be delivered more than once after a retry around a process interruption. Decisions and bookings are protected against repeated actions.

## Test phase one

Use a separate customer Telegram account if convenient, or register the admin account as a customer through `/start`.

1. Set payment details and publish a future day with at least one private and one public slot.
2. Register two customers. In a capacity-two slot, create two holds and verify a third customer cannot hold a place.
3. Upload a receipt. Check that the image appears in the admin chat and under Payment requests.
4. Approve it with a tracking code. Check My bookings and the admin session/customer views.
5. Repeat with rejection and a custom reason. Verify capacity is released and the customer receives the message.
6. Create another hold and let it expire. Verify its place is released. A late upload should direct the customer to the admin.
7. Restart the bot while a receipt is pending. Reopen Payment requests and process it.
8. Test changing an empty slot's type and times. Check that overlapping times and unsafe changes to occupied slots are rejected.
9. Try an unpaid admin assignment, record payment, and inspect it from the customer profile.

Run automated tests from the project folder.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The automated checks use temporary databases and mocked Telegram delivery. Live Telegram/VPN latency and file delivery need testing on the deployment computer.

## Commit this phase

```bash
git add bot.py storage.py users.py bookings.py phase_one.py requirements.txt .env.example .gitignore README.md tests/
git commit -m "Implement phase one scheduling and receipt-approved bookings"
git push
```
