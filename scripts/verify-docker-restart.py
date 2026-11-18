"""Offline regression checks. Uses mocks only, even inside a deployed container."""
import importlib.util
import os
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

import django

django.setup()
from django.utils import timezone
from bots.models import BotEventTypes

candidate = os.getenv('RESTART_CANDIDATE_PATH')
if candidate:
    spec = importlib.util.spec_from_file_location('restart_candidate', candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
else:
    import bots.tasks.restart_bot_pod_task as module


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.join_at = timezone.now()
        self.event = MagicMock()
        self.event.event_type = BotEventTypes.JOIN_REQUESTED
        self.event.metadata = {}
        self.bot.last_bot_event.return_value = self.event
        self.task = MagicMock()
        self.task.request.id = 'restart-fixture'
        self.client = MagicMock()
        self.container = self.client.containers.get.return_value
        self.container.status = 'exited'
        self.docker_patch = patch('docker.from_env', return_value=self.client)
        self.launch_patch = patch('bots.launch_bot_utils.launch_bot')
        self.docker_patch.start()
        self.launch = self.launch_patch.start()
        self.addCleanup(self.docker_patch.stop)
        self.addCleanup(self.launch_patch.stop)

    def test_exited_container_is_archived_and_relaunched(self):
        module.restart_docker_bot(self.task, self.bot, self.event)
        self.container.rename.assert_called_once()
        self.launch.assert_called_once_with(self.bot)
        self.assertIsNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNone(self.event.requested_bot_action_taken_at)
        self.assertEqual(self.event.metadata['docker_restart_task_id'], 'restart-fixture')
        self.client.close.assert_called_once()

    def test_live_container_is_preserved(self):
        self.container.status = 'running'
        module.restart_docker_bot(self.task, self.bot, self.event)
        self.container.rename.assert_not_called()
        self.launch.assert_not_called()

    def test_redelivery_does_not_restart_replacement(self):
        self.event.metadata['docker_restart_task_id'] = self.task.request.id
        module.restart_docker_bot(self.task, self.bot, self.event)
        self.client.containers.get.assert_not_called()
        self.launch.assert_not_called()

    def test_expired_meeting_fails_without_rejoining(self):
        self.bot.join_at = timezone.now() - timedelta(hours=1)
        with patch.object(module.BotEventManager, 'create_event') as fail:
            module.restart_docker_bot(self.task, self.bot, self.event)
        fail.assert_called_once()
        self.client.containers.get.assert_not_called()
        self.launch.assert_not_called()

    def test_missing_container_is_relaunched(self):
        import docker
        self.client.containers.get.side_effect = docker.errors.NotFound('fixture')
        module.restart_docker_bot(self.task, self.bot, self.event)
        self.launch.assert_called_once_with(self.bot)

    def test_default_worker_forwards_to_launcher_without_kubernetes(self):
        task = module.restart_bot_pod
        with patch.dict(os.environ, LAUNCH_BOT_METHOD='docker-compose-multi-host'), patch.object(module.Bot.objects, 'get', return_value=self.bot), patch.object(task, 'apply_async') as enqueue, patch.object(module.config, 'load_incluster_config') as kube:
            task.run(113)
        enqueue.assert_called_once_with(args=[113], queue='bot_launcher_vm')
        kube.assert_not_called()

    def test_completed_bot_is_never_restarted(self):
        self.event.event_type = BotEventTypes.POST_PROCESSING_COMPLETED
        with patch.object(module.Bot.objects, 'get', return_value=self.bot), patch.object(module, 'restart_docker_bot') as restart, patch.object(module.config, 'load_incluster_config') as kube:
            module.restart_bot_pod.run(113)
        restart.assert_not_called()
        kube.assert_not_called()


if __name__ == '__main__':
    unittest.main()
