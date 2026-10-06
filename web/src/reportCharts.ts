import {chartPath} from './api.ts';

// A structural MDAST view; Markdown parsing and escaping stay with react-markdown.
export type ReportNode = {
  type:string; value?:string; url?:string; alt?:string;
  children?:ReportNode[];
  data?:{hName?:string;hProperties?:Record<string,string>};
};

function textOf(node:ReportNode):string {
  return node.value??node.alt??node.children?.map(textOf).join('')??'';
}

function isSection(node:ReportNode):boolean {
  if(node.type==='heading')return true;
  if(node.type!=='paragraph')return false;
  const meaningful=node.children?.filter(child=>child.type!=='text'||!!child.value?.trim())||[];
  return meaningful.length===1&&meaningful[0].type==='strong';
}

function isChartSection(node:ReportNode):boolean {
  if(!isSection(node))return false;
  const title=textOf(node).trim().replace(/^(?:[一二三四五六七八九十百]+|\d+)[、.．)）]\s*/u,'').replace(/[:：]$/,'');
  return title.length<=30&&!/未|没有|不能|限制|建议|说明|证据|待验证/.test(title)
    &&/(?:图表|折线图|柱状图|贡献图|趋势图|走势图)(?:展示)?(?:[（(].*[）)])?$/.test(title);
}

function chartNode(path:string,label='本轮分析图表'):ReportNode {
  return {type:'paragraph',children:[],data:{hName:'div',hProperties:{dataReportChart:path,dataChartTitle:label}}};
}

/**
 * Place only charts confirmed by API artifacts, once each.
 * Exact references take priority. A legacy report with one chart and one
 * dedicated chart section is repaired without changing report text/numbers.
 * Ambiguous multi-chart mappings remain attachments rather than being guessed.
 */
export function placeReportCharts(tree:ReportNode,paths:string[]) {
  const available=[...new Set(paths.filter(path=>chartPath(path)))];
  if(!available.length||!tree.children)return;
  const allowed=new Set(available),used=new Set<string>();
  const output:ReportNode[]=[];
  for(const block of tree.children){
    const referenced=new Map<string,string>();
    function visit(node:ReportNode):ReportNode[] {
      if(['code','html','table'].includes(node.type))return [node];
      if((node.type==='image'||node.type==='link')&&node.url&&allowed.has(node.url)){
        const label=textOf(node).trim();
        referenced.set(node.url,label||'本轮分析图表');
        return node.type==='image'?[]:[{type:'text',value:label===node.url?'':label}];
      }
      if(node.type==='text'||node.type==='inlineCode'){
        const matches=node.value?.match(/(?<![\w/:.-])\/api\/v1\/charts\/[a-zA-Z0-9_-]+\.png(?![\w?/#.-])/g)||[];
        for(const path of matches)if(allowed.has(path)){
          referenced.set(path,'本轮分析图表');
          node.value=node.value?.replace(path,'');
        }
      }
      if(node.children)node.children=node.children.flatMap(visit);
      return [node];
    }
    visit(block);
    if(block.type!=='paragraph'||textOf(block).trim())output.push(block);
    for(const [path,label] of referenced)if(!used.has(path)){
      used.add(path);output.push(chartNode(path,label));
    }
  }
  const remaining=available.filter(path=>!used.has(path));
  const sections=output.map((node,index)=>isChartSection(node)?index:-1).filter(index=>index>=0);
  if(available.length===1&&remaining.length===1&&sections.length===1){
    let insertion=sections[0]+1;
    while(insertion<output.length&&!isSection(output[insertion]))insertion++;
    output.splice(insertion,0,chartNode(remaining[0]));
  }else{
    for(const path of remaining)output.push(chartNode(path));
  }
  tree.children=output;
}

export function remarkReportCharts({paths}:{paths:string[]}) {
  return (tree:ReportNode)=>{placeReportCharts(tree,paths)};
}
