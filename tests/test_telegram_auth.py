"""Telegram callbacks and commands are accepted ONLY from the configured chat."""
import unittest
from unittest import mock

from alerts.telegram import TelegramAlerts


def _updates(*items):
    return {"ok": True, "result": list(items)}


def _cb(uid, sender, chat, data, cb_id="q1"):
    return {"update_id": uid, "callback_query": {"id": cb_id, "data": data,
            "from": {"id": sender}, "message": {"message_id": 7, "chat": {"id": chat}}}}


def _msg(uid, sender, chat, text):
    return {"update_id": uid, "message": {"text": text, "from": {"id": sender}, "chat": {"id": chat}}}


class TestTelegramAuth(unittest.TestCase):
    def setUp(self):
        self.a = TelegramAlerts({"bot_token": "tok", "chat_id": "123"})
        self.a.pending_confirms = {"confirm_x": {"opp": {"question": "q"}}, "skip_x": {"opp": {"question": "q"}}}
        self.posts = []
        self.a._post = lambda method, data: self.posts.append((method, data)) or {"ok": True}

    def test_owner_confirm_is_accepted(self):
        self.a._get = lambda *a, **k: _updates(_cb(1, 123, 123, "confirm_x"))
        out = self.a.poll_callbacks()
        self.assertEqual(len(out), 1)
        self.assertNotIn("confirm_x", self.a.pending_confirms)

    def test_stranger_confirm_is_rejected_and_stays_pending(self):
        self.a._get = lambda *a, **k: _updates(_cb(1, 999, 999, "confirm_x"))
        out = self.a.poll_callbacks()
        self.assertEqual(out, [])
        self.assertIn("confirm_x", self.a.pending_confirms)          # still awaiting the owner
        self.assertEqual(self.posts[0][0], "answerCallbackQuery")
        self.assertEqual(self.posts[0][1]["text"], "Not authorized")
        self.assertEqual(self.a.offset, 2)                            # but the update is consumed

    def test_forwarded_into_owner_chat_by_other_user_is_rejected(self):
        # group chat with the owner's id as chat but a different sender
        self.a._get = lambda *a, **k: _updates(_cb(1, 999, 123, "confirm_x"))
        self.assertEqual(self.a.poll_callbacks(), [])
        self.assertIn("confirm_x", self.a.pending_confirms)

    def test_stranger_resume_command_is_dropped(self):
        self.a._get = lambda *a, **k: _updates(_msg(1, 999, 999, "/resume"), _msg(2, 123, 123, "/pnl"))
        self.a.poll_callbacks()
        self.assertEqual(self.a.drain_commands(), ["pnl"])

    def test_unconfigured_chat_id_accepts_nothing(self):
        a = TelegramAlerts({"bot_token": "tok", "chat_id": ""})
        a._post = lambda *x: {"ok": True}
        a._get = lambda *x, **k: _updates(_msg(1, 123, 123, "/resume"))
        a.poll_callbacks()
        self.assertEqual(a.drain_commands(), [])


if __name__ == "__main__":
    unittest.main()
