import test from 'node:test';
import assert from 'node:assert/strict';
import {analyzeStream,APIError} from '../src/api.ts';
import {failureInfo,answerLabel,canShowArtifacts,progressStage} from '../src/chatStatus.ts';

test('stopped and failed drafts cannot present charts or completed labels',()=>{
 for(const status of ['running','stopped','failed']) assert.equal(canShowArtifacts(status),false);
 assert.equal(canShowArtifacts('completed'),true);
 assert.equal(answerLabel('stopped'),'已停止接收');
 assert.match(failureInfo(new Error(),true).hint,/消耗不会撤回/);
 assert.equal(progressStage('正在读取数据结构…'),'读取数据');
 for(const [error,kind] of [[new APIError(409,'会话忙'),'busy'],[new APIError(404,'找不到'),'missing'],[new Error('达到预算'),'budget'],[new Error('连接中断'),'connection'],[new Error('请求超时'),'timeout']]) assert.equal(failureInfo(error).kind,kind);
});

test('session arrives before failure and SSE preserves HTTP error status',async()=>{
 const original=globalThis.fetch,events=[];
 globalThis.fetch=async()=>new Response('event: session\ndata: {"session_id":"saved-id"}\n\nevent: delta\ndata: {"text":"片段"}\n\nevent: error\ndata: {"status":409,"message":"会话忙"}\n\n');
 try{
  await assert.rejects(()=>analyzeStream('测试',null,(event,data)=>events.push([event,data]),new AbortController().signal),error=>error instanceof APIError&&error.status===409);
  assert.deepEqual(events.map(([event])=>event),['session','delta']);
  assert.equal(events[0][1].session_id,'saved-id');
 }finally{globalThis.fetch=original}
});

test('persisted result is terminal even without EOF and is not overwritten by late error',async()=>{
 const original=globalThis.fetch,events=[];let cancelled=false;
 globalThis.fetch=async()=>new Response(new ReadableStream({
  start(controller){controller.enqueue(new TextEncoder().encode('event: result\ndata: {"answer":"完成","finished":true,"steps":[],"charts":[],"suggestions":[]}\n\nevent: error\ndata: {"message":"过期错误"}\n\n'))},
  cancel(){cancelled=true}
 }));
 try{
  await analyzeStream('测试',null,(event)=>events.push(event),new AbortController().signal);
  assert.deepEqual(events,['result']);assert.equal(cancelled,true);
 }finally{globalThis.fetch=original}
});

test('malformed or incomplete result cannot be mistaken for success',async()=>{
 const original=globalThis.fetch;
 try{
  for(const payload of ['broken','{"answer":"不完整"}']){
   globalThis.fetch=async()=>new Response('event: result\ndata: '+payload+'\n\n');
   await assert.rejects(()=>analyzeStream('测试',null,()=>assert.fail('invalid result was rendered'),new AbortController().signal),/异常响应|完整分析结果/);
  }
 }finally{globalThis.fetch=original}
});
