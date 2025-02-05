from api_etl.utils import data_to_file
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.test import TestCase


class TestUtils(TestCase):

    def test_data_to_file_empty_data(self):
        with self.assertRaises(ValueError) as context:
            data_to_file([])
        self.assertIn('The data is empty and cannot be written to a file.', str(context.exception))

    def test_data_to_file_valid_data(self):
        data = [{'name': 'Alice', 'age': 28}, {'name': 'Bob', 'age': 34}]
        file = data_to_file(data, 'test_data')

        self.assertIsInstance(file, InMemoryUploadedFile)
        self.assertEqual(file.name, 'test_data.csv')
        self.assertEqual(file.content_type, 'text/csv')

        file.file.seek(0)
        content = file.file.read().decode('utf-8')
        expected_csv = 'name,age\r\nAlice,28\r\nBob,34\r\n'
        self.assertEqual(content, expected_csv)
