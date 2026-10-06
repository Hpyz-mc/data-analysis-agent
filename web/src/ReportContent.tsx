import {useState} from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {Icon} from './icons';
import {remarkReportCharts} from './reportCharts';

function ReportChart({path,label,onExpand}:{path:string;label:string;onExpand:(path:string)=>void}) {
  const [unavailable,setUnavailable]=useState(false);
  return <div className="chart-card live-chart report-inline-chart">
    <div className="chart-header">
      <div><h3>{label}</h3><p>来源：本轮实际生成的图表</p></div>
      <div className="chart-actions">
        <button className="icon-button" aria-label="放大图表" onClick={()=>onExpand(path)}><Icon name="expand" size={16}/></button>
        <a className="icon-button" href={path} download aria-label="下载图表"><Icon name="download" size={16}/></a>
      </div>
    </div>
    {unavailable?<p className="chart-unavailable" role="status">图表暂时无法读取，请稍后重新打开历史。</p>
      :<img src={path} alt={label} loading="lazy" onError={()=>setUnavailable(true)}/>}
  </div>;
}

export function ReportContent({content,charts,partial=false,streaming=false,onExpand}:{content:string;charts:string[];partial?:boolean;streaming?:boolean;onExpand:(path:string)=>void}) {
  return <div className={`markdown ${partial?'partial-answer':''}`}>
    <Markdown remarkPlugins={[remarkGfm,[remarkReportCharts,{paths:charts}]]} skipHtml components={{
      table:({children})=><div className="report-table-scroll"><table>{children}</table></div>,
      // Insert chart cards as block nodes, never invalid divs inside p.
      div:({node,children,...props})=>{
        const path=node?.properties.dataReportChart;
        const title=node?.properties.dataChartTitle;
        return typeof path==='string'&&charts.includes(path)
          ?<ReportChart path={path} label={typeof title==='string'?title:'本轮分析图表'} onExpand={onExpand}/>
          :<div {...props}>{children}</div>;
      },
      img:()=>null,
      a:({children,href})=><a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
    }}>{content}</Markdown>
    {streaming&&content&&<span className="typing-cursor">▍</span>}
  </div>;
}
