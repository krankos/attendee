"""Admission regressions; no browser or database is needed."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bots.google_meet_bot_adapter.google_meet_ui_methods import GoogleMeetUIMethods

MODULE = "bots.google_meet_bot_adapter.google_meet_ui_methods"


class GoogleMeetAdmissionTests(unittest.TestCase):
    def bot(self):
        bot = GoogleMeetUIMethods()
        bot.driver = MagicMock()
        bot.driver.execute_script.return_value = False
        bot.driver.find_elements.return_value = []
        bot.automatic_leave_configuration = SimpleNamespace(waiting_room_timeout_seconds=1)
        return bot

    def test_self_participant_starts_admission_without_captions(self):
        bot = self.bot()
        bot.driver.execute_script.return_value = True
        self.assertTrue(bot.is_in_call())
        bot.driver.find_elements.assert_not_called()

    def test_missing_self_and_controls_do_not_admit(self):
        self.assertFalse(self.bot().is_in_call())

    def test_controls_are_fallback_when_participant_sync_is_not_ready(self):
        bot = self.bot()
        bot.driver.find_elements.return_value = [object()]
        self.assertTrue(bot.is_in_call())

    def test_admission_returns_before_slow_checks(self):
        bot = self.bot()
        bot.is_in_call = MagicMock(return_value=True)
        bot.look_for_blocked_element = MagicMock()
        bot.wait_until_admitted()
        bot.look_for_blocked_element.assert_not_called()

    def test_denial_propagates(self):
        bot = self.bot()
        bot.driver.execute_script.return_value = "Someone in the call denied your request to join"
        bot.is_in_call = MagicMock(return_value=False)
        bot.look_for_denied_your_request_element = MagicMock(side_effect=RuntimeError("denied"))
        with self.assertRaisesRegex(RuntimeError, "denied"):
            bot.wait_until_admitted()

    def test_waiting_room_timeout_propagates(self):
        bot = self.bot()
        bot.check_if_waiting_room_timeout_exceeded = MagicMock(side_effect=RuntimeError("timeout"))
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            bot.wait_until_admitted()

    def test_poll_does_not_search_page_when_no_notice_is_present(self):
        bot = self.bot()
        bot.is_in_call = MagicMock(side_effect=[False, True])
        bot.driver.execute_script.return_value = "Waiting for admission"
        bot.look_for_denied_your_request_element = MagicMock()
        bot.look_for_blocked_element = MagicMock()
        bot.check_if_waiting_room_timeout_exceeded = MagicMock()
        with patch(MODULE + ".time.sleep") as sleep:
            bot.wait_until_admitted()
        sleep.assert_called_once_with(0.25)
        bot.look_for_blocked_element.assert_not_called()
        bot.look_for_denied_your_request_element.assert_not_called()

    def test_caption_failure_does_not_skip_remaining_setup(self):
        bot = self.bot()
        bot.disable_incoming_video = True
        bot.google_meet_closed_captions_language = None
        bot.enable_captions_best_effort = MagicMock(side_effect=RuntimeError("captions"))
        bot.disable_incoming_video_in_ui = MagicMock()
        bot.ready_to_show_bot_image = MagicMock()
        bot.after_admitted_setup()
        bot.disable_incoming_video_in_ui.assert_called_once()
        bot.ready_to_show_bot_image.assert_called_once()
