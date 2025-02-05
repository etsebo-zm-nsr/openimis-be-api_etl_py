from django.test import TestCase
from unittest.mock import patch, MagicMock
from api_etl.sinks.individual_import_sink import IndividualImportSink, WORKFLOW_NAME, WORKFLOW_GROUP
from core.models import User
from django.core.files.uploadedfile import InMemoryUploadedFile


class TestIndividualImportSink(TestCase):

    def setUp(self):
        self.mock_user = MagicMock(spec=User)

    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_init_successful_workflow(self, mock_get_workflows):
        mock_get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1, 'name': WORKFLOW_NAME}]
            }
        }
        sink = IndividualImportSink(self.mock_user)
        self.assertEqual(sink.workflow['name'], WORKFLOW_NAME)
        mock_get_workflows.assert_called_once_with(WORKFLOW_NAME, WORKFLOW_GROUP)

    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_init_no_workflow_found(self, mock_get_workflows):
        mock_get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': []
            }
        }
        with self.assertRaises(IndividualImportSink.Error) as context:
            IndividualImportSink(self.mock_user)
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
            IndividualImportSink(self.mock_user)
        self.assertIn('Multiple workflows found', str(context.exception))

    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_init_workflow_service_failure(self, mock_get_workflows):
        mock_get_workflows.return_value = {
            'success': False,
            'message': 'Error occurred',
            'details': 'Service unavailable'
        }
        with self.assertRaises(IndividualImportSink.Error) as context:
            IndividualImportSink(self.mock_user)
        self.assertIn('Error occurred: Service unavailable', str(context.exception))

    @patch('api_etl.sinks.individual_import_sink.IndividualImportService.import_individuals')
    @patch('api_etl.sinks.individual_import_sink.WorkflowService.get_workflows')
    def test_push_data(self, mock_get_workflows, mock_import_individuals):
        mock_get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1, 'name': WORKFLOW_NAME}]
            }
        }
        mock_import_individuals.return_value = {'imported': 5}

        sink = IndividualImportSink(self.mock_user)
        data = [{'name': 'John Doe', 'age': 30}, {'name': 'Jane Smith', 'age': 25}]

        sink.push(data)

        mock_import_individuals.assert_called_once()
        import_file = mock_import_individuals.call_args[0][0]
        self.assertIsInstance(import_file, InMemoryUploadedFile)

        # Verify the content of the generated file
        import_file.file.seek(0)
        content = import_file.file.read().decode('utf-8')
        expected_csv = 'name,age\r\nJohn Doe,30\r\nJane Smith,25\r\n'
        self.assertEqual(content, expected_csv)
