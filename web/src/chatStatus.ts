import {APIError} from './api.ts';

export type Failure = {kind:'stopped'|'busy'|'budget'|'timeout'|'connection'|'missing'|'service';title:string;detail:string;hint:string};

export function failureInfo(error:unknown,stopped=false):Failure {
  const detail=error instanceof Error?error.message:'分析暂时未完成，请稍后重试。';
  if(stopped)return {kind:'stopped',title:'已停止接收回答',detail:'你已停止本次等待。',hint:'服务端可能仍在结束处理；请先查看最新历史。已发生的模型消耗不会撤回。'};
  if(error instanceof APIError&&error.status===409)return {kind:'busy',title:'这段会话仍在处理中',detail,hint:'请先查看最新历史，确认上一轮已经结束后再发送，避免重复请求。'};
  if(error instanceof APIError&&error.status===404)return {kind:'missing',title:'这段会话暂时无法读取',detail,hint:'会话可能已被删除。可以带着原问题新建分析。'};
  if(/预算|调用次数上限/.test(detail))return {kind:'budget',title:'本轮已达到调用预算',detail,hint:'请明确数据表、缩小时间范围或减少拆解维度，再提交问题。'};
  if(/超时|时限|timeout/i.test(detail))return {kind:'timeout',title:'本轮等待超时',detail,hint:'请先查看最新历史，确认是否已有结果，再决定重试或缩小分析范围。'};
  if(error instanceof TypeError||/连接|网络|流式|完整分析结果|异常响应/.test(detail))return {kind:'connection',title:'连接中断，结果尚未确认',detail,hint:'服务端可能仍在处理；请先查看最新历史，避免重复提交。'};
  return {kind:'service',title:'本轮分析未完成',detail,hint:'问题已保留。可以查看错误详情，编辑问题后重新发送；不会自动重试。'};
}

export function progressStage(message:string) {
  if(/连接/.test(message))return '连接服务';
  if(/数据结构/.test(message))return '读取数据';
  if(/查询/.test(message))return '查询数据';
  if(/计算|核对分析结果/.test(message))return '计算核验';
  if(/图表/.test(message))return '生成图表';
  if(/生成回答/.test(message))return '生成回答';
  if(/规划|检查证据/.test(message))return '分析规划';
  return '正在分析';
}

export function answerLabel(status?:string) {
  if(status==='running')return '处理中';
  if(status==='stopped')return '已停止接收';
  if(status==='failed')return '本轮未完成';
  if(status==='clarification')return '需要澄清';
  if(status==='incomplete')return '本轮未完成';
  return '分析回答';
}

export function canShowArtifacts(status?:string) {
  return status!=='running'&&status!=='failed'&&status!=='stopped';
}
