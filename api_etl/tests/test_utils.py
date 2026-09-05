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


class RaggedBatchTestCase(TestCase):
    """A column present on only SOME rows must still produce a valid CSV.

    Both the sink (`linkage_note`, `linkage_candidate_id`, `alt_external_ids`) and
    connector adapters attach columns to the rows they apply to and no others. Deriving
    the header from `data[0]` made that a hard failure whose occurrence depended on row
    ORDER: identical data succeeded or raised according to which record happened to sort
    first, so a sync could run for weeks and then break on the first flagged record that
    was not at the head of its page.
    """

    def _read(self, data):
        file = data_to_file(data, 'ragged')
        file.file.seek(0)
        return file.file.read().decode('utf-8').splitlines()

    def test_column_appearing_only_on_a_later_row_is_kept(self):
        rows = [
            {'name': 'Alice', 'national_id': '1'},
            {'name': 'Bob', 'national_id': '2', 'linkage_note': 'matched 3 individuals'},
        ]
        lines = self._read(rows)
        self.assertEqual(lines[0], 'name,national_id,linkage_note')
        self.assertEqual(lines[1], 'Alice,1,')          # blank, not dropped
        self.assertEqual(lines[2], 'Bob,2,matched 3 individuals')

    def test_header_does_not_depend_on_row_order(self):
        first = {'name': 'Alice', 'note': 'x'}
        second = {'name': 'Bob'}
        self.assertEqual(
            sorted(self._read([first, second])[0].split(',')),
            sorted(self._read([second, first])[0].split(',')),
        )

    def test_every_row_is_written(self):
        rows = [{'name': 'A'}, {'name': 'B', 'note': 'n'}, {'name': 'C'}]
        self.assertEqual(len(self._read(rows)), 4)      # header + 3

    def test_disjoint_keys_across_rows_all_appear(self):
        rows = [{'a': 1}, {'b': 2}, {'c': 3}]
        self.assertEqual(self._read(rows)[0], 'a,b,c')
