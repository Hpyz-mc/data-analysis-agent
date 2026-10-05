import {useRef,useState} from 'react';
import type {Dataset} from './api';
import {datasetName,formatFileSize,matchesDataset,previewValue} from './datasets';
import type {DuplicateImport,ImportReceipt} from './datasets';
import {Icon} from './icons';

type Props = {
  datasets:Dataset[];
  selected:string;
  file:File|null;
  busy:boolean;
  refreshing:boolean;
  catalogError:string;
  error:string;
  duplicate:DuplicateImport|null;
  receipt:ImportReceipt|null;
  hasConversation:boolean;
  onFile:(file:File|null,error?:string)=>void;
  onUpload:()=>void;
  onChoose:(tableName:string)=>void;
  onRefresh:()=>void;
};

const uploadTime=(value?:string)=>value
  ? new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
  : '时间未记录';

function Preview({dataset}:{dataset:Dataset}) {
  return <details className="dataset-preview" open>
    <summary>数据预览<span>最多 3 行 · 仅用于核对字段</span></summary>
    <div className="table-scroll" tabIndex={0} aria-label={`${datasetName(dataset)}的数据预览，可横向滚动`}>
      <table>
        <caption className="sr-only">{datasetName(dataset)}数据库预览，最多 3 行</caption>
        <thead><tr>{dataset.columns.map(column=><th scope="col" key={column}>{column}</th>)}</tr></thead>
        <tbody>{dataset.preview.slice(0,3).map((row,index)=><tr key={index}>
          {dataset.columns.map(column=><td key={column} title={previewValue(row[column])}>
            <span className={row[column]===null?'null-value':''}>{previewValue(row[column])}</span>
          </td>)}
        </tr>)}</tbody>
      </table>
    </div>
    {!dataset.preview.length&&<p className="muted">暂时没有可显示的预览行。</p>}
  </details>;
}

export function DatasetsPanel(props:Props) {
  const {datasets,selected,file,busy,receipt,duplicate}=props;
  const input=useRef<HTMLInputElement>(null);
  const [query,setQuery]=useState('');
  const [dragging,setDragging]=useState(false);
  const current=datasets.find(dataset=>dataset.table_name===selected);
  const visible=datasets.filter(dataset=>matchesDataset(dataset,query));
  function pickFile(value:File|null) {
    if (busy) return;
    props.onFile(value);
    if (input.current) input.current.value='';
  }

  return <div className="datasets-page">
    <div className="eyebrow">数据准备</div><h1>数据集管理</h1>
    <p className="muted">上传表格、核对字段，选择一份明确的数据开始分析。</p>

    {selected&&<div className="dataset-current">
      <span className="file-icon"><Icon name="database" size={21}/></span>
      <div><span className="current-label">当前选中的数据</span>
        <strong title={current?datasetName(current):selected}>{current?datasetName(current):selected}</strong>
        <small>{current?`${current.rows.toLocaleString()} 行 · ${current.columns.length} 列`:'来自当前会话的分析范围'} · <code>public.{selected}</code></small>
      </div><span className="selected-pill"><Icon name="check" size={13}/>已选择</span>
    </div>}

    <section className={`dataset-upload ${dragging&&!busy?'dragging':''}`} aria-labelledby="dataset-upload-title"
      onDragOver={event=>{event.preventDefault();if(!busy)setDragging(true)}}
      onDragLeave={event=>{if(!event.currentTarget.contains(event.relatedTarget as Node|null))setDragging(false)}}
      onDrop={event=>{event.preventDefault();setDragging(false);if(!busy){if(event.dataTransfer.files.length>1){props.onFile(null,'每次只导入一个文件，请单独选择一份表格。');return}pickFile(event.dataTransfer.files[0]||null)}}}>
      <div className="upload-intro"><span className="upload-symbol"><Icon name="upload" size={24}/></span>
        <div><h2 id="dataset-upload-title">上传你的数据</h2><p>拖入表格，或选择文件后确认导入。</p></div>
        <button className="outline-button" disabled={busy} onClick={()=>input.current?.click()}>{file?'更换文件':'选择文件'}</button>
      </div>
      <input ref={input} type="file" accept=".csv,.xlsx,.xls" hidden disabled={busy} onChange={event=>pickFile(event.target.files?.[0]||null)}/>
      <div className="upload-limits"><span>CSV / Excel</span><span>最大 20 MB</span><span>最多导入前 50,000 行</span></div>
      {file&&<div className="upload-file"><Icon name="file" size={22}/>
        <div><strong title={file.name}>{file.name}</strong><small>{formatFileSize(file.size)} · {busy?'正在上传并导入，请稍候':'已选择，确认后才会上传'}</small></div>
        <button className="icon-button" aria-label="移除待上传文件" disabled={busy} onClick={()=>pickFile(null)}><Icon name="close" size={16}/></button>
        <button className="primary-button" disabled={busy} onClick={props.onUpload}>{busy?'导入中…':'确认导入'}</button>
      </div>}
      {busy&&<div className="import-progress" role="status"><span className="pulse"/>正在处理文件并保存到数据库，请勿重复提交。完成后会显示真实行列数。</div>}
      {props.error&&<div className="import-error" role="alert">{props.error}</div>}
      <p className="upload-privacy">导入不调用模型。数据以独立版本保存，不覆盖旧表；开始分析时，查询结果会发送到模型服务。</p>
    </section>

    {duplicate&&<div className="import-result duplicate-result" role="status">
      <Icon name="database" size={21}/><div><h3>这份数据已经导入，无需重复上传</h3><p>{duplicate.message}</p><code>{duplicate.table_name}</code></div>
      <button className="outline-button" disabled={busy} onClick={()=>props.onChoose(duplicate.table_name)}>使用已有数据分析 ↗</button>
    </div>}
    {receipt&&<div className="import-result success-result" role="status">
      <Icon name="check" size={21}/><div><h3>导入成功，数据已可用于分析</h3>
        <p>{datasetName(receipt.dataset)} · {receipt.dataset.rows.toLocaleString()} 行 · {receipt.dataset.columns.length} 列</p>
        <code>{receipt.dataset.table_name}</code>
        {receipt.truncated&&<p className="import-warning">文件超过行数上限，只保存了前 50,000 行；分析不会覆盖未导入部分。</p>}
      </div><button className="primary-button" disabled={busy} onClick={()=>props.onChoose(receipt.dataset.table_name)}>开始分析 ↗</button>
    </div>}

    <div className="dataset-list-heading"><div><h2>已导入的数据</h2><p>最近登记的 {datasets.length} 份 · 最多显示 20 份</p></div>
      <button className="outline-button" disabled={busy||props.refreshing} onClick={props.onRefresh}>{props.refreshing?'刷新中…':'刷新列表'}</button>
    </div>
    <label className="dataset-search"><Icon name="search" size={16}/><input aria-label="搜索数据集" placeholder="搜索文件名、表名或字段" value={query} onChange={event=>setQuery(event.target.value)}/></label>
    {props.hasConversation&&<p className="dataset-switch-note">选择数据开始分析会开启新会话，原来的对话仍保留在左侧历史中。</p>}
    {props.catalogError&&<div className="import-error" role="alert">列表读取失败：{props.catalogError}。可点击“刷新列表”重试，已有数据不会被删除。</div>}
    {props.refreshing&&!datasets.length?<p className="muted" role="status">正在读取数据集…</p>
      :!props.catalogError&&!datasets.length?<div className="dataset-empty"><Icon name="database" size={25}/><h3>还没有导入数据</h3><p>从上方上传一份 CSV 或 Excel，导入后即可预览并开始分析。</p></div>
      :datasets.length>0&&!visible.length?<p className="muted">没有匹配的数据集，请换一个文件名或字段关键词。</p>:null}
    {visible.map(dataset=><article className={`dataset-card ${selected===dataset.table_name?'dataset-selected':''}`} key={dataset.table_name}>
      <div className="dataset-card-head"><span className="file-icon"><Icon name="database" size={23}/></span>
        <div><h3 title={datasetName(dataset)}>{datasetName(dataset)}</h3><p>导入时间 · {uploadTime(dataset.upload_time)}</p></div>
        <span className={selected===dataset.table_name?'selected-pill':'version'}>{selected===dataset.table_name?'当前选择':'独立版本'}</span>
      </div>
      <div className="dataset-meta"><span><strong>{dataset.rows.toLocaleString()}</strong> 行</span><span><strong>{dataset.columns.length}</strong> 列</span><span>表名 <code>{dataset.table_name}</code></span></div>
      <Preview dataset={dataset}/>
      <div className="dataset-footer"><span>预览不是完整数据，分析会重新查询数据库。</span><button className="primary-button" disabled={busy} onClick={()=>props.onChoose(dataset.table_name)}>{selected===dataset.table_name?'用这份数据开始分析':'选择并开始分析'} ↗</button></div>
    </article>)}
  </div>;
}
