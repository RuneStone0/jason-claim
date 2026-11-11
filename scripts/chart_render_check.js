// Render-proof for the jason-claim chart: run the page's OWN drawChart() against data.json and
// assert every series produced a polyline. Catches the 2026-11-11 class of failure, where the page
// shipped with QQQ and AI all-null and the chart silently drew one line, with nothing failing.
const fs = require('fs');
const path = process.argv[2] || '/opt/data/code/RuneStone0/jason-claim';

const html = fs.readFileSync(path + '/index.html', 'utf8');
const data = JSON.parse(fs.readFileSync(path + '/data.json', 'utf8'));

const start = html.indexOf('function drawChart(c){');
if (start < 0) { console.error('FAIL: drawChart not found in index.html'); process.exit(1); }
let depth = 0, end = -1;
for (let j = html.indexOf('{', start); j < html.length; j++) {
  if (html[j] === '{') depth++;
  else if (html[j] === '}') { depth--; if (depth === 0) { end = j + 1; break; } }
}
const fn = html.slice(start, end);

let svgHTML = '';
const el = () => ({ set innerHTML(v) { svgHTML = v; }, get innerHTML() { return svgHTML; } });
const $ = () => el();
const esc = (s) => String(s);
const drawChart = new Function('$', 'esc', fn + '; return drawChart;')($, esc);
drawChart(data.chart);

const strokes = [...svgHTML.matchAll(/stroke="(#[0-9a-f]{6})"/g)].map(m => m[1]);
const want = { '#f7931a': 'BTC', '#5ea8ff': 'QQQ', '#2ee6a8': 'AI' };
const missing = Object.keys(want).filter(c => !strokes.includes(c));
const circles = (svgHTML.match(/<circle/g) || []).length;

console.log(`as_of ${data.meta.as_of} | dates ${data.chart.dates.length} | circles ${circles}`);
console.log(`lines drawn: ${strokes.filter(c => want[c]).length}/3` +
            (missing.length ? ` MISSING ${missing.map(c => want[c]).join(', ')}` : ''));
if (missing.length) { console.error('FAIL: a chart line did not render'); process.exit(1); }
console.log('PASS: BTC, QQQ and AI all render');
