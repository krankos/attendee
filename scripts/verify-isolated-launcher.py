import os
import unittest
from unittest.mock import MagicMock, mock_open, patch

import django
django.setup()

import docker
from bots.tasks.run_bot_in_ephemeral_container_task import BotLauncherCapacityError, run_bot_in_ephemeral_container


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.ephemeral_container_name.return_value = "attendee-isolation-fixture"
        self.bot.automatic_leave_settings.return_value = {"max_uptime_seconds": 900}
        self.client = MagicMock()
        self.client.containers.get.side_effect = docker.errors.NotFound("absent")
        self.client.containers.list.return_value = []
        self.client.containers.run.return_value.short_id = "fixture"
        self.client.containers.run.return_value.logs.return_value = b""
        self.runner = getattr(run_bot_in_ephemeral_container, "_orig_run", run_bot_in_ephemeral_container.run)

    def launch(self, available_kb=8000000):
        with patch("docker.from_env", return_value=self.client), patch("bots.tasks.run_bot_in_ephemeral_container_task.Bot.objects.get", return_value=self.bot), patch("builtins.open", mock_open(read_data=f"MemAvailable: {available_kb} kB\n")), patch("time.sleep"):
            return self.runner(42)

    def test_bridge_audio_runtime_and_image_code(self):
        with patch.dict(os.environ, {"BOT_CONTAINER_NETWORK": "fixture-network", "BOT_CONTAINER_IMAGE": "fixture-image", "BOT_MEMORY_LIMIT": "1280m", "PULSE_SERVER": "inherited-socket", "XDG_RUNTIME_DIR": "/inherited", "DOCKER_HOST": "unix:///var/run/docker.sock"}):
            self.launch()
        options = self.client.containers.run.call_args.kwargs
        self.assertEqual(options["network"], "fixture-network")
        self.assertEqual(options["image"], "fixture-image")
        self.assertEqual(options["volumes"], {})
        self.assertNotIn("PULSE_SERVER", options["environment"])
        self.assertNotIn("XDG_RUNTIME_DIR", options["environment"])
        self.assertNotIn("DOCKER_HOST", options["environment"])
        self.assertEqual(options["mem_limit"], "1280m")
        self.assertIn("timeout 4500", options["command"])

    def test_redelivery_preserves_running_bot(self):
        self.client.containers.get.side_effect = None
        self.client.containers.get.return_value.status = "running"
        result = self.launch()
        self.assertEqual(result["status"], "already_started")
        self.client.containers.run.assert_not_called()

    def test_low_memory_keeps_task_retryable_without_starting_a_bot(self):
        with patch.dict(os.environ, {"BOT_CONTAINER_NETWORK": "fixture-network", "BOT_MEMORY_LIMIT": "1280m"}):
            with self.assertRaises(BotLauncherCapacityError):
                self.launch(available_kb=512000)
        self.client.containers.run.assert_not_called()

    def test_concurrency_cap_keeps_task_retryable(self):
        with patch.dict(os.environ, {"BOT_MAX_SIMULTANEOUS_BOTS": "0"}):
            with self.assertRaises(BotLauncherCapacityError):
                self.launch()
        self.client.containers.run.assert_not_called()

    def test_host_network_is_rejected(self):
        with patch.dict(os.environ, {"BOT_CONTAINER_NETWORK": "host"}):
            with self.assertRaises(RuntimeError):
                self.launch()
        self.client.containers.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
