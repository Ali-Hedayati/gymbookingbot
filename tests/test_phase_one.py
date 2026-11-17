import asyncio
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import storage
import bookings as data
import bot
import phase_one as ui

ADMIN = bot.ADMIN_ID
TZ = ZoneInfo("Asia/Tehran")


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp.name) / "gym.sqlite3"
        storage.initialize_database()
        self.day = datetime.now(TZ).date() + timedelta(days=14)
        self.week = self.day - timedelta(days=(self.day.weekday() - 5) % 7)
        self.start = datetime.combine(self.day, datetime.min.time(), TZ) + timedelta(
            hours=8
        )
        slots = [
            {
                "start": (self.start + timedelta(hours=i * 2)).isoformat(),
                "end": (self.start + timedelta(hours=i * 2 + 2)).isoformat(),
                "session_type": "public",
                "capacity": 2,
            }
            for i in range(3)
        ]
        draft = storage.load_week(ADMIN, str(self.week))
        draft["days"][str(self.day)] = {"sessions": slots}
        draft["revision"] = storage.save_draft(ADMIN, draft)
        # Publish using the old API before running the new migration.
        storage.publish_week(
            ADMIN, str(self.week), draft["revision"], 3, datetime.now(TZ)
        )
        data.initialize()
        for key, value in {
            "card_number": "1234567890123456",
            "admin_phone": "09123456789",
            "public_price": 100,
            "private_price": 200,
        }.items():
            data.set_config(ADMIN, key, value)
        self.slots = data.sessions(ADMIN)
        self.people = [
            storage.create_customer(
                "+1202555" + str(1000 + i), "Customer " + str(i), 1000 + i
            )
            for i in range(12)
        ]

    def tearDown(self):
        storage.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_upgrade_and_idempotent_publish(self):
        data.initialize()
        self.assertEqual(len(data.sessions(ADMIN)), 3)
        self.assertEqual(
            storage.publish_week(ADMIN, str(self.week), 999, 99, datetime.now(TZ)), 3
        )
        self.assertEqual(len(data.sessions(ADMIN)), 3)

    def test_concurrent_capacity_and_same_person_retry(self):
        sid = self.slots[0]["id"]

        def attempt(person):
            try:
                return data.hold(sid, person["id"], ADMIN)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, self.people[:10]))
        accepted = [result for result in results if result]
        self.assertEqual(len(accepted), 2)
        self.assertEqual(data.session(sid, ADMIN)["available"], 0)
        first = accepted[0]
        again = data.hold(sid, first["customer_id"], ADMIN)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["expires_at"], first["expires_at"])

    def test_receipt_review_rejection_and_retry(self):
        held = data.hold(self.slots[0]["id"], self.people[0]["id"], ADMIN)
        with self.assertRaises(ValueError):
            data.submit_receipt(
                held["id"], self.people[1]["id"], "file", "photo", "unique"
            )
        pending = data.submit_receipt(
            held["id"], held["customer_id"], "file", "photo", "unique"
        )
        self.assertEqual(pending["status"], "pending")
        data.expire(held["expires_at"] + 10)
        self.assertEqual(data.booking(held["id"])["status"], "pending")
        data.submit_receipt(
            held["id"], held["customer_id"], "different", "photo", "other"
        )
        with storage.database() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE event_key LIKE 'receipt:%'"
                ).fetchone()[0],
                1,
            )
        rejected = data.decide(held["id"], ADMIN, False, "Please contact us")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(data.session(held["session_id"], ADMIN)["available"], 2)
        self.assertEqual(data.decide(held["id"], ADMIN, True)["status"], "rejected")
        retry = data.hold(held["session_id"], held["customer_id"], ADMIN)
        self.assertNotEqual(retry["id"], held["id"])

    def test_expiry_late_receipt_and_restart(self):
        now = time.time()
        held = data.hold(
            self.slots[0]["id"], self.people[0]["id"], ADMIN, now=now - 901
        )
        data.initialize()
        self.assertEqual(data.booking(held["id"])["status"], "expired")
        with self.assertRaises(ValueError):
            data.submit_receipt(
                held["id"], held["customer_id"], "file", "document", "x"
            )
        self.assertEqual(data.session(held["session_id"], ADMIN)["available"], 2)

    def test_approval_race_and_edit_safeguards(self):
        held = data.hold(self.slots[0]["id"], self.people[0]["id"], ADMIN)
        data.submit_receipt(held["id"], held["customer_id"], "file", "photo", "x")

        def approve(_):
            return data.decide(held["id"], ADMIN, True, tracking="ABC")["status"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(approve, range(4))), ["confirmed"] * 4)
        item = data.booking(held["id"])
        self.assertEqual(item["tracking_code"], "ABC")
        with storage.database() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE event_key LIKE 'decision:%'"
                ).fetchone()[0],
                1,
            )
        session = data.session(held["session_id"], ADMIN)
        with self.assertRaises(ValueError):
            data.edit_session(
                session["id"], ADMIN, session["version"], {"session_type": "private"}
            )
        data.edit_session(session["id"], ADMIN, session["version"], {"capacity": 3})
        with self.assertRaises(ValueError):
            data.edit_session(session["id"], ADMIN, session["version"], {"capacity": 4})
        self.assertEqual(data.session(session["id"], ADMIN)["available"], 2)

    def test_session_cancel_and_outbox_restart(self):
        held = data.hold(self.slots[0]["id"], self.people[0]["id"], ADMIN)
        data.submit_receipt(held["id"], held["customer_id"], "file", "photo", "x")
        item = data.session(held["session_id"], ADMIN)
        data.edit_session(item["id"], ADMIN, item["version"], {"cancelled": 1})
        self.assertEqual(data.booking(held["id"])["status"], "cancelled")
        self.assertNotIn(item["id"], [slot["id"] for slot in data.sessions(ADMIN)])
        events = data.outbox_batch()
        self.assertTrue(events)
        self.assertEqual(data.outbox_batch(), [])
        data.outbox_done(events[0]["id"], False)
        self.assertTrue(data.outbox_batch(time.time() + 121))

    def test_start_deadline_and_admin_assignment(self):
        held = data.hold(self.slots[0]["id"], self.people[0]["id"], ADMIN)
        data.submit_receipt(held["id"], held["customer_id"], "file", "photo", "x")
        data.expire(self.start.timestamp() + 1)
        self.assertEqual(data.booking(held["id"])["status"], "rejected")
        assigned = data.assign(self.slots[1]["id"], self.people[1]["id"], ADMIN, False)
        self.assertFalse(assigned["is_paid"])
        data.mark_paid(assigned["id"], ADMIN, "TRACK")
        self.assertTrue(data.booking(assigned["id"])["is_paid"])
        self.assertEqual(
            data.assign(self.slots[1]["id"], self.people[1]["id"], ADMIN, True)["id"],
            assigned["id"],
        )
        with self.assertRaises(ValueError):
            data.cancel_booking(assigned["id"], self.people[2]["telegram_id"], ADMIN)

    def test_draft_switch_overlap_and_add_published(self):
        next_day = self.day + timedelta(days=7)
        week = next_day - timedelta(days=(next_day.weekday() - 5) % 7)
        first = self.start + timedelta(days=7)
        slot = {
            "start": first.isoformat(),
            "end": (first + timedelta(hours=2)).isoformat(),
            "session_type": "public",
            "capacity": 6,
        }
        data.edit_draft(ADMIN, next_day, 0, 0, new_session=slot)
        saved = storage.load_week(ADMIN, str(week))
        data.edit_draft(
            ADMIN,
            next_day,
            0,
            saved["revision"],
            {"session_type": "private", "capacity": 2},
        )
        saved = storage.load_week(ADMIN, str(week))
        self.assertEqual(
            saved["days"][str(next_day)]["sessions"][0]["session_type"], "private"
        )
        with self.assertRaises(ValueError):
            data.edit_draft(ADMIN, next_day, 0, saved["revision"], new_session=slot)
        saved_week = storage.load_week(ADMIN, str(self.week))
        last = self.start + timedelta(hours=7)
        extra = {
            "start": last.isoformat(),
            "end": (last + timedelta(hours=1)).isoformat(),
            "session_type": "private",
            "capacity": 2,
        }
        data.add_session(ADMIN, self.day, saved_week["revision"], extra)
        self.assertEqual(len(data.sessions(ADMIN)), 4)

    def test_price_snapshot_customer_edit_and_phone_conflict(self):
        held = data.hold(self.slots[0]["id"], self.people[0]["id"], ADMIN)
        data.set_config(ADMIN, "public_price", 999)
        self.assertEqual(data.booking(held["id"])["amount"], 100)
        self.assertEqual(data.session(self.slots[0]["id"], ADMIN)["amount"], 999)
        data.edit_customer(self.people[0]["id"], "name", "Updated")
        self.assertEqual(storage.search_customers("updated")[1], 1)
        with self.assertRaises(ValueError):
            data.edit_customer(self.people[0]["id"], "phone", self.people[1]["phone"])

    def test_notifications_worker_media(self):
        held = data.hold(self.slots[0]["id"], self.people[0]["id"], ADMIN)
        data.submit_receipt(held["id"], held["customer_id"], "image-file", "photo", "x")

        async def run():
            sender = SimpleNamespace(
                send_photo=AsyncMock(),
                send_document=AsyncMock(),
                send_message=AsyncMock(),
            )
            app = SimpleNamespace(bot=sender)
            original = ui.asyncio.sleep

            async def stop(_):
                raise asyncio.CancelledError()

            ui.asyncio.sleep = stop
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await ui.maintenance(app)
            finally:
                ui.asyncio.sleep = original
            sender.send_photo.assert_awaited_once()

        asyncio.run(run())

    def test_registration_conflict_survives_upgrade(self):
        c = storage.create_customer("09123456789", "Ali")
        self.assertEqual(
            storage.register_customer("+989123456789", 123, "New", "ali")["id"], c["id"]
        )
        with self.assertRaises(ValueError):
            storage.register_customer("+989123456789", 456, "Other", None)
        storage.initialize_database()
        data.initialize()
        self.assertEqual(storage.customer_by_telegram(123)["name"], "Ali")

    def test_real_handlers_customer_and_admin_review(self):
        async def run():
            sent = SimpleNamespace(edit_text=AsyncMock())
            message = SimpleNamespace(
                text="screen",
                photo=[],
                document=None,
                reply_markup=None,
                reply_text=AsyncMock(return_value=sent),
                chat=SimpleNamespace(type="private"),
            )
            user = SimpleNamespace(id=self.people[0]["telegram_id"])
            query = SimpleNamespace(
                from_user=user,
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
                message=message,
                data="",
            )
            update = SimpleNamespace(
                callback_query=query,
                effective_user=user,
                effective_chat=SimpleNamespace(id=user.id, type="private"),
                message=message,
            )
            context = SimpleNamespace(
                user_data={},
                bot=SimpleNamespace(send_photo=AsyncMock(), send_document=AsyncMock()),
            )

            async def click(action):
                query.data = action
                await ui.handle_callback(update, context, ADMIN)

            await click(f"p:hold:{self.slots[0]['id']}:0")
            bid = context.user_data["receipt_booking_id"]
            message.photo = [SimpleNamespace(file_id="photo", file_unique_id="unique")]
            await ui.receipt_image(update, context, ADMIN)
            self.assertEqual(data.booking(bid)["status"], "pending")
            # A customer cannot approve their own pending receipt.
            message.photo = []
            await click(f"p:accept:{bid}")
            self.assertNotIn("phase_flow", context.user_data)
            admin = SimpleNamespace(id=ADMIN)
            query.from_user = admin
            update.effective_user = admin
            await click(f"p:accept:{bid}")
            self.assertEqual(context.user_data["phase_flow"]["kind"], "accept")
            await click("p:skip")
            self.assertEqual(data.booking(bid)["status"], "confirmed")
            context.bot.send_photo.assert_awaited()
            await click(f"p:aslot:{self.slots[0]['id']}")
            self.assertIn("Occupied · 1", query.edit_message_text.call_args.args[0])

        asyncio.run(run())

    def test_complete_week_wizard_mixed_slots_and_manual_add(self):
        async def run():
            day = self.day + timedelta(days=7)
            week = day - timedelta(days=(day.weekday() - 5) % 7)
            sent = SimpleNamespace(edit_text=AsyncMock())
            message = SimpleNamespace(
                text="screen",
                photo=[],
                document=None,
                reply_markup=None,
                reply_text=AsyncMock(return_value=sent),
                chat=SimpleNamespace(type="private"),
            )
            user = SimpleNamespace(id=ADMIN)
            query = SimpleNamespace(
                from_user=user,
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
                message=message,
                data="",
            )
            update = SimpleNamespace(
                callback_query=query,
                effective_user=user,
                effective_chat=SimpleNamespace(id=ADMIN, type="private"),
                message=message,
            )
            context = SimpleNamespace(
                user_data={},
                bot=SimpleNamespace(send_photo=AsyncMock(), send_document=AsyncMock()),
            )
            ui.admin_router = bot.admin_buttons

            async def click(action):
                query.data = action
                if action.startswith("admin:"):
                    await bot.admin_buttons(update, context)
                else:
                    await bot.phase_callback(update, context)

            async def text(value):
                message.text = value
                await bot.admin_text(update, context)

            await click("admin:week:" + str(week))
            await click("admin:day:" + str(day))
            await click(f"admin:type:{day}:public")
            await click(f"admin:generate:{day}")
            await click(f"admin:count:{day}")
            await text("08:00 20:00 6")
            token = context.user_data["session_preview"]["id"]
            await click(f"admin:toggle:{token}:0")
            self.assertEqual(
                context.user_data["session_preview"]["sessions"][0]["capacity"], 2
            )
            await click(f"admin:confirm:{token}")
            saved = storage.load_week(ADMIN, str(week))
            self.assertEqual(
                saved["days"][str(day)]["sessions"][0]["session_type"], "private"
            )
            self.assertEqual(
                saved["days"][str(day)]["sessions"][1]["session_type"], "public"
            )
            await click(f"p:de:{day}:1:type:{saved['revision']}")
            await click("p:confirm:" + context.user_data["phase_confirm"]["id"])
            self.assertEqual(
                storage.load_week(ADMIN, str(week))["days"][str(day)]["sessions"][1][
                    "session_type"
                ],
                "private",
            )
            await click(f"p:manual:{day}")
            await text("20:00 21:00 public")
            await click("p:confirm:" + context.user_data["phase_confirm"]["id"])
            saved = storage.load_week(ADMIN, str(week))
            self.assertEqual(len(saved["days"][str(day)]["sessions"]), 7)
            await click("admin:publish:" + str(week))
            await click(f"admin:publish_ok:{week}:{saved['revision']}:7")
            self.assertEqual(len(data.sessions(ADMIN, day)), 7)
            await click("admin:day:" + str(day))
            self.assertIn("Published day", query.edit_message_text.call_args.args[0])
            await click("p:config")
            await click("p:cf:public_price")
            # Back should return directly to payment settings.
            await click("p:back")
            self.assertIn("Payment settings", query.edit_message_text.call_args.args[0])
            await click(f"p:manual:{day}")
            await click("p:back")
            self.assertIn("Published day", query.edit_message_text.call_args.args[0])

        asyncio.run(run())

    def test_admin_assignment_and_profile_edit_through_handlers(self):
        async def run():
            sent = SimpleNamespace(edit_text=AsyncMock())
            message = SimpleNamespace(
                text="screen",
                photo=[],
                document=None,
                reply_markup=None,
                reply_text=AsyncMock(return_value=sent),
                chat=SimpleNamespace(type="private"),
            )
            user = SimpleNamespace(id=ADMIN)
            query = SimpleNamespace(
                from_user=user,
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
                message=message,
                data="",
            )
            update = SimpleNamespace(
                callback_query=query,
                effective_user=user,
                effective_chat=SimpleNamespace(id=ADMIN, type="private"),
                message=message,
            )
            context = SimpleNamespace(
                user_data={},
                bot=SimpleNamespace(send_photo=AsyncMock(), send_document=AsyncMock()),
            )

            async def click(action):
                query.data = action
                await bot.phase_callback(update, context)

            async def text(value):
                message.text = value
                await bot.admin_text(update, context)

            await click(f"p:assign:{self.slots[0]['id']}")
            await text("+447700900123")
            await click("p:newcustomer")
            await click("p:assignpaid:0")
            await click("p:skip")
            await click("p:confirm:" + context.user_data["phase_confirm"]["id"])
            person = storage.customer_by_phone("+447700900123")
            assigned = data.bookings_for(ADMIN, customer_id=person["id"])[0]
            self.assertEqual(assigned["status"], "confirmed")
            self.assertFalse(assigned["is_paid"])
            await click(f"p:customeredit:{person['id']}:name")
            await text("New name")
            await click("p:confirm:" + context.user_data["phase_confirm"]["id"])
            self.assertEqual(storage.get_customer(person["id"])["name"], "New name")
            await click(f"p:paid:{assigned['id']}")
            await click("p:skip")
            self.assertTrue(data.booking(assigned["id"])["is_paid"])
            # An old booking button must not silently use newly edited slot details.
            empty = self.slots[1]
            data.edit_session(empty["id"], ADMIN, empty["version"], {"capacity": 3})
            with self.assertRaises(ValueError):
                data.hold(
                    empty["id"],
                    self.people[1]["id"],
                    ADMIN,
                    expected_version=empty["version"],
                )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
