import {useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {ReportContent} from './ReportContent';
import {Icon} from './icons';
import {DatasetsPanel} from './DatasetsPanel';
import {RunStatus,FailureNotice} from './RunStatus';
import {failureInfo,answerLabel,canShowArtifacts} from './chatStatus';
import {datasetName,validateDatasetFile} from './datasets';
import type {DuplicateImport,ImportReceipt} from './datasets';
import {APIError,analyzeStream,displayQuestion,listDatasets,listSessions,request} from './api';
import type {Dataset,Message,Result,Scope,Session} from './api';
import './styles.css';
import './live.css';
import './refinements.css';
import './datasets.css';
import './chatStatus.css';

const formatTime=(value:string)=>new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const errorText=(error:unknown)=>error instanceof Error?error.message:'操作暂时未完成，请重试。';
const ids=()=>crypto.randomUUID();
type Turn={turn_no:number;question:string;answer:string|null;status:string;charts?:string[];suggestions?:Result['suggestions'];analysis_context?:Scope|null;session_context?:Scope|null;context_reset?:boolean};

function scopeFromTurns(turns:Turn[]){let scope:Scope|null=null;for(const turn of turns){if(turn.context_reset)scope=null;else if(turn.session_context||turn.analysis_context)scope=turn.session_context||turn.analysis_context||null}return scope}
function chartPaths(message:Message){return message.result?.charts||message.charts||[]}
function summaryMetrics(result?:Result){if(!result)return [];for(const step of [...result.steps].reverse()){try{const obj=JSON.parse(step.observation);if(step.action==='python'&&obj.operation==='attribution'&&obj.result?.reconciled){const r=obj.result;return [{label:'基期 / 基准总额',value:r.baseline_total},{label:'当期 / 对比总额',value:r.current_total},{label:'净变化',value:r.total_change}]}}catch{/* A failed tool is not evidence. */}}return []}

function App(){
 const [view,setView]=useState<'chat'|'datasets'>('chat');
 const [nav,setNav]=useState(false);
 const [sessions,setSessions]=useState<Session[]>([]);
 const [datasets,setDatasets]=useState<Dataset[]>([]);
 const [session,setSession]=useState<string|null>(null);
 const [selected,setSelected]=useState<string>('');
 const [messages,setMessages]=useState<Message[]>([]);
 const [scope,setScope]=useState<Scope|null>(null);
 const [memory,setMemory]=useState<Scope|null>(null);
 const [search,setSearch]=useState('');
 const [draft,setDraft]=useState('');
 const [busy,setBusy]=useState(false);
 const [loading,setLoading]=useState(false);
 const [dataBusy,setDataBusy]=useState(false);
 const [toast,setToast]=useState('');
 const [error,setError]=useState('');
 const [startupError,setStartupError]=useState('');
 const [progress,setProgress]=useState('');
 const [expanded,setExpanded]=useState<string|null>(null);
 const [pendingDelete,setPendingDelete]=useState<Session|null>(null);
 const [deleting,setDeleting]=useState(false);
 const [file,setFile]=useState<File|null>(null);
 const [duplicate,setDuplicate]=useState<DuplicateImport|null>(null);
 const [receipt,setReceipt]=useState<ImportReceipt|null>(null);
 const [dataError,setDataError]=useState('');
 const [catalogError,setCatalogError]=useState('');
 const [catalogRefreshing,setCatalogRefreshing]=useState(true);
 const refreshEpoch=useRef(0);
 const contentRef=useRef<HTMLDivElement>(null);
 const abort=useRef<AbortController|null>(null);
 const inputRef=useRef<HTMLTextAreaElement>(null);
 const loadEpoch=useRef(0);
 const busyRef=useRef(false);
 const selectedData=datasets.find(d=>d.table_name===selected);
 const activeScope=scope||memory;
 async function refresh(){
  const epoch=++refreshEpoch.current;
  setCatalogRefreshing(true);
  const results=await Promise.allSettled([listSessions(),listDatasets()]);
  if(epoch!==refreshEpoch.current)return;
  const errors:string[]=[];
  if(results[0].status==='fulfilled')setSessions(results[0].value.sessions);
  else errors.push('会话列表：'+errorText(results[0].reason));
  if(results[1].status==='fulfilled'){
   setDatasets(results[1].value.datasets.filter(d=>!['demo_ecommerce_orders','agent_sales_sample'].includes(d.table_name)));
   setCatalogError('');
  }else{
   const message=errorText(results[1].reason);
   setCatalogError(message);errors.push('数据集列表：'+message);
  }
  setStartupError(errors.join('；'));setCatalogRefreshing(false);
 }
 useEffect(()=>{void refresh();return()=>{abort.current?.abort();loadEpoch.current++;refreshEpoch.current++}},[]);
 useEffect(()=>{if(!toast)return;const t=setTimeout(()=>setToast(''),4500);return()=>clearTimeout(t)},[toast]);
 useEffect(()=>{if(busy)contentRef.current?.scrollTo({top:contentRef.current.scrollHeight,behavior:'smooth'})},[messages,busy,progress]);
 function newChat(){if(busyRef.current||dataBusy)return;loadEpoch.current++;setSession(null);setMessages([]);setScope(null);setMemory(null);setError('');setDraft('');setLoading(false);setView('chat');setNav(false)}
 async function openSession(item:Session){
  if(busyRef.current||dataBusy)return;
  const epoch=++loadEpoch.current;setLoading(true);setError('');setNav(false);
  try{
   const history=await request<{turns:Turn[]}>('/api/v1/sessions/'+item.session_id);
   if(epoch!==loadEpoch.current)return;
   const current=scopeFromTurns(history.turns);
   setSession(item.session_id);
   setMessages(history.turns.flatMap(turn=>{
    const unfinished=turn.status==='failed'||turn.status==='running';
    const source=(turn.session_context||turn.analysis_context||current)?.source_table.replace(/^public\./,'')||turn.question.match(/^使用数据表 public\.([\p{L}\p{N}_]+)。/u)?.[1]||'';
    return [{id:`${turn.turn_no}-u`,role:'user' as const,content:displayQuestion(turn.question)},
     {id:`${turn.turn_no}-a`,role:'assistant' as const,content:turn.answer||'',status:unfinished?'failed':turn.status,
      charts:turn.charts,suggestions:turn.suggestions,context:turn.analysis_context,
      ...(unfinished?{failure:turn.status==='running'?failureInfo(new APIError(409,'历史记录显示本轮尚未结束。')):failureInfo(new Error('历史记录显示本轮失败，未保存成功回答。')),retry:{question:displayQuestion(turn.question),sourceTable:source}}:{})}];
   }));
   setMemory(current);setScope(history.turns.at(-1)?.analysis_context||null);
   setSelected((history.turns.at(-1)?.analysis_context||current)?.source_table.replace(/^public\./,'')||'');
   setView('chat');setDraft('');
  }catch(error){if(epoch===loadEpoch.current)setError(errorText(error))}
  finally{if(epoch===loadEpoch.current)setLoading(false)}
 }
 function prepareRetry(message:Message){
  if(busyRef.current||loading||dataBusy||!message.retry)return;
  if(message.failure?.kind==='missing')newChat();
  setSelected(message.retry.sourceTable);setDraft(message.retry.question);
  inputRef.current?.focus();
 }
 function latestHistory(){
  if(!session)return;
  void openSession(sessions.find(item=>item.session_id===session)||{session_id:session,title:'当前分析',created_at:'',updated_at:''});
 }
 async function send(text=draft){if(busyRef.current||loading||dataBusy||!text.trim())return;if(text.trim().length<2||text.trim().length>2000){setError('问题需要 2–2000 个字。');return}busyRef.current=true;setBusy(true);setError('');setView('chat');setProgress('正在连接分析服务…');loadEpoch.current++;
  const controller=new AbortController();abort.current=controller;const assistantId=ids();let question=text.trim();if(selected&&(!session||activeScope?.source_table.replace(/^public\./,'')!==selected))question=`使用数据表 public.${selected}。${question}`;
  setDraft('');setMessages(items=>[...items,{id:ids(),role:'user',content:text.trim()},{id:assistantId,role:'assistant',content:'',status:'running',retry:{question:text.trim(),sourceTable:selected||activeScope?.source_table.replace(/^public\./,'')||''}}]);
  try{
   await analyzeStream(question,session,(event,data)=>{
    if(event==='session')setSession(data.session_id);
    if(event==='status')setProgress(data.message||'正在分析…');
    if(event==='delta'&&typeof data.text==='string')setMessages(items=>items.map(m=>m.id===assistantId?{...m,content:m.content+data.text}:m));
    if(event==='result'){
     const result=data as Result;
     setSession(result.session_id);setScope(result.analysis_context||null);
     if(result.context_reset)setMemory(null);else setMemory(result.session_context||result.analysis_context||memory);
     if(result.analysis_context)setSelected(result.analysis_context.source_table.replace(/^public\./,''));
     setMessages(items=>items.map(m=>m.id===assistantId?{...m,content:result.answer,result,status:result.finished?'completed':result.requires_clarification?'clarification':'incomplete'}:m));
    }
   },controller.signal);
  }catch(error){
   const stopped=controller.signal.aborted;
   setMessages(items=>items.map(m=>m.id===assistantId?{...m,status:stopped?'stopped':'failed',failure:failureInfo(error,stopped),result:undefined,charts:[],suggestions:[]}:m));
  }finally{
   busyRef.current=false;setBusy(false);abort.current=null;setProgress('');
   await refresh();
  }
 }
 async function removeSession(){if(!pendingDelete||deleting)return;setDeleting(true);try{await request('/api/v1/sessions/'+pendingDelete.session_id,{method:'DELETE'});if(session===pendingDelete.session_id)newChat();setSessions(items=>items.filter(s=>s.session_id!==pendingDelete.session_id));setPendingDelete(null);setToast('会话及其聊天记录已删除，无法从页面恢复。')}catch(error){setError(errorText(error))}finally{setDeleting(false)}}
 function chooseDataset(tableName:string){
  if(busyRef.current||dataBusy||!tableName)return;
  newChat();setSelected(tableName);setDuplicate(null);setDataError('');
  const dataset=datasets.find(item=>item.table_name===tableName);
  setToast(`已选择 ${dataset?datasetName(dataset):tableName}，新分析将使用这份数据。`);
 }
 async function upload(){
  if(!file||dataBusy)return;
  const issue=validateDatasetFile(file);if(issue){setDataError(issue);return}
  const uploadingFile=file;
  setDataBusy(true);setDataError('');setDuplicate(null);setReceipt(null);
  const form=new FormData();form.append('file',uploadingFile);if(session)form.append('session_id',session);
  try{
   const data=await request<Dataset&{truncated?:boolean}>('/api/v1/datasets/upload',{method:'POST',body:form});
   const imported:Dataset={...data,original_file_name:uploadingFile.name};
   setReceipt({dataset:imported,truncated:!!data.truncated});setSelected(data.table_name);setFile(null);
   await refresh();
   // Keep the confirmed import visible even if refreshing the catalog failed.
   setDatasets(items=>[items.find(item=>item.table_name===imported.table_name)||imported,...items.filter(item=>item.table_name!==imported.table_name)]);
   setToast('导入成功，可以核对预览后开始分析。');
  }catch(error){
   if(error instanceof APIError&&error.status===409&&typeof error.detail?.table_name==='string'){
    setDuplicate({table_name:error.detail.table_name,message:error.message});await refresh();
   }else setDataError(errorText(error));
  }finally{setDataBusy(false)}
 }
 function selectFile(value:File|null,errorMessage=''){
  if(dataBusy)return;
  const issue=errorMessage||(value?validateDatasetFile(value):'');
  setDataError(issue);setFile(issue?null:value);setDuplicate(null);
  if(value&&!issue)setReceipt(null);
 }
 const title=view==='datasets'?'数据集管理':sessions.find(s=>s.session_id===session)?.title|| (messages.length?(busy?'分析进行中':'当前分析'):'新建分析');
 return <div className="app">
  {nav&&<div className="nav-scrim" onClick={()=>setNav(false)}/>}
  <nav className={`sidebar ${nav?'open':''}`}><div className="brand"><span className="brand-icon"><Icon name="spark" size={23}/></span><div>洞察<span>DATA ANALYSIS AGENT</span></div><button className="icon-button mobile-only" aria-label="关闭导航" onClick={()=>setNav(false)}><Icon name="close"/></button></div><button className="new-chat" disabled={busy||dataBusy} onClick={newChat}><Icon name="plus"/>新建分析<span>↵</span></button><button className={`nav-item ${view==='datasets'?'selected':''}`} disabled={busy||dataBusy} onClick={()=>{setView('datasets');setNav(false)}}><Icon name="database"/>数据集管理<span className="count">{datasets.length}</span></button><div className="nav-label">分析记录<span>{sessions.length}</span></div><label className="search"><Icon name="search" size={15}/><input aria-label="搜索历史分析" placeholder="搜索分析记录" value={search} onChange={e=>setSearch(e.target.value)}/></label><div className="history">{sessions.filter(s=>(s.question_preview||s.title).includes(search)).map(s=><div key={s.session_id} className={`session ${s.session_id===session&&view==='chat'?'active':''}`}><button className="session-open" title={s.question_preview||s.title} disabled={busy||dataBusy} onClick={()=>void openSession(s)}><Icon name="chat" size={16}/><span>{s.title}<small>{formatTime(s.created_at)}</small></span></button><button className="delete" disabled={busy||dataBusy} aria-label={`删除${s.title}`} onClick={()=>setPendingDelete(s)}><Icon name="trash" size={14}/></button></div>)}{sessions.length===0&&<p className="empty-search">还没有分析记录<br/>发送第一个问题后自动保存。</p>}{search&&!sessions.some(s=>(s.question_preview||s.title).includes(search))&&<p className="empty-search">没有匹配的分析记录</p>}</div><div className="sidebar-bottom"><div className="sandbox-note"><span className="status-dot"/>本机部署 · 模型远程调用</div><div className="profile"><span className="avatar">我</span><div>本地工作空间<small>只读查询 · 证据可核对</small></div><span className="local-tag">LOCAL</span></div></div></nav>
  <section className={`workspace ${view==='chat'&&!messages.length&&!loading?'is-empty':''}`}><header><div className="breadcrumb"><button className="icon-button mobile-only" onClick={()=>setNav(true)} aria-label="展开导航"><Icon name="panel"/></button>工作空间<span>/</span><strong title={sessions.find(s=>s.session_id===session)?.question_preview||title}>{title}</strong></div><div className="header-actions"><span className="demo-badge">{busy?'正在分析':'数据分析工作台'}</span></div></header>
   {startupError&&<div className="error-banner" role="alert">{startupError}<button onClick={()=>void refresh()}>重新连接</button></div>}{error&&<div className="error-banner" role="alert">{error}<button aria-label="关闭错误提示" onClick={()=>setError('')}><Icon name="close" size={14}/></button></div>}
   <div className="content" ref={contentRef}>{view==='datasets'?<DatasetsPanel datasets={datasets} selected={selected} file={file} busy={dataBusy} refreshing={catalogRefreshing} catalogError={catalogError} error={dataError} duplicate={duplicate} receipt={receipt} hasConversation={!!session||messages.length>0} onFile={selectFile} onUpload={()=>void upload()} onChoose={chooseDataset} onRefresh={()=>void refresh()}/>:loading?<div className="loading-state">正在读取会话历史…</div>:messages.length===0?<div className="welcome"><span className="eyebrow">从问题出发，让数据给出答案</span><h1>今天，想探索什么？</h1><p>选择或上传数据，然后输入你的分析问题。</p><button className="dataset-chip" onClick={()=>setView('datasets')}><Icon name="database"/><span className="welcome-source-name">{selectedData?datasetName(selectedData):selected||'选择数据集'}</span><span>{selectedData?`${selectedData.rows.toLocaleString()} 行 · ${selectedData.columns.length} 列`:'上传或查看已有数据'}</span><span>⌄</span></button><div className="welcome-foot"><Icon name="check" size={15}/>查询与计算可核对 · 分析范围可追溯</div></div>:<div className="conversation">{messages.map(m=>m.role==='user'?<div className="question-row" key={m.id}><div className="question-bubble">{m.content}</div><span className="user-avatar">我</span></div>:<div className="assistant" key={m.id}><span className="assistant-avatar"><Icon name="spark" size={21}/></span><div className="answer-body"><div className="answer-byline">洞察<span>· {answerLabel(m.status)}</span></div>{m.status==='running'&&busy&&<RunStatus message={progress} hasContent={!!m.content} onStop={()=>abort.current?.abort()}/>}{m.failure&&<FailureNotice failure={m.failure} hasPartial={!!m.content.trim()} disabled={busy||loading||dataBusy} onHistory={session?latestHistory:undefined} onRetry={m.id===messages.at(-1)?.id?()=>prepareRetry(m):undefined}/>}{canShowArtifacts(m.status)&&summaryMetrics(m.result).length>0&&<div className="metrics">{summaryMetrics(m.result).map(item=><div key={item.label}><span>{item.label}</span><strong>{Number(item.value).toLocaleString('zh-CN',{maximumFractionDigits:2})}</strong><small>{m.result?.analysis_context?.metric_label||'本轮计算'}</small></div>)}</div>}<ReportContent content={m.content} charts={canShowArtifacts(m.status)?chartPaths(m):[]} partial={!!m.failure} streaming={m.status==='running'} onExpand={setExpanded}/>{m.result&&canShowArtifacts(m.status)&&<details className="evidence-details"><summary><Icon name="check" size={15}/>口径与执行证据<span>展开查看</span></summary><p>{m.result.analysis_context?scopeCaption(m.result.analysis_context):m.result.plan?.metric||'本轮未建立结构化分析范围。'}</p>{m.result.steps.map(step=><div className="tool-evidence" key={step.round}><strong>第 {step.round} 轮 · {step.action}</strong><p>{step.reasoning}</p><pre>{step.tool_input}</pre><pre>{step.observation}</pre></div>)}</details>}{canShowArtifacts(m.status)&&(m.result?.suggestions||m.suggestions||[]).length>0&&<div className="followups"><span>继续探索</span>{(m.result?.suggestions||m.suggestions||[]).map(s=><button disabled={busy} key={s.question} onClick={()=>setDraft(s.question)}>{s.label} ↗</button>)}</div>}<div className="answer-meta">{m.status==='running'?'正在等待完整结果保存':m.failure?'本轮未确认完成，请以最新历史记录为准。':'历史数字不作为追问证据；新的数据问题会重新查询。'}</div></div></div>)}</div>}</div>
   {view==='chat'&&<footer className="composer-area"><form className="composer" onSubmit={e=>{e.preventDefault();void send()}}><textarea ref={inputRef} aria-label="输入分析问题" placeholder={messages.length?'继续追问，或换一个分析角度…':'问一个关于数据的问题…'} value={draft} disabled={busy||loading} maxLength={2000} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void send()}}} rows={2}/><div className="composer-bottom"><button type="button" className="composer-dataset" disabled={busy||dataBusy} title={selected?`当前数据：${selectedData?datasetName(selectedData):selected}（public.${selected}）`:'选择或上传本轮分析的数据'} onClick={()=>setView('datasets')}><Icon name="database" size={15}/>{selected&&<span className="composer-source-label">当前数据</span>}<span className="composer-source-name">{selectedData?datasetName(selectedData):selected||'选择数据集'}</span></button><span>Enter 发送 · Shift + Enter 换行</span>{busy&&<button type="button" className="composer-stop" onClick={()=>abort.current?.abort()}>停止接收</button>}<button type="submit" className="send" disabled={!draft.trim()||busy||loading} aria-label="发送分析问题"><Icon name="arrow"/></button></div></form><p>结果需结合口径核对 · 当前仅限本机使用 · 含业务数据的查询结果会发送至模型服务</p></footer>}
  </section>
  {expanded&&<div className="modal-backdrop" onClick={()=>setExpanded(null)}><div className="modal chart-modal" onClick={e=>e.stopPropagation()}><div className="modal-heading"><h3>分析图表</h3><button className="icon-button" aria-label="关闭图表" onClick={()=>setExpanded(null)}><Icon name="close"/></button></div><img src={expanded} alt="放大的分析图表"/></div></div>}
  {pendingDelete&&<div className="modal-backdrop"><div className="modal delete-modal"><h3>永久删除这个会话？</h3><p>“{pendingDelete.title}”及其所有聊天记录将从数据库删除。没有自动备份，无法从页面恢复；已导入数据表不会被删除。</p><div><button className="outline-button" disabled={deleting} onClick={()=>setPendingDelete(null)}>取消</button><button className="danger-button" disabled={deleting} onClick={()=>void removeSession()}>{deleting?'删除中…':'确认永久删除'}</button></div></div></div>}{toast&&<div className="toast" role="status"><Icon name="check"/>{toast}</div>}
 </div>
}
function scopeCaption(scope:Scope){return `${scope.source_table} · ${scope.metric_label} = ${scope.metric_expression} · ${scope.baseline?scope.baseline.start+' 至 '+scope.baseline.end+'（不含） → ':''}${scope.current?scope.current.start+' 至 '+scope.current.end+'（不含）':'未限定时间'} · ${scope.filters.length?scope.filters.map(f=>f.column+'='+f.values.join('、')).join('；'):'无筛选'}`}
createRoot(document.getElementById('root')!).render(<App/>);
