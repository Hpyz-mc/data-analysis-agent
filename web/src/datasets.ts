import type {Dataset} from './api';

export type ImportReceipt = {dataset:Dataset;truncated:boolean};
export type DuplicateImport = {table_name:string;message:string};

export function datasetName(dataset:Dataset) {
  return dataset.original_file_name || dataset.table_name;
}

export function formatFileSize(bytes:number) {
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.max(1, Math.ceil(bytes / 1024))} KB`;
}

export function validateDatasetFile(file:Pick<File,'name'|'size'>) {
  if (!/\.(csv|xlsx|xls)$/i.test(file.name)) return '仅支持 CSV、XLSX 和 XLS 文件。';
  if (file.size === 0) return '这个文件是空的，请选择包含数据的表格。';
  if (file.size > 20 * 1024 * 1024) return '文件超过 20 MB，请拆分或缩小文件后再上传。';
  return '';
}

export function matchesDataset(dataset:Dataset,query:string) {
  const keyword = query.trim().toLocaleLowerCase();
  return !keyword || [datasetName(dataset),dataset.table_name,...dataset.columns]
    .some(value=>value.toLocaleLowerCase().includes(keyword));
}

export function previewValue(value:unknown) {
  return value === null ? 'NULL' : value === undefined ? '—' : String(value);
}
