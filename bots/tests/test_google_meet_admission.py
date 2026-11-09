"""Admission regressions; no browser or database is needed."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bots.google_meet_bot_adapter.google_meet_ui_methods import GoogleMeetUIMethods, UiKnockAgainException, UiNoOneRespondedException
from bots.web_bot_adapter.ui_methods import UiCouldNotJoinMeetingWaitingRoomTimeoutException, UiInfinitelyRetryableException, UiRequestToJoinDeniedException

MODULE = "bots.google_meet_bot_adapter.google_meet_ui_methods"


class GoogleMeetAdmissionTests(unittest.TestCase):
    def bot(self):
        bot = GoogleMeetUIMethods()
        bot.driver = MagicMock()
        bot.driver.execute_script.return_value = False
        bot.driver.find_elements.return_value = []
        bot.automatic_leave_configuration = SimpleNamespace(waiting_room_timeout_seconds=1)
        bot.ADMISSION_CONFIRMATION_SECONDS = 0
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
        bot.is_in_call = MagicMock(side_effect=[False, True, True])
        bot.driver.execute_script.return_value = "Waiting for admission"
        bot.look_for_denied_your_request_element = MagicMock()
        bot.look_for_blocked_element = MagicMock()
        bot.check_if_waiting_room_timeout_exceeded = MagicMock()
        with patch(MODULE + ".time.sleep") as sleep:
            bot.wait_until_admitted()
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.25, 0])
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


class GoogleMeetKnockAgainTests(unittest.TestCase):
    def bot(self, waiting_room_timeout_seconds=900):
        bot = GoogleMeetUIMethods()
        bot.driver = MagicMock()
        bot.google_meet_bot_login_is_available = False
        bot.ui_interaction_mode = "default"
        bot.get_layout_to_select = MagicMock(return_value="speaker")
        bot.ask_to_join = MagicMock()
        bot.abort_join_attempt = MagicMock()
        bot.automatic_leave_configuration = SimpleNamespace(waiting_room_timeout_seconds=waiting_room_timeout_seconds)
        return bot

    def test_unanswered_knock_asks_for_a_fresh_browser(self):
        bot = self.bot()
        bot.wait_until_admitted = MagicMock(side_effect=UiNoOneRespondedException("no one"))
        with self.assertRaises(UiKnockAgainException) as raised:
            bot.attempt_to_join_meeting()
        # Retried by the outer loop without using up its retry count.
        self.assertIsInstance(raised.exception, UiInfinitelyRetryableException)
        bot.abort_join_attempt.assert_not_called()

    def test_second_knock_in_fresh_browser_keeps_the_budget(self):
        bot = self.bot()
        bot.wait_until_admitted = MagicMock(side_effect=UiNoOneRespondedException("no one"))
        with self.assertRaises(UiKnockAgainException):
            bot.attempt_to_join_meeting()
        started_at = bot.waiting_room_timeout_started_at
        bot.ask_to_join = MagicMock()
        bot.wait_until_admitted = MagicMock(return_value=None)
        bot.attempt_to_join_meeting()
        bot.ask_to_join.assert_called_once_with(2)
        bot.wait_until_admitted.assert_called_once_with(started_at)
        self.assertEqual(bot.layout_to_select_after_admission, "speaker")

    def test_spent_budget_reports_waiting_room_timeout(self):
        bot = self.bot(waiting_room_timeout_seconds=10)
        bot.wait_until_admitted = MagicMock(side_effect=UiNoOneRespondedException("no one"))
        with self.assertRaises(UiCouldNotJoinMeetingWaitingRoomTimeoutException):
            bot.attempt_to_join_meeting()
        bot.ask_to_join.assert_called_once()
        bot.abort_join_attempt.assert_called_once()

    def test_fresh_browser_with_spent_budget_does_not_knock(self):
        bot = self.bot()
        bot.knocks_sent = 1
        bot.waiting_room_timeout_started_at = time.time() - 890
        with self.assertRaises(UiCouldNotJoinMeetingWaitingRoomTimeoutException):
            bot.attempt_to_join_meeting()
        bot.ask_to_join.assert_not_called()

    def test_active_denial_is_not_retried(self):
        bot = self.bot()
        bot.wait_until_admitted = MagicMock(side_effect=UiRequestToJoinDeniedException("denied"))
        with self.assertRaises(UiRequestToJoinDeniedException) as raised:
            bot.attempt_to_join_meeting()
        self.assertNotIsInstance(raised.exception, UiKnockAgainException)


class GoogleMeetAdmissionConfirmationTests(unittest.TestCase):
    def bot(self):
        bot = GoogleMeetUIMethods()
        bot.driver = MagicMock()
        bot.ADMISSION_CONFIRMATION_SECONDS = 0
        bot.automatic_leave_configuration = SimpleNamespace(waiting_room_timeout_seconds=900)
        return bot

    def test_exit_notice_after_admission_signal_is_not_admission(self):
        # 2026-11-08: admission signal fired as Meet showed the knock timeout page.
        bot = self.bot()
        bot.is_in_call = MagicMock(return_value=True)
        bot.driver.execute_script.return_value = "No one responded to your request to join the call\nReturning to home screen"
        bot.look_for_denied_your_request_element = MagicMock(side_effect=UiNoOneRespondedException("no one"))
        with patch(MODULE + ".time.sleep"):
            with self.assertRaises(UiNoOneRespondedException):
                bot.wait_until_admitted()

    def test_admission_that_holds_is_accepted(self):
        bot = self.bot()
        bot.is_in_call = MagicMock(return_value=True)
        bot.driver.execute_script.return_value = "Hi Talents Assistant\nNouha"
        with patch(MODULE + ".time.sleep"):
            bot.wait_until_admitted()

    def test_admission_that_vanishes_keeps_waiting(self):
        bot = self.bot()
        bot.is_in_call = MagicMock(side_effect=[True, False, False, True, True])
        bot.driver.execute_script.return_value = "Please wait until a meeting host brings you into the call"
        bot.check_if_waiting_room_timeout_exceeded = MagicMock()
        with patch(MODULE + ".time.sleep"):
            bot.wait_until_admitted()
        self.assertEqual(bot.is_in_call.call_count, 5)


class GoogleMeetKnockAgainJoinLoopTests(unittest.TestCase):
    """The real outer join loop: fresh browser per knock, ends on the budget."""

    def adapter(self, ask_to_join, wait_until_admitted, waiting_room_timeout_seconds=900):
        adapter = MagicMock()
        adapter.left_meeting = False
        adapter.cleaned_up = False
        adapter.meeting_url = "https://meet.google.com/abc-defg-hij"
        ui = GoogleMeetUIMethods()
        ui.google_meet_bot_login_is_available = False
        ui.ui_interaction_mode = "default"
        ui.get_layout_to_select = MagicMock(return_value="speaker")
        ui.abort_join_attempt = MagicMock()
        ui.automatic_leave_configuration = SimpleNamespace(waiting_room_timeout_seconds=waiting_room_timeout_seconds)
        ui.ask_to_join = ask_to_join
        ui.wait_until_admitted = wait_until_admitted
        adapter.attempt_to_join_meeting = ui.attempt_to_join_meeting
        adapter.automatic_leave_configuration = ui.automatic_leave_configuration
        return adapter, ui

    def run_loop(self, adapter):
        from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter

        with patch("bots.web_bot_adapter.web_bot_adapter.sleep"):
            WebBotAdapter.repeatedly_attempt_to_join_meeting(adapter)

    def test_admitted_on_second_knock_after_a_fresh_browser(self):
        adapter, ui = self.adapter(MagicMock(), MagicMock(side_effect=[UiNoOneRespondedException("no one"), None]))
        self.run_loop(adapter)
        self.assertEqual(adapter.init_driver.call_count, 2)
        self.assertEqual([c.args[0] for c in ui.ask_to_join.call_args_list], [1, 2])
        adapter.after_bot_joined_meeting.assert_called_once()

    def test_knocking_stops_when_the_budget_is_spent(self):
        adapter, ui = self.adapter(MagicMock(), MagicMock(side_effect=UiNoOneRespondedException("no one")), waiting_room_timeout_seconds=900)
        clock = iter(range(0, 100000, 100))
        with patch(MODULE + ".time.time", side_effect=lambda: next(clock)):
            self.run_loop(adapter)
        adapter.after_bot_joined_meeting.assert_not_called()
        sent = [c.args[0]["message"] for c in adapter.send_message_callback.call_args_list]
        self.assertEqual(sent, [adapter.Messages.LEAVE_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED])
        self.assertGreaterEqual(ui.ask_to_join.call_count, 2)
