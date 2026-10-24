# Diagnosing repeated assistant joins

Search Coolify logs for `BOT_DIAGNOSTIC`. Each message contains a JSON object.
Existing human-readable messages remain. New diagnostics omit participant names,
meeting URLs/query credentials, captions, and exception messages.

## Correlation

- `meeting_key`: SHA-256 of lowercase URL hostname + pathname without trailing
  slashes (first 24 hexadecimal characters). Query strings are excluded. The app
  and bot service use the same calculation. Different scheduled uses of the same
  room share this key, so also check timestamps.
- `provider_bot_id` and `meeting_bot_id`: remote bot and app database attempt.
- `launcher_task_id`, `launch_id`: Celery delivery and launch invocation. Task
  retries/redeliveries carry `task_retries` and `redelivered`.
- `run_id`, `container`, `pid`: adapter execution. A new runtime gets a new run ID,
  even when the provider bot ID is reused after a restart.
- `attempt`: every browser join loop iteration, including expected exceptions
  that do not increment the provider's retry-policy counter. `elapsed_ms` uses a
  monotonic clock. `browser_ready` records the Chrome session ID.

## Events to investigate

`launcher_reused_container` means a delivery found an existing running container;
this is observed reuse, not proof of a second launch. `container_started` identifies
an actual new container. `launcher_capacity_retry` means capacity delayed launch.

`join_attempt_started` → `browser_ready` → `admission_click_started` →
`admission_click_completed` → `join_succeeded` describes browser/UI progress.
`join_attempt_failed` records exception class, UI step and attempt duration. The
next `join_attempt_started` proves another attempt occurred. A start with
`self_already_in_meeting=true` warns that a UI retry could reload a bot already
admitted. UI success may lag the participant's actual admission.

`self_joined` and `self_left` reflect platform participant transitions, with hashed
`device_key` and cumulative `self_join_count`. Multiple self joins in one run
indicate re-entry or a changed platform identity. `other_assistant_observed` means
another identity has the same normalized display name as this bot.
`duplicate_assistant_detected` warns when two or more such assistant/self identities
are active simultaneously; identical updates do not repeat the warning.
`duplicate_assistant_cleared` marks when the overlap ends. These observations do
not identify the other process's provider; correlate the run and launcher logs.

`admission_failed.reason` distinguishes `host_denied`, `no_response_timeout`, and
`left_meeting_screen`. `removed_from_meeting`, `leave_requested` and
`bot_runtime_cleanup` help explain termination.

## Capture

Use Coolify's server terminal to identify retained containers, including exited
ones, then read the selected bot's complete diagnostic timeline:

```sh
docker ps -a --filter label=attendee.type=ephemeral-bot --format '{{.Names}} {{.Status}}'
docker logs --timestamps <bot-container-name> 2>&1 | grep BOT_DIAGNOSTIC
```

Read `attendee-launcher` logs for launch events. Preserve both timelines with UTC
timestamps and IDs when investigating an incident. Existing isolated deployment
keeps completed containers (`BOT_CONTAINER_AUTO_REMOVE=false`) and rotates each
container's logs at 3 × 10 MB; retention is finite. No retention or recording
settings are changed by this instrumentation.

## Release and verification

This branch builds on `codex/isolated-bot-launcher`, the deployed infrastructure
branch, rather than the older `main`. Deploy the app scheduling logs separately.
Rebuild the Attendee image, deploy this bot-service branch and ensure
`BOT_CONTAINER_IMAGE` points to that rebuilt image for new containers. Existing
bot containers keep their original code. No database migrations are needed.

Offline checks (no real meeting, Docker daemon or database):

```sh
python3 scripts/verify-join-diagnostics.py
```

This exercises the production join loop and admission classifier with fixture
browser calls, participant updates, duplicate warnings, and launcher redelivery.
It does not validate a live Google Meet session. After rollout, look for
`container_started` in the launcher and `bot_runtime_started` in a new bot's logs.
