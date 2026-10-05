import test from 'node:test';
import assert from 'node:assert/strict';
import {placeReportCharts} from '../src/reportCharts.ts';

const a='/api/v1/charts/chart-a.png',b='/api/v1/charts/chart-b.png';
const text=value=>({type:'text',value});
const paragraph=(...children)=>({type:'paragraph',children});
const section=value=>paragraph({type:'strong',children:[text(value)]});
const image=(url,alt='')=>({type:'image',url,alt});
const path=node=>node.data?.hProperties?.dataReportChart;
const cards=tree=>tree.children.filter(node=>path(node));

test('the screenshot report puts its image between section three and four',()=>{
 const description=paragraph(text('已返回的折线图（仅总体月度走势，无渠道维度）：\n'),image(a,'销售额月度走势'));
 const tree={type:'root',children:[section('三、图表'),description,section('四、口径与限制'),paragraph(text('销售额 702,600；时间不变。'))]};
 placeReportCharts(tree,[a]);
 assert.equal(path(tree.children[2]),a);
 assert.equal(tree.children[3].children[0].children[0].value,'四、口径与限制');
 assert.equal(tree.children[1].children[0].value,'已返回的折线图（仅总体月度走势，无渠道维度）：\n');
 assert.equal(cards(tree).length,1);
});

test('multiple explicit images are placed beside their own descriptions, in report order',()=>{
 const tree={type:'root',children:[section('贡献图'),paragraph(image(b,'渠道贡献')),section('趋势图'),paragraph(image(a,'月度趋势')),section('限制')]};
 placeReportCharts(tree,[a,b]);
 assert.deepEqual(cards(tree).map(path),[b,a]);
 assert.equal(tree.children[1].data.hProperties.dataChartTitle,'渠道贡献');
 assert.equal(tree.children[3].data.hProperties.dataChartTitle,'月度趋势');
});

test('links, plain chart paths and duplicate references show one chart only',()=>{
 const tree={type:'root',children:[paragraph({type:'link',url:a,children:[text('走势图')]}),paragraph(text('图表：'+a)),paragraph(image(a))]};
 placeReportCharts(tree,[a,a]);
 assert.equal(cards(tree).length,1);
 assert.equal(path(tree.children[1]),a);
 assert.equal(tree.children[0].children[0].value,'走势图');
});

test('a legacy single chart section receives its attachment before the next section',()=>{
 const tree={type:'root',children:[section('三、图表'),paragraph(text('已生成销售额折线图。')),section('四、口径'),paragraph(text('保持原始口径。'))]};
 placeReportCharts(tree,[a]);
 assert.equal(path(tree.children[2]),a);
 assert.equal(tree.children[3].children[0].children[0].value,'四、口径');
 const ambiguous={type:'root',children:[section('趋势图'),section('贡献图'),section('限制')]};
 placeReportCharts(ambiguous,[a,b]);
 assert.deepEqual(ambiguous.children.slice(-2).map(path),[a,b]);
});

test('unconfirmed paths, remote images and unfinished responses cannot fabricate chart cards',()=>{
 const tree={type:'root',children:[paragraph(image(a)),paragraph(image('https://example.test/image.png'))]};
 placeReportCharts(tree,[]);
 assert.equal(cards(tree).length,0);
 placeReportCharts(tree,['https://example.test/image.png','/api/v1/charts/../a.png']);
 assert.equal(cards(tree).length,0);
});

test('code and data tables are not treated as chart placement instructions',()=>{
 const code={type:'code',value:a},table={type:'table',children:[{type:'tableRow',children:[{type:'tableCell',children:[text(a)]}]}]};
 const external=paragraph(text('https://example.test'+a));
 const tree={type:'root',children:[code,table,external,section('限制')]};
 placeReportCharts(tree,[a]);
 assert.equal(tree.children[0],code);
 assert.equal(tree.children[1],table);
 assert.equal(tree.children[2].children[0].value,'https://example.test'+a);
 assert.equal(path(tree.children.at(-1)),a);
});
