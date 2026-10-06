import test from 'node:test';
import assert from 'node:assert/strict';
import {analyzeStream,chartPath,displayQuestion,request,APIError} from '../src/api.ts';

test('user-facing questions hide only the source prefix, never the request content',()=>{
 assert.equal(displayQuestion('使用数据表 public.user_sales_20261003_01。查询月度销售额'),'查询月度销售额');
 assert.equal(displayQuestion('请比较 public.sales 和其他表'),'请比较 public.sales 和其他表');
 assert.equal(displayQuestion('使用数据表 public.sales。'),'使用数据表 public.sales。');
});

test('SSE handles UTF-8 and CRLF split at every byte, and carries session automatically',async()=>{
 const original=globalThis.fetch,events=[];
 globalThis.fetch=async(_url,init)=>{assert.deepEqual(JSON.parse(init.body),{question:'测试问题',session_id:'abc'});const bytes=new TextEncoder().encode('event: status\r\ndata: {"message":"查询中"}\r\n\r\nevent: delta\r\ndata: {"text":"中文回答"}\r\n\r\nevent: result\r\ndata: {"answer":"中文回答","finished":true,"steps":[],"charts":[],"suggestions":[]}\r\n\r\n');return new Response(new ReadableStream({start(controller){for(const byte of bytes)controller.enqueue(new Uint8Array([byte]));controller.close()}}))};
 try{await analyzeStream('测试问题','abc',(event,data)=>events.push({event,data}),new AbortController().signal);assert.deepEqual(events.map(e=>e.event),['status','delta','result']);assert.equal(events[1].data.text,'中文回答')}finally{globalThis.fetch=original}
});
test('a new chat omits the session ID; missing result is not success',async()=>{
 const original=globalThis.fetch;
 globalThis.fetch=async(_url,init)=>{assert.deepEqual(JSON.parse(init.body),{question:'测试'});return new Response('event: delta\ndata: {"text":"未完成"}\n\n')};
 try{await assert.rejects(()=>analyzeStream('测试',null,()=>{},new AbortController().signal),/未收到完整/)}finally{globalThis.fetch=original}
});
test('SSE error event becomes a readable failure',async()=>{
 const original=globalThis.fetch;globalThis.fetch=async()=>new Response('event: error\ndata: {"message":"证据不足"}\n\n');
 try{await assert.rejects(()=>analyzeStream('测试',null,()=>{},new AbortController().signal),/证据不足/)}finally{globalThis.fetch=original}
});
test('duplicate upload preserves structured information and deletion allows 204',async()=>{
 const original=globalThis.fetch;globalThis.fetch=async()=>new Response(JSON.stringify({detail:{message:'重复文件',table_name:'user_sales_01'}}),{status:409});
 try{await assert.rejects(()=>request('/api/v1/datasets/upload'),error=>error instanceof APIError&&error.status===409&&error.detail.table_name==='user_sales_01');globalThis.fetch=async()=>new Response(null,{status:204});assert.equal(await request('/api/v1/sessions/a',{method:'DELETE'}),undefined)}finally{globalThis.fetch=original}
});
test('only application-owned PNG paths can be fetched as charts',()=>{
 assert.equal(chartPath('/api/v1/charts/abc123.png'),'/api/v1/charts/abc123.png');for(const path of ['https://evil.test/a.png','/api/v1/charts/../a.png','/api/v1/charts/a.png?secret=1','javascript:alert(1)'])assert.equal(chartPath(path),null)
});
