import logging

import graphene as graphene

from django.utils.translation import gettext as _
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError

from api_etl.apps import ApiEtlConfig
from api_etl.dispatch import build_service, resolve_service
from core.gql.gql_mutations.base_mutation import BaseMutation
from core.schema import OpenIMISMutation

logger = logging.getLogger(__name__)


class ETLServiceMutation(BaseMutation):
    """
    Mutation to execute the ETLService
    """
    _mutation_class = "ETLServiceMutation"
    _mutation_module = "api_etl"

    class Input(OpenIMISMutation.Input):
        name_of_service = graphene.String(required=True)

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise ValidationError("mutation.authentication_required")

        # A connector may declare its own trigger right, so access can be granted per
        # source rather than "may trigger every ETL in the system".
        name_of_service = data.get('name_of_service')
        if name_of_service:
            _, registration = resolve_service(name_of_service)
            if registration is not None and registration.trigger_perms:
                if not user.has_perms(registration.trigger_perms):
                    raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        try:
            data.pop('client_mutation_id', None)
            data.pop('client_mutation_label', None)
            name_of_service = data.pop('name_of_service', None)
            if not name_of_service:
                return [{
                    'message': "api_etl.mutation.failed_to_execute_etl_service",
                    'detail': _('There is no ETL service with provided name')
                }]

            result = cls._run(name_of_service, user)

            if result['success']:
                return None
            else:
                return [{
                    'message': result['message'],
                    'detail': result['detail']
                }]
        except Exception as exc:
            return [{
                'message': "api_etl.mutation.failed_to_execute_etl_service",
                'detail': str(exc)
            }]

    @classmethod
    def _run(cls, name_of_service, user):
        """Hand the run to a Celery worker so the FE button and cron share one path.

        A full sync can take minutes; executing it inline would hold the HTTP request
        open (mutations are synchronous here because MODE=Prod != "PROD"). Falls back to
        running inline if there is no reachable broker, so a misconfigured deployment
        degrades to "slow" rather than "silently does nothing".
        """
        from api_etl.tasks import run_etl_source, run_etl_source_sync

        if not ApiEtlConfig.run_async:
            return run_etl_source_sync(name_of_service, getattr(user, "id", None))

        try:
            run_etl_source.delay(name_of_service, str(getattr(user, "id", "")) or None)
            return {'success': True, 'message': None, 'detail': None, 'data': {'queued': True}}
        except Exception as exc:
            logger.warning("api_etl: could not enqueue %r (%s); running inline",
                           name_of_service, exc)
            return run_etl_source_sync(name_of_service, getattr(user, "id", None))
