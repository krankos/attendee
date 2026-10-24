"""Offline diagnostics/real join-loop tests without Django, Chrome, or a meeting."""

import ast
import json
import logging
import runpy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bots.join_diagnostics import JoinDiagnostics, meeting_key

# Compile the production retry method with its actual UI exception hierarchy.
# This avoids importing media/Django/Chrome dependencies just to test the loop.
source = ast.parse(Path("bots/web_bot_adapter/web_bot_adapter.py").read_text())
adapter = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "WebBotAdapter")
method = next(node for node in adapter.body if isinstance(node, ast.FunctionDef) and node.name == "repeatedly_attempt_to_join_meeting")
namespace = runpy.run_path("bots/web_bot_adapter/ui_methods.py")
namespace.update(logger=logging.getLogger("join-loop-test"), time=__import__("time"), sleep=lambda _: None)
exec(compile(ast.Module(body=[method], type_ignores=[]), "join-loop", "exec"), namespace)
join_loop = namespace["repeatedly_attempt_to_join_meeting"]


class JoinDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.diagnostics = JoinDiagnostics("https://meet.google.com/test-room?secret=hidden", "Hi Talents Assistant")
        self.capture = patch("bots.join_diagnostics.logger.log")
        self.log = self.capture.start()
        self.addCleanup(self.capture.stop)

    def events(self, name=None):
        rows = [json.loads(call.args[2]) for call in self.log.call_args_list]
        return [row for row in rows if not name or row["event"] == name]

    def user(self, identity, active=True, own=False):
        return {"deviceId": identity, "fullName": "Hi Talents Assistant", "isCurrentUser": own, "active": active}

    def test_duplicate_detection_deduplicates_updates_and_clears(self):
        self.diagnostics.participant_update(self.user("self", own=True))
        self.diagnostics.participant_update(self.user("other"))
        self.diagnostics.participant_update(self.user("other"))
        self.assertEqual(len(self.events("duplicate_assistant_detected")), 1)
        self.assertEqual(self.events("duplicate_assistant_detected")[0]["assistant_count"], 2)
        self.diagnostics.participant_update({"deviceId": "other", "active": False})
        self.assertEqual(len(self.events("duplicate_assistant_cleared")), 1)
        self.diagnostics.participant_update({"deviceId": "other", "active": True})
        self.assertEqual(len(self.events("duplicate_assistant_detected")), 2)

    def test_partial_updates_rejoin_and_retry_after_admission(self):
        self.diagnostics.participant_update(self.user("self", own=True))
        self.diagnostics.begin_attempt()
        self.assertTrue(self.events("join_attempt_started")[0]["self_already_in_meeting"])
        self.diagnostics.participant_update({"deviceId": "self", "active": False})
        self.diagnostics.participant_update({"deviceId": "self", "active": True})
        self.assertEqual(self.events("self_joined")[-1]["self_join_count"], 2)
        self.assertEqual(len(self.events("self_left")), 1)

    def test_human_participants_and_repeated_inactive_updates_do_not_alert(self):
        self.diagnostics.participant_update(self.user("self", own=True))
        self.diagnostics.participant_update({"deviceId": "human", "fullName": "Private Name", "active": True})
        self.diagnostics.participant_update({"deviceId": "unknown", "fullName": None, "active": True})
        self.assertFalse(self.events("duplicate_assistant_detected"))
        self.assertNotIn("human", self.diagnostics.participants)

    def test_logs_exclude_names_urls_credentials_and_exception_text(self):
        self.diagnostics.participant_update(self.user("self", own=True))
        self.diagnostics.failed(namespace["UiRetryableException"]("secret=hidden", "join_button"))
        encoded = json.dumps(self.events())
        for value in ["hidden", "secret", "Hi Talents Assistant", "meet.google.com"]:
            self.assertNotIn(value, encoded)
        self.assertEqual(meeting_key("https://MEET.google.com/test-room/"), self.diagnostics.context["meeting_key"])
        self.assertNotEqual(meeting_key("https://meet.google.com/other"), self.diagnostics.context["meeting_key"])

    def test_admission_timeout_and_host_denial_have_distinct_reasons(self):
        source = ast.parse(Path("bots/google_meet_bot_adapter/google_meet_ui_methods.py").read_text())
        methods = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "GoogleMeetUIMethods")
        method = next(node for node in methods.body if isinstance(node, ast.FunctionDef) and node.name == "look_for_denied_your_request_element")
        environment = dict(namespace, logging=logging, By=SimpleNamespace(XPATH="xpath"))
        exec(compile(ast.Module(body=[method], type_ignores=[]), "admission", "exec"), environment)
        for text, reason in [("Someone in the call denied your request to join", "host_denied"), ("No one responded to your request to join the call", "no_response_timeout"), ("You left the meeting", "left_meeting_screen")]:
            with self.subTest(reason=reason):
                fixture = SimpleNamespace(join_diagnostics=self.diagnostics, find_element_by_selector=lambda *_, **__: SimpleNamespace(text=text))
                with self.assertRaises(namespace["UiRequestToJoinDeniedException"]):
                    environment["look_for_denied_your_request_element"](fixture, "admission")
                self.assertEqual(self.events("admission_failed")[-1]["reason"], reason)

    def fixture(self, failures):
        fixture = SimpleNamespace(
            meeting_url="https://meet.google.com/test-room",
            join_diagnostics=self.diagnostics,
            driver=SimpleNamespace(session_id="browser-one"),
            init_driver=MagicMock(),
            attempt_to_join_meeting=MagicMock(side_effect=failures),
            after_bot_joined_meeting=MagicMock(),
            subclass_specific_after_bot_joined_meeting=MagicMock(),
            send_request_to_join_denied_message=MagicMock(),
            left_meeting=False,
            cleaned_up=False,
        )
        return fixture

    def test_real_join_loop_counts_expected_retry_and_success_once(self):
        fixture = self.fixture([namespace["UiRetryableExpectedException"]("blocked", "join_button"), None])
        join_loop(fixture)
        self.assertEqual([row["attempt"] for row in self.events("join_attempt_started")], [1, 2])
        self.assertEqual(len(self.events("join_attempt_failed")), 1)
        self.assertEqual(len(self.events("join_succeeded")), 1)
        fixture.after_bot_joined_meeting.assert_called_once()
        self.assertEqual(len({row["run_id"] for row in self.events()}), 1)

    def test_real_join_loop_admission_failure_never_logs_success(self):
        fixture = self.fixture([namespace["UiRequestToJoinDeniedException"]("timeout", "admission")])
        join_loop(fixture)
        fixture.send_request_to_join_denied_message.assert_called_once()
        fixture.after_bot_joined_meeting.assert_not_called()
        self.assertEqual(len(self.events("join_attempt_started")), 1)
        self.assertFalse(self.events("join_succeeded"))


class LauncherDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        class NotFound(Exception):
            pass

        class APIError(Exception):
            pass

        class CapacityError(Exception):
            pass

        self.docker = MagicMock()
        self.docker.errors.NotFound = NotFound
        self.docker.errors.APIError = APIError
        self.docker.errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
        self.docker.utils.parse_bytes.return_value = 1024
        self.client = self.docker.from_env.return_value
        self.client.containers.get.side_effect = NotFound()
        self.client.containers.list.return_value = []
        self.client.containers.run.return_value.short_id = "container-id"
        self.client.containers.run.return_value.logs.return_value = b""
        self.bot = SimpleNamespace(id=42, object_id="bot_fixture", metadata={"meeting_bot_id": "local-row"}, meeting_url="https://meet.google.com/test", ephemeral_container_name=lambda: "bot-fixture", automatic_leave_settings=lambda: {"max_uptime_seconds": 900})
        source = ast.parse(Path("bots/tasks/run_bot_in_ephemeral_container_task.py").read_text())
        method = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "run_bot_in_ephemeral_container")
        method.decorator_list = []
        import bots.join_diagnostics as diagnostics

        namespace = dict(logging=logging, docker=self.docker, Bot=SimpleNamespace(objects=SimpleNamespace(get=lambda **_: self.bot)), os=__import__("os"), uuid=__import__("uuid"), logger=logging.getLogger("launcher-test"), BotLauncherCapacityError=CapacityError, bot_log_context=diagnostics.bot_log_context, log_diagnostic=diagnostics.log_diagnostic)
        exec(compile(ast.Module(body=[method], type_ignores=[]), "launcher", "exec"), namespace)
        self.launch = namespace["run_bot_in_ephemeral_container"]
        self.task = SimpleNamespace(request=SimpleNamespace(id="task-id", retries=2, delivery_info={"redelivered": True}))

    def test_launch_correlates_task_container_and_runtime_environment(self):
        with patch.dict("os.environ", {"BOT_CONTAINER_NETWORK": "fixture-network"}), patch("builtins.open", mock_open(read_data="MemAvailable: 8000000 kB\n")), patch("time.sleep"), patch("bots.join_diagnostics.logger.log") as log:
            result = self.launch(self.task, 42)
        events = [json.loads(call.args[2]) for call in log.call_args_list]
        self.assertEqual([row["event"] for row in events], ["launcher_received", "container_started"])
        self.assertTrue(all(row["redelivered"] and row["task_retries"] == 2 for row in events))
        environment = self.client.containers.run.call_args.kwargs["environment"]
        self.assertEqual(environment["BOT_DIAGNOSTIC_LAUNCH_ID"], events[0]["launch_id"])
        self.assertEqual(environment["BOT_DIAGNOSTIC_TASK_ID"], "task-id")
        self.assertEqual(events[-1]["meeting_bot_id"], "local-row")
        self.assertEqual(result["status"], "started")

    def test_redelivery_logs_reuse_without_launching_a_second_container(self):
        self.client.containers.get.side_effect = None
        self.client.containers.get.return_value.status = "running"
        self.client.containers.get.return_value.short_id = "original-container"
        with patch("bots.join_diagnostics.logger.log") as log:
            result = self.launch(self.task, 42)
        self.client.containers.run.assert_not_called()
        self.assertEqual(result["status"], "already_started")
        self.assertEqual(json.loads(log.call_args.args[2])["event"], "launcher_reused_container")


if __name__ == "__main__":
    unittest.main()
