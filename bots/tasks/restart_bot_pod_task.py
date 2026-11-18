import logging
import os
from datetime import timedelta
import time

from celery import shared_task
from kubernetes import client, config

from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes
from django.utils import timezone

logger = logging.getLogger(__name__)
from django.conf import settings

from bots.bot_pod_creator import BotPodCreator


@shared_task(bind=True, soft_time_limit=3600)
def restart_bot_pod(self, bot_id):
    """
    Restart a bot pod.
    """

    logger.info(f"Restarting bot pod for bot {bot_id}")

    bot = Bot.objects.get(id=bot_id)

    last_bot_event = bot.last_bot_event()

    if last_bot_event.event_type not in (BotEventTypes.JOIN_REQUESTED, BotEventTypes.STAGED):
        logger.info(f"Bot {bot_id} is not in JOINING or STAGED state, so not restarting pod")
        return

    # Recovery must use the same infrastructure as the original launch. Forward
    # Docker work to the launcher, the only worker with Docker socket access.
    if os.getenv("LAUNCH_BOT_METHOD") == "docker-compose-multi-host":
        if (self.request.delivery_info or {}).get("routing_key") != "bot_launcher_vm":
            self.apply_async(args=[bot_id], queue="bot_launcher_vm")
            return
        return restart_docker_bot(self, bot, last_bot_event)

    # Initialize kubernetes client
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    v1 = client.CoreV1Api()
    namespace = settings.BOT_POD_NAMESPACE

    # Check if pod already exists with this name
    pod_name = bot.k8s_pod_name()
    try:
        # Directly read the specific pod by name instead of listing all pods
        v1.read_namespaced_pod(name=pod_name, namespace=namespace)

        # Delete the pod if it exists (we'll only get here if the pod exists)
        logger.info(f"Found existing pod {pod_name}, deleting it before creating a new one")
        v1.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=60)

        # Sleep until the pod is no longer found
        num_retries = 20
        for i in range(num_retries):
            try:
                v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            except client.ApiException as e:
                if e.status == 404:
                    logger.info(f"Pod {pod_name} deleted successfully")
                    break
                else:
                    logger.error(f"Error checking for existing pod: {str(e)}")
            if i == num_retries - 1:
                logger.error(f"Pod {pod_name} did not delete after {num_retries} retries")
                raise Exception(f"Pod {pod_name} did not delete after {num_retries} retries")
            time.sleep(5)

    except client.ApiException as e:
        if e.status == 404:
            # Pod doesn't exist - this is fine, just continue
            logger.info(f"Pod {pod_name} not found, no need to delete")
        else:
            # Some other API error occurred
            logger.error(f"Error checking for existing pod: {str(e)}")

    last_bot_event.requested_bot_action_taken_at = None
    if "pod_recreations" not in last_bot_event.metadata:
        last_bot_event.metadata["pod_recreations"] = []
    last_bot_event.metadata["pod_recreations"].append(int(time.time()))
    last_bot_event.save()

    bot.first_heartbeat_timestamp = None
    bot.last_heartbeat_timestamp = None
    bot.save()

    bot_pod_creator = BotPodCreator()
    bot_pod_create_result = bot_pod_creator.create_bot_pod(
        bot_id=bot.id,
        bot_name=bot.k8s_pod_name(),
        bot_cpu_request=bot.cpu_request(),
        add_webpage_streamer=bot.should_launch_webpage_streamer(),
        add_persistent_storage=bot.reserve_additional_storage(),
        bot_pod_spec_type=bot.bot_pod_spec_type,
    )

    logger.info(f"Bot pod create result: {bot_pod_create_result}")


def restart_docker_bot(task, bot, last_bot_event):
    import docker
    from bots.launch_bot_utils import launch_bot

    # Never resurrect an old meeting or retry a delivered restart twice.
    if last_bot_event.metadata.get("docker_restart_task_id") == task.request.id:
        return
    if (bot.join_at or bot.created_at) < timezone.now() - timedelta(minutes=15):
        BotEventManager.create_event(
            bot=bot,
            event_type=BotEventTypes.FATAL_ERROR,
            event_sub_type=BotEventSubTypes.FATAL_ERROR_UI_ELEMENT_NOT_FOUND,
            event_metadata={"bot_restarts_exceeded_max_retries": True},
        )
        return

    docker_client = docker.from_env()
    try:
        try:
            container = docker_client.containers.get(bot.ephemeral_container_name())
        except docker.errors.NotFound:
            container = None
        if container is not None:
            if container.status not in {"exited", "dead"}:
                # A duplicate recovery must never terminate a live assistant.
                logger.info("Bot %s still has a live container; preserving it", bot.id)
                return
            # Keep the previous attempt and its logs, freeing the deterministic
            # launch name. The launcher's existing name check fences duplicates.
            container.rename(f"{bot.ephemeral_container_name()}-restart-{task.request.id}")

        last_bot_event.requested_bot_action_taken_at = None
        last_bot_event.metadata["docker_restart_task_id"] = task.request.id
        last_bot_event.metadata.setdefault("pod_recreations", []).append(int(time.time()))
        last_bot_event.save()
        bot.first_heartbeat_timestamp = None
        bot.last_heartbeat_timestamp = None
        bot.save()
        launch_bot(bot)
    finally:
        docker_client.close()
