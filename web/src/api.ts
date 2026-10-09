import type {Failure} from './chatStatus';
export type Scope = {kind:string;source_table:string;metric_label:string;metric_expression:string;time_grain?:string|null;time_column?:string|null;baseline?:{start:string;end:string}|null;current?:{start:string;end:string}|null;baseline_group?:string|null;current_group?:string|null;dimension?:string|null;filters:{column:string;values:(string|number|boolean)[]}[]};
export type Suggestion = {label:string;question:string};
export type Step = {round:number;action:string;tool_input:string;observation:string;reasoning:string};
export type Result = {answer:string;finished:boolean;session_id:string|null;source_table?:string|null;charts:string[];suggestions:Suggestion[];analysis_context:Scope|null;session_context?:Scope|null;context_reset?:boolean;requires_clarification?:boolean;steps:Step[];plan?:{metric:string;time_range:string;comparison:string}|null};
export type Message = {id:string;role:'user'|'assistant';content:string;status?:string;result?:Result;charts?:string[];suggestions?:Suggestion[];context?:Scope|null;failure?:Failure;retry?:{question:string;sourceTable:string}};
export type Session = {session_id:string;title:string;question_preview?:string;created_at:string;updated_at:string};
export type Dataset = {table_name:string;original_file_name?:string;rows:number;columns:string[];preview:Record<string,unknown>[];upload_time?:string};
export class APIError extends Error {status:number;detail:any;constructor(status:number,detail:any){super(typeof detail==='string'?detail:detail?.message||'请求未完成');this.status=status;this.detail=detail}}
export async function request<T>(path:string,init:RequestInit={}):Promise<T> {const response=await fetch(path,init);if(!response.ok){const body=await response.json().catch(()=>({detail:'接口暂时不可用'}));throw new APIError(response.status,body.detail)}return response.status===204?undefined as T:response.json()}
export function listSessions(){return request<{sessions:Session[]}>('/api/v1/sessions')}
export function listDatasets(){return request<{datasets:Dataset[]}>('/api/v1/datasets')}
export function displayQuestion(question:string){const original=question.trim();return original.replace(/^使用数据表 public\.[\p{L}\p{N}_]+。\s*/u,'')||original}
export function chartPath(value:string){return /^\/api\/v1\/charts\/[a-zA-Z0-9_-]+\.png$/.test(value)?value:null}
export async function analyzeStream(question:string,session:string|null,onEvent:(event:string,data:any)=>void,signal:AbortSignal,sourceTable:string|null=null) {
 const response=await fetch('/api/v1/analyze/stream',{method:'POST',headers:{'Content-Type':'application/json','Accept':'text/event-stream'},body:JSON.stringify({question,...(session?{session_id:session}:{}),...(sourceTable?{source_table:sourceTable}:{})}),signal});
 if(!response.ok){const error=await response.json().catch(()=>({detail:'连接分析服务失败'}));throw new APIError(response.status,error.detail)}
 if(!response.body)throw new Error('浏览器未收到流式响应');
 const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',completed=false;
 function parse(block:string){let event='message';const data:string[]=[];for(const line of block.split('\n')){if(line.startsWith('event:'))event=line.slice(6).trim();else if(line.startsWith('data:'))data.push(line.slice(5).trimStart())}if(!data.length)return;let value;try{value=JSON.parse(data.join('\n'))}catch{throw new Error('收到异常响应，请查看最新历史。')}if(event==='error')throw new APIError(typeof value.status==='number'?value.status:500,value.message||'分析未完成');if(event==='result'){if(typeof value.answer!=='string'||typeof value.finished!=='boolean'||!Array.isArray(value.steps)||!Array.isArray(value.charts)||!Array.isArray(value.suggestions))throw new Error('未收到完整分析结果，请查看最新历史。');completed=true}if(event==='session'&&typeof value.session_id!=='string')throw new Error('收到异常响应，请查看最新历史。');onEvent(event,value)}
 try {while(true){const {value,done}=await reader.read();buffer+=decoder.decode(value,{stream:!done});buffer=buffer.replace(/\r\n/g,'\n');let index;while((index=buffer.indexOf('\n\n'))!==-1){parse(buffer.slice(0,index));if(completed)return;buffer=buffer.slice(index+2)}if(done)break}if(buffer.trim())parse(buffer);if(!completed)throw new Error('连接已结束，但未收到完整分析结果，请查看历史或重试。')}finally{await reader.cancel().catch(()=>{});reader.releaseLock()}
}
