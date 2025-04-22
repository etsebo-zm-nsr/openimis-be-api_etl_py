from django.test import TestCase
from unittest.mock import patch, MagicMock
from api_etl.sinks.individual_import_sink import IndividualImportSink, IMPORT_NEW_INDIVIDUALS, UPDATE_EXISTING_INDIVIDUALS, WORKFLOW_GROUP
from core.test_helpers import LogInHelper
from individual.models import Individual
from django.core.files.uploadedfile import InMemoryUploadedFile
from api_etl.apps import ApiEtlConfig

class TestIndividualImportSink(TestCase):

    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api()
        ApiEtlConfig.sink_model_lookup_field = 'json_ext__external_id'
        ApiEtlConfig.sink_update_existing = True

        # Create existing individual in the database
        self.individual = Individual(
            first_name='John',
            last_name='Doe',
            dob='1990-01-01',
            json_ext={'external_id': 123},
        )
        self.individual.save(username=self.user.username)

    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_init_successful_workflow(self, mock_get_workflows):
        mock_get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1, 'name': IMPORT_NEW_INDIVIDUALS}]
            }
        }
        sink = IndividualImportSink(self.user)
        self.assertEqual(sink.import_new_workflow['name'], IMPORT_NEW_INDIVIDUALS)
        mock_get_workflows.assert_any_call(IMPORT_NEW_INDIVIDUALS, WORKFLOW_GROUP)
        mock_get_workflows.assert_any_call(UPDATE_EXISTING_INDIVIDUALS, WORKFLOW_GROUP)

    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_init_no_workflow_found(self, mock_get_workflows):
        mock_get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': []
            }
        }
        with self.assertRaises(IndividualImportSink.Error) as context:
            IndividualImportSink(self.user)
        self.assertIn('Workflow not found', str(context.exception))

    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_init_multiple_workflows_found(self, mock_get_workflows):
        mock_get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1}, {'id': 2}]
            }
        }
        with self.assertRaises(IndividualImportSink.Error) as context:
            IndividualImportSink(self.user)
        self.assertIn('Multiple workflows found', str(context.exception))

    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_init_workflow_service_failure(self, mock_get_workflows):
        mock_get_workflows.return_value = {
            'success': False,
            'message': 'Error occurred',
            'details': 'Service unavailable'
        }
        with self.assertRaises(IndividualImportSink.Error) as context:
            IndividualImportSink(self.user)
        self.assertIn('Error occurred: Service unavailable', str(context.exception))

    @patch('api_etl.sinks.individual_import_sink.IndividualImportService.import_individuals')
    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_push_data_with_existing_and_new_records(self, mock_get_workflows, mock_import_individuals):
        mock_get_workflows.side_effect = mock_get_workflow
        mock_import_individuals.return_value = {'imported': 2}

        sink = IndividualImportSink(self.user)
        data = [
            {'external_id': 123, 'name': 'John Doe', 'age': 30},  # Existing record
            {'external_id': 456, 'name': 'Jane Smith', 'age': 25}  # New record
        ]

        sink.push(data)

        # Check if import_individuals was called twice: once for new, once for existing
        self.assertEqual(mock_import_individuals.call_count, 2)

        # Validate new records import
        new_import_file = mock_import_individuals.call_args_list[0][0][0]
        self.assertIsInstance(new_import_file, InMemoryUploadedFile)
        new_import_file.file.seek(0)
        new_content = new_import_file.file.read().decode('utf-8')
        expected_new_csv = 'external_id,name,age\r\n456,Jane Smith,25\r\n'
        self.assertEqual(new_content, expected_new_csv)

        # Validate existing records update
        existing_update_file = mock_import_individuals.call_args_list[1][0][0]
        self.assertIsInstance(existing_update_file, InMemoryUploadedFile)
        existing_update_file.file.seek(0)
        existing_content = existing_update_file.file.read().decode('utf-8')
        expected_existing_csv = f'external_id,name,age,ID\r\n123,John Doe,30,{self.individual.id}\r\n'
        self.assertEqual(existing_content, expected_existing_csv)

    @patch('api_etl.sinks.individual_import_sink.IndividualImportService.import_individuals')
    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_push_data_skip_existing_when_update_disabled(self, mock_get_workflows, mock_import_individuals):
        mock_get_workflows.side_effect = mock_get_workflow
        ApiEtlConfig.sink_update_existing = False  # Disable updating existing records

        sink = IndividualImportSink(self.user)
        data = [
            {'external_id': 123, 'name': 'John Doe', 'age': 30},  # Existing record
            {'external_id': 456, 'name': 'Jane Smith', 'age': 25}  # New record
        ]

        sink.push(data)

        # Only new records should be imported
        self.assertEqual(mock_import_individuals.call_count, 1)
        import_file = mock_import_individuals.call_args[0][0]
        self.assertIsInstance(import_file, InMemoryUploadedFile)
        import_file.file.seek(0)
        content = import_file.file.read().decode('utf-8')
        expected_csv = 'external_id,name,age\r\n456,Jane Smith,25\r\n'
        self.assertEqual(content, expected_csv)

def mock_get_workflow(name, group):
    return {
        'success': True,
        'data': {
            'workflows': [{'id': 1, 'name': name}]
        }
    }

