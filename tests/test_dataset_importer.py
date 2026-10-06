import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.tools.dataset_importer import (
    DatasetImportError, DuplicateDatasetError, _clean_columns, _parse_file, _prepare_frame,
)
from app.tools.sql_tool import SQLTool
from app.tools.sql_validator import SQLValidationError, validate_read_only_sql


class DatasetImporterTests(unittest.TestCase):
    def test_csv_gbk_and_field_cleaning(self):
        content = '日期,销售额,销售额,Channel Name\n2026-02-01,"￥1,200",,Offline\n'.encode('gbk')
        frame, truncated = _parse_file(content, '销售数据.csv')
        columns, kinds, rows = _prepare_frame(frame)
        self.assertFalse(truncated)
        self.assertEqual(columns, ['日期', '销售额', '销售额_1', 'channel_name'])
        self.assertEqual(kinds, ['DATE', 'NUMERIC', 'TEXT', 'TEXT'])
        self.assertEqual(rows[0], (date(2026, 2, 1), Decimal('1200'), None, 'Offline'))

    def test_chinese_identifiers_and_only_relevant_version_clarification(self):
        columns = _clean_columns([' 订单日期 ', '2026 销售额', '渠道/来源', '销售额' * 30])
        self.assertEqual(columns[:3], ['订单日期', 'column_2_2026_销售额', '渠道_来源'])
        self.assertLessEqual(len(columns[3].encode('utf-8')), 54)

        sql_tool = SQLTool.__new__(SQLTool)
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {'audit_table': 'system_upload_records'}
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        cur.fetchall.return_value = [
            {'table_name': f'user_sales_20260929_{seq:02d}', 'upload_time': now}
            for seq in (1, 2)
        ]
        sql_tool._connect = MagicMock(return_value=conn)
        self.assertIsNone(sql_tool.find_ambiguous_imports('分析库存周转率'))
        self.assertIsNone(sql_tool.find_ambiguous_imports('分析 demo_ecommerce_orders 的销售额'))
        self.assertIn('user_sales_20260929_01', sql_tool.find_ambiguous_imports('分析销售额'))

    def test_xlsx_and_empty_file(self):
        import pandas as pd
        output = BytesIO()
        pd.DataFrame({'Sale Amount': ['$2,345'], 'Date': ['2026/03/02']}).to_excel(
            output, index=False,
        )
        frame, _ = _parse_file(output.getvalue(), 'sales.xlsx')
        columns, kinds, rows = _prepare_frame(frame)
        self.assertEqual(columns, ['sale_amount', 'date'])
        self.assertEqual(kinds, ['NUMERIC', 'DATE'])
        self.assertEqual(rows[0], (Decimal('2345'), date(2026, 3, 2)))
        with self.assertRaises(DatasetImportError):
            _parse_file(b'', 'empty.csv')

    def test_internal_tables_rejected_even_for_readonly_select(self):
        with self.assertRaises(SQLValidationError):
            validate_read_only_sql('SELECT * FROM public.system_upload_records')

    def test_upload_endpoint_maps_duplicate_to_409(self):
        from datetime import datetime, timezone
        importer = MagicMock()
        importer.import_file.side_effect = DuplicateDatasetError(
            'user_sales_20260929_01', datetime(2026, 9, 29, tzinfo=timezone.utc),
        )
        with patch('app.api.analysis._importer', return_value=importer):
            response = TestClient(app).post(
                '/api/v1/datasets/upload',
                files={'file': ('sales.csv', b'a,b\n1,2\n', 'text/csv')},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['table_name'], 'user_sales_20260929_01')


if __name__ == '__main__':
    unittest.main()
