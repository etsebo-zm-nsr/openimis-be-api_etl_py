import graphene as graphene

from django.utils.translation import gettext as _
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError

from api_etl.utils import (
    get_class_by_name,
    ETL_CLASS
)
from api_etl.apps import ApiEtlConfig
from core.gql.gql_mutations.base_mutation import BaseMutation
from core.schema import OpenIMISMutation


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

            etl_service_class = get_class_by_name(ETL_CLASS, name_of_service)

            # Instantiate and execute the ETL service
            etl_service = etl_service_class(user)
            result = etl_service.execute()

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
