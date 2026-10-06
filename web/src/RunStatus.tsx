import {useEffect,useState} from 'react';
import {progressStage} from './chatStatus';
import type {Failure} from './chatStatus';

export function RunStatus({message,hasContent,onStop}:{message:string;hasContent:boolean;onStop:()=>void}) {
  const [slow,setSlow]=useState(false);
  useEffect(()=>{setSlow(false);const timer=setTimeout(()=>setSlow(true),20000);return()=>clearTimeout(timer)},[message]);
  return <div className="run-status">
    <div className="run-status-top"><span className="pulse"/><strong>{progressStage(message)}</strong><button onClick={onStop}>停止接收</button></div>
    <p role="status" aria-live="polite">{message}</p>
    <small>{slow&&!hasContent?'这一阶段等待较久，请勿重复提交；可以继续等待，或停止接收后查看历史。':hasContent?'回答正在生成，完整结果保存后才会标记完成。':'阶段来自实际执行进度，不显示估算百分比。'}</small>
  </div>;
}

export function FailureNotice({failure,hasPartial,disabled,onRetry,onHistory}:{failure:Failure;hasPartial:boolean;disabled:boolean;onRetry?:()=>void;onHistory?:()=>void}) {
  return <div className={`failure-notice ${failure.kind==='stopped'?'stopped-notice':''}`}>
    <div role="status"><strong>{failure.title}</strong><p>{failure.hint}</p></div>
    {hasPartial&&<small>下方仅为中断前的片段，不是已完成结论；不会作为成功回答展示。</small>}
    <details><summary>查看错误详情</summary><p>{failure.detail}</p></details>
    {(onRetry||onHistory)&&<div className="failure-actions">
      {onHistory&&<button disabled={disabled} onClick={onHistory}>查看最新历史</button>}
      {onRetry&&<button disabled={disabled} onClick={onRetry}>{failure.kind==='missing'?'带问题新建分析':'编辑后重试'}</button>}
      {onRetry&&<span>只填回问题，确认发送后才会重新调用。</span>}
    </div>}
  </div>;
}
