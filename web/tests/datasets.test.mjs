import test from 'node:test';
import assert from 'node:assert/strict';
import {datasetName,formatFileSize,matchesDataset,previewValue,validateDatasetFile} from '../src/datasets.ts';

test('reject invalid files before upload; accept the exact size limit and Excel extensions',()=>{
  assert.match(validateDatasetFile({name:'销售.csv',size:0}),/空/);
  assert.match(validateDatasetFile({name:'sales.csv',size:20*1024*1024+1}),/20 MB/);
  assert.match(validateDatasetFile({name:'report.pdf',size:10}),/仅支持/);
  for(const name of ['销售.csv','SALES.XLSX','旧表.xls']) assert.equal(validateDatasetFile({name,size:20*1024*1024}),'');
});

test('dataset discovery uses filename, physical version and Chinese fields without changing source',()=>{
  const dataset={table_name:'user_sales_20261003_01',original_file_name:'销售.CSV',columns:['订单日期','销售额']};
  assert.equal(datasetName(dataset),'销售.CSV');
  for(const query of ['销售','20261003_01','订单日期',' csv ','']) assert.equal(matchesDataset(dataset,query),true);
  assert.equal(matchesDataset(dataset,'库存'),false);
  assert.equal(dataset.table_name,'user_sales_20261003_01');
});

test('file sizes and preview values distinguish database NULL from zero and empty strings',()=>{
  assert.equal(formatFileSize(1024),'1 KB');
  assert.equal(formatFileSize(2*1024*1024),'2.0 MB');
  assert.equal(previewValue(null),'NULL');
  assert.equal(previewValue(undefined),'—');
  assert.equal(previewValue(0),'0');
  assert.equal(previewValue(false),'false');
  assert.equal(previewValue(''),'');
});
