import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app
from app.tools.dataset_importer import DatasetImporter, DatasetImportError


class DatasetListTests(unittest.TestCase):
    def test_listing_uses_reader_and_bounded_queries(self):
        importer=DatasetImporter('writer','reader')
        connect=MagicMock()
        importer._connect=connect
        cur=connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value={'existing':'system_upload_records'}
        cur.fetchall.side_effect=[[{'table_name':'user_sales','original_file_name':'销售.csv','rows':1}],
                                 [{'column_name':'销售额'}],[{'销售额':12}]]
        rows=importer.list_datasets()
        connect.assert_called_once_with('reader',read_only=True)
        self.assertEqual(rows[0]['columns'],['销售额'])
        self.assertEqual(rows[0]['preview'],[{'销售额':12}])
        statements=[str(call.args[0]) for call in cur.execute.call_args_list]
        self.assertTrue(any('LIMIT 20' in statement for statement in statements))
        self.assertTrue(any('LIMIT 3' in statement for statement in statements))
        self.assertFalse(any('CREATE' in statement or 'INSERT' in statement or 'GRANT' in statement for statement in statements))

    def test_empty_catalog_does_not_create_audit_table(self):
        importer=DatasetImporter('writer','reader')
        importer._connect=MagicMock()
        cur=importer._connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value={'existing':None}
        self.assertEqual(importer.list_datasets(),[])

    def test_api_list_and_friendly_unavailable_error(self):
        with patch('app.api.analysis._importer') as factory:
            factory.return_value.list_datasets.return_value=[]
            response=TestClient(app).get('/api/v1/datasets')
            self.assertEqual(response.json(),{'datasets':[]})
            factory.return_value.list_datasets.side_effect=DatasetImportError('数据库暂不可用')
            response=TestClient(app).get('/api/v1/datasets')
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json()['detail'],'数据库暂不可用')
