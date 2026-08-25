"""Celery entry point for ETL runs.

Everything that triggers a sync - the frontend button, APScheduler, a management
command - enqueues THIS task, so there is exactly one execution path to reason about.

Running in a worker rather than in-process matters more than it looks:

  * `core.apps` sets `async_mutations = os.environ.get("ASYNC", os.environ.get("MODE","PROD")) == "PROD"`,
    and the distribution's compose sets `MODE=Prod` - which is not equal to "PROD". So
    GraphQL mutations execute synchronously inside the HTTP request, and a full sync
    would hold a request thread until nginx or waitress timed it out.
  * APScheduler's BackgroundScheduler runs inside the web process, so a scheduled sync
    would occupy a request thread for the same reason.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


def run_etl_source_sync(source_name, user_id=None):
    """Run a registered ETL source in the current process.

    Separated from the Celery task so it can be called directly by tests and by
    management commands without a broker.
    """
    from api_etl.dispatch import build_service
    from api_etl.registry import get_etl_source

    registration = get_etl_source(source_name)
    if registration is None:
        return {"success": False, "message": f"Unknown ETL source {source_name!r}",
                "detail": None, "data": None}

    user = _resolve_user(user_id)
    if user is None:
        return {"success": False, "message": "No user available to run the ETL",
                "detail": f"Configure api_etl system_user_login or pass a user id",
                "data": None}

    service = build_service(source_name, user)
    result = service.execute()
    logger.info("api_etl[%s]: run finished success=%s data=%s",
                source_name, result.get("success"), result.get("data"))
    return result


def _resolve_user(user_id=None):
    """The user a run is attributed to.

    `IndividualImportService.import_individuals` needs a real User for audit columns,
    and the scheduled path has no request user - hence a configurable system login.
    """
    from core.models import User
    from api_etl.apps import ApiEtlConfig

    if user_id:
        user = User.objects.filter(id=user_id).first()
        if user:
            return user
        logger.warning("api_etl: user id %s not found, falling back to the system user", user_id)

    login = getattr(ApiEtlConfig, "system_user_login", None)
    if login:
        user = User.objects.filter(username=login).first()
        if user:
            return user
        logger.error("api_etl: configured system_user_login %r does not exist", login)
    return None


@shared_task(name="api_etl.run_etl_source")
def run_etl_source(source_name, user_id=None):
    return run_etl_source_sync(source_name, user_id)
