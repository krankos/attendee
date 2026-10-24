import logging
import os
import uuid

import docker
from celery import Task, shared_task

from bots.join_diagnostics import bot_log_context, log_diagnostic
from bots.models import Bot, BotEventManager, BotEventSubTypes, BotEventTypes, BotStates

logger = logging.getLogger(__name__)


class BotLauncherCapacityError(Exception):
    """Raised when the maximum number of simultaneous bots is reached."""


class BotLauncherTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        try:
            bot = Bot.objects.get(id=args[0] if args else kwargs["bot_id"])
            if bot.state not in {BotStates.ENDED, BotStates.FATAL_ERROR, BotStates.DATA_DELETED}:
                BotEventManager.create_event(
                    bot=bot,
                    event_type=BotEventTypes.FATAL_ERROR,
                    event_sub_type=BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED,
                )
        except Exception:
            logger.exception("Unable to persist isolated bot launcher failure")


@shared_task(
    bind=True,
    base=BotLauncherTask,
    autoretry_for=(BotLauncherCapacityError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=12,
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_bot_in_ephemeral_container(self, bot_id: int):
    """
    Launches an ephemeral Docker container to execute a bot.
    Timeout is calculated from bot's max_uptime_seconds + 1h margin.
    If max_uptime_seconds is not defined, uses default (4h).
    Container auto-removes on exit.
    Celery task returns in ~2 seconds.
    """
    launch_id = str(uuid.uuid4())
    request = self.request
    diagnostic_context = {
        "bot_id": bot_id,
        "launch_id": launch_id,
        "launcher_task_id": request.id,
        "task_retries": request.retries,
        "redelivered": bool((request.delivery_info or {}).get("redelivered")),
    }
    log_diagnostic("launcher_received", diagnostic_context)
    logger.info(f"Launching ephemeral Docker container for bot {bot_id}")

    try:
        # Connect to Docker daemon
        client = docker.from_env()

        # A launcher restart or lost Docker response may redeliver this task.
        bot = Bot.objects.get(id=bot_id)
        diagnostic_context.update(bot_log_context(bot))
        container_name = bot.ephemeral_container_name()
        try:
            existing = client.containers.get(container_name)
        except docker.errors.NotFound:
            existing = None
        if existing is not None:
            if existing.status in {"created", "running", "restarting"}:
                log_diagnostic("launcher_reused_container", diagnostic_context, level=logging.WARNING, container_id=existing.short_id, container_name=container_name, container_status=existing.status)
                return {"container_id": existing.short_id, "container_name": container_name, "bot_id": bot_id, "status": "already_started"}
            raise RuntimeError("Previous isolated bot container requires inspection before relaunch")

        # Check maximum simultaneous bots limit
        max_simultaneous_bots = int(os.getenv("BOT_MAX_SIMULTANEOUS_BOTS", "100"))
        running_containers = client.containers.list(filters={"label": "attendee.type=ephemeral-bot", "status": "running"})
        current_running_count = len(running_containers)

        if current_running_count >= max_simultaneous_bots:
            error_msg = f"Maximum simultaneous bots limit reached: {current_running_count}/{max_simultaneous_bots}. Cannot launch bot {bot_id}"
            logger.error(error_msg)
            raise BotLauncherCapacityError(error_msg)

        logger.info(f"Current running bots: {current_running_count}/{max_simultaneous_bots}. Launching bot {bot_id}")

        # Image to use (same as worker)
        image = os.getenv("BOT_CONTAINER_IMAGE", "attendee-attendee-worker-local:latest")

        # Copy all environment variables from worker to container
        # This ensures all env vars (DB, Redis, AWS, Deepgram, etc.) are automatically passed
        env_vars = os.environ.copy()

        # Remove Docker-specific or worker-specific vars that shouldn't be in the bot container
        vars_to_exclude = {
            "BOT_CONTAINER_IMAGE",  # Only needed by launcher
            "BOT_MEMORY_LIMIT",  # Only needed by launcher
            "BOT_CPU_QUOTA",  # Only needed by launcher
            "BOT_CPU_PERIOD",  # Only needed by launcher
            "BOT_MAX_EXECUTION_SECONDS",  # Only needed by launcher
            "BOT_MAX_SIMULTANEOUS_BOTS",  # Only needed by launcher
            "PULSE_SERVER",  # Each ephemeral container should start its own PulseAudio server
            "PULSE_RUNTIME_PATH",  # Each container has its own runtime path
            "XDG_RUNTIME_DIR",  # Each container has its own runtime dir
            "HOSTNAME",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
        env_vars = {k: v for k, v in env_vars.items() if k not in vars_to_exclude}
        env_vars["BOT_DIAGNOSTIC_LAUNCH_ID"] = launch_id
        env_vars["BOT_DIAGNOSTIC_TASK_ID"] = request.id or ""

        # Resource limits per bot (configurable)
        mem_limit = os.getenv("BOT_MEMORY_LIMIT", "2g")  # 2GB default
        cpu_quota = int(os.getenv("BOT_CPU_QUOTA", "100000"))  # 1 CPU default (100000 = 1 core)
        cpu_period = int(os.getenv("BOT_CPU_PERIOD", "100000"))

        # Get bot to retrieve max_uptime_seconds (Bot.DoesNotExist propagates)
        automatic_leave_settings = bot.automatic_leave_settings()
        bot_max_uptime = automatic_leave_settings.get("max_uptime_seconds")

        # Container name
        network = os.getenv("BOT_CONTAINER_NETWORK")
        if not network or network == "host":
            raise RuntimeError("BOT_CONTAINER_NETWORK must name the application's private Docker bridge network")

        # Account for memory a running bot has reserved but has not allocated
        # yet, preserving headroom for other applications on this shared host.
        requested_memory = docker.utils.parse_bytes(mem_limit)
        headroom = docker.utils.parse_bytes(os.getenv("BOT_HOST_MEMORY_HEADROOM", "1g"))
        with open("/proc/meminfo", encoding="utf-8") as meminfo:
            available_memory = next(int(line.split()[1]) * 1024 for line in meminfo if line.startswith("MemAvailable:"))
        reserved_memory = 0
        for running in running_containers:
            limit = running.attrs.get("HostConfig", {}).get("Memory", 0)
            usage = running.stats(stream=False).get("memory_stats", {}).get("usage", 0)
            reserved_memory += max(0, limit - usage)
        if available_memory - reserved_memory < requested_memory + headroom:
            raise BotLauncherCapacityError("Insufficient host memory for another isolated bot")

        # Calculate timeout = max_uptime_seconds + 1h (3600s) if defined, otherwise use default
        if bot_max_uptime is not None:
            max_execution_seconds = bot_max_uptime + 3600  # + 1h margin
            logger.info(f"Bot {bot_id} has max_uptime_seconds={bot_max_uptime}, setting container timeout to {max_execution_seconds}s")
        else:
            # No max_uptime defined, use default (4h)
            max_execution_seconds = int(os.getenv("BOT_MAX_EXECUTION_SECONDS", "14400"))
            logger.info(f"Bot {bot_id} has no max_uptime_seconds, using default timeout {max_execution_seconds}s")

        # Command to execute in container with timeout
        # timeout forces stop after max_execution_seconds
        command = f"timeout {max_execution_seconds} python manage.py run_bot --botid {bot_id}"

        # Labels for identification
        labels = {
            "attendee.type": "ephemeral-bot",
            "attendee.bot_id": str(bot_id),
        }

        # Check if we should keep containers for debugging
        auto_remove = os.getenv("BOT_CONTAINER_AUTO_REMOVE", "true").lower() != "false"

        # Mount volumes (same as worker for code access)
        # Get the host path - if we're in a container, use env var or detect from mounted volume
        # The worker runs with .:/attendee mounted, so we need the host path
        # Use the code baked into the selected image. A default host bind mount
        # can hide manage.py entirely on a Coolify server.
        host_code_path = os.getenv("BOT_HOST_CODE_PATH")
        volumes = {host_code_path: {"bind": "/attendee", "mode": "ro"}} if host_code_path else {}

        # Launch ephemeral container
        container = client.containers.run(
            image=image,
            command=command,
            name=container_name,
            detach=True,  # Detached so task returns quickly
            remove=auto_remove,  # Auto-remove on exit (can be disabled for debugging)
            environment=env_vars,
            labels=labels,
            volumes=volumes,
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
            cpu_period=cpu_period,
            network=network,
            shm_size=os.getenv("BOT_SHM_SIZE", "256m"),
            log_config=docker.types.LogConfig(type="json-file", config={"max-size": "10m", "max-file": "3"}),
            security_opt=["seccomp=unconfined"],  # Same config as workers
            # Container will automatically stop after max_execution_seconds thanks to timeout in command
        )

        log_diagnostic("container_started", diagnostic_context, container_id=container.short_id, container_name=container_name, image=image, auto_remove=auto_remove)

        # Log container info and how to view logs
        log_instruction = f"View logs with: docker logs -f {container_name}" if not auto_remove else "Container will auto-remove when done. To keep containers, set BOT_CONTAINER_AUTO_REMOVE=false"

        logger.info(f"Ephemeral container {container_name} (ID: {container.short_id}) started for bot {bot_id}. {log_instruction}")

        # Try to capture and log initial container output (first few lines)
        try:
            # Wait a moment for container to start producing output
            import time

            time.sleep(0.5)
            logs = container.logs(tail=20, stdout=True, stderr=True).decode("utf-8", errors="replace")
            if logs.strip():
                logger.info(f"Initial logs from {container_name}:\n{logs}")
        except Exception as e:
            # If we can't get logs yet, that's okay - container might not have started outputting
            logger.debug(f"Could not retrieve initial logs from {container_name}: {e}")

        # If auto-remove is enabled, start a background task to periodically capture logs
        # This helps see logs even if container is removed
        if auto_remove:
            logger.info(f"💡 Tip: To see full logs, run: sudo docker logs -f {container_name} (while container is running) or set BOT_CONTAINER_AUTO_REMOVE=false to keep containers")

        return {
            "container_id": container.short_id,
            "container_name": container_name,
            "bot_id": bot_id,
            "status": "started",
        }

    except docker.errors.ImageNotFound:
        logger.error(f"Image {image} not found. Cannot launch bot {bot_id}")
        raise
    except docker.errors.APIError as e:
        log_diagnostic("launcher_failed", diagnostic_context, level=logging.ERROR, exception_class=type(e).__name__, docker_status=getattr(e, "status_code", None))
        logger.error(f"Docker API error while launching bot {bot_id}: {str(e)}")
        raise
    except BotLauncherCapacityError:
        log_diagnostic("launcher_capacity_retry", diagnostic_context, level=logging.WARNING)
        raise  # Celery retries capacity exhaustion without declaring failure.
    except Exception as e:
        logger.error(f"Error launching ephemeral container for bot {bot_id}: {str(e)}", exc_info=True)
        raise
