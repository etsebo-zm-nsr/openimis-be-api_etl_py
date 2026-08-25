"""APScheduler registration for ETL connectors.

openIMIS's `apscheduler_runner` walks `settings.OPENIMIS_APPS` and calls
`<app>.scheduled_tasks.schedule_tasks(scheduler)` for any module that defines it
(precedent: dhis2_etl). Using that hook rather than `settings.SCHEDULER_JOBS` /
`SCHEDULER_CUSTOM` means a connector's schedule ships with the connector and needs no
change to the backend assembly.

Note the scheduler only starts when SCHEDULER_AUTOSTART is set. In the distribution that
variable is exported inside `entrypoint.sh`'s `init()`, which runs in the one-shot
`migrations` container - so the backend container never sees it and no scheduled job
ever fires. The compose file needs it in the shared api environment block.
"""
import logging

logger = logging.getLogger(__name__)


def schedule_tasks(scheduler):
    from apscheduler.triggers.cron import CronTrigger

    from api_etl.registry import list_etl_sources

    for registration in list_etl_sources():
        try:
            config = registration.build_config()
        except Exception as exc:
            logger.error("api_etl: cannot schedule %r - config failed to build: %s",
                         registration.name, exc)
            continue

        if not config.schedule.enabled:
            logger.debug("api_etl: %r has no schedule enabled", registration.name)
            continue
        if not config.schedule.cron:
            logger.warning("api_etl: %r has schedule.enabled but no cron expression",
                           registration.name)
            continue

        job_id = f"api_etl_{registration.name}"
        try:
            scheduler.add_job(
                run_etl_source_job,
                args=[registration.name],
                trigger=CronTrigger(**config.schedule.cron),
                id=job_id,
                # A long sync must never overlap itself. The sync-state lease is the
                # real guard (it also covers manual triggers), this is belt and braces.
                max_instances=1,
                replace_existing=True,
            )
            logger.info("api_etl: scheduled %s with cron=%s", job_id, config.schedule.cron)
        except Exception as exc:
            logger.error("api_etl: failed to schedule %s: %s", job_id, exc, exc_info=exc)


def run_etl_source_job(source_name):
    """Enqueue, do not execute.

    This runs inside the web process's BackgroundScheduler thread, so the actual work is
    handed to a Celery worker. See api_etl/tasks.py for why.
    """
    from api_etl.tasks import run_etl_source
    try:
        run_etl_source.delay(source_name)
        logger.info("api_etl: enqueued scheduled run for %r", source_name)
    except Exception as exc:
        # No broker (or it is down): fall back to running in-thread rather than silently
        # skipping the sync altogether.
        logger.warning("api_etl: could not enqueue %r (%s); running inline", source_name, exc)
        from api_etl.tasks import run_etl_source_sync
        run_etl_source_sync(source_name)
