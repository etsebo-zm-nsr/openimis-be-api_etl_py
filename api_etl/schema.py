import graphene

from api_etl.apps import ApiEtlConfig
from api_etl.dispatch import available_service_names, resolve_service
from api_etl.gql_queries import (
    ETLServicesGQLType,
    ETLServicesListGQLType,
)
from api_etl.gql_mutations import ETLServiceMutation


class Query(graphene.ObjectType):

    etl_services_by_service_name = graphene.Field(
        ETLServicesListGQLType,
        name_of_service=graphene.Argument(graphene.String, required=False),
    )

    def resolve_etl_services_by_service_name(parent, info, **kwargs):
        if not info.context.user.has_perms(ApiEtlConfig.gql_query_api_etl_rule_perms):
            raise PermissionError("Unauthorized")

        service_name = kwargs.get("name_of_service", None)
        if service_name:
            # Registered connectors resolve by their registry name; services defined
            # inside api_etl still resolve by class name.
            service_cls, _ = resolve_service(service_name)
            names = [service_name] if service_cls else []
        else:
            names = available_service_names()

        return ETLServicesListGQLType(
            [ETLServicesGQLType(name_of_service=name) for name in names]
        )


class Mutation(graphene.ObjectType):
    etl_service_mutation = ETLServiceMutation.Field()
