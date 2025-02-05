import logging
from unittest.mock import patch, MagicMock

from django.db import connection
from django.test import TestCase

from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider import get_auth_provider
from api_etl.services import ExampleIndividualETLService
from api_etl.sources import ExampleIndividualSource
from core.test_helpers import create_test_interactive_user
from individual.models import Individual
from unittest import skipIf

logger = logging.getLogger(__name__)


MOCKED_RESPONSE_DATA = [
    {
        "status": True,
        "rowCount": 2,
        "rows": [
            {"firstName": "John", "lastName": "Doe", "dateOfBirth": "1990-01-01", "id": 1, "extraField": "value1"},
            {"firstName": "Jane", "lastName": "Smith", "dateOfBirth": "1985-05-15", "id": 2, "extraField": "value2"}
        ],
    },
    {
        "status": True,
        "rowCount": 1,
        "rows": [
            {"firstName": "Alice", "lastName": "Johnson", "dateOfBirth": "2000-12-12", "id": 3, "extraField": "value3"}
        ],
    },
]

class ETLServiceTestCase(TestCase):

    @patch("requests.Session.request")
    @patch("api_etl.apps.ApiEtlConfig.source_batch_size", new=2)
    @patch('individual.services.IndividualConfig.enable_maker_checker_for_individual_upload', False)
    @skipIf(
        connection.vendor != "postgresql",
        "Skipping tests due to individual workflow only supports postgres."
    )
    def test_example_individual_etl_service(self, mock_request):
        mock_request.side_effect = [
            MagicMock(
                ok=True,
                json=MagicMock(return_value=page)
            )
            for page in MOCKED_RESPONSE_DATA
        ]

        initial_count = Individual.objects.count()

        user = create_test_interactive_user(username="test_admin")
        source = ExampleIndividualSource(get_auth_provider('noauth'))
        service = ExampleIndividualETLService(user, source=source)
        service.execute()

        final_count = Individual.objects.count()

        self.assertEqual(final_count - initial_count, 3)
        logging.info(f"Successfully imported {final_count - initial_count} individuals")

        self.assertTrue(Individual.objects.filter(first_name="John", last_name="Doe").exists())
        self.assertTrue(Individual.objects.filter(first_name="Jane", last_name="Smith").exists())
        self.assertTrue(Individual.objects.filter(first_name="Alice", last_name="Johnson").exists())
