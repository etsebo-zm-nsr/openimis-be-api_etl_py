import json
from unittest.mock import patch, MagicMock

from django.db import connection
from django.test import TestCase

from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider import get_auth_provider
from api_etl.services import ExampleIndividualETLService
from api_etl.sources import ExampleIndividualSource
from core.test_helpers import LogInHelper
from individual.models import Individual
from unittest import skipIf


MOCKED_RESPONSE_DATA = [
    {
        "status": True,
        "rowCount": 2,
        "rows": [
            {"firstName": "John", "lastName": "Doe Updated", "dateOfBirth": "1990-01-01", "id": 1, "extraField": "value1"},
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
    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api()
        ApiEtlConfig.sink_model_lookup_field = 'json_ext__external_id'
        ApiEtlConfig.sink_update_existing = True

        # Create existing individual in the database
        self.individual = Individual(
            first_name='John',
            last_name='Doe',
            dob='1990-01-01',
            json_ext={'external_id': 1},
        )
        self.individual.save(username=self.user.username)

    @patch("requests.Session.request")
    @patch("api_etl.apps.ApiEtlConfig.source_batch_size", new=2)
    @patch('individual.services.IndividualConfig.enable_maker_checker_for_individual_upload', False)
    @patch('individual.services.IndividualConfig.enable_maker_checker_for_individual_update', False)
    @patch('individual.services.IndividualConfig.individual_schema', json.dumps({
        "properties": {
            "external_id": {"type": "int"}
        }
    }))
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

        source = ExampleIndividualSource(get_auth_provider('noauth'))
        service = ExampleIndividualETLService(self.user, source=source)
        service.execute()

        final_count = Individual.objects.count()

        self.assertEqual(final_count - initial_count, 2)

        self.assertTrue(Individual.objects.filter(first_name="John", last_name="Doe Updated").exists())
        self.assertTrue(Individual.objects.filter(first_name="Jane", last_name="Smith").exists())
        self.assertTrue(Individual.objects.filter(first_name="Alice", last_name="Johnson").exists())
