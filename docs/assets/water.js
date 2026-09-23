'use strict';

const colors = { 0: '#9aa9a2', 20: '#237563', 50: '#2878b5' };
const layoutBase = {
  margin: { l: 62, r: 18, t: 22, b: 54 },
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  font: { family: 'Noto Sans JP, sans-serif', size: 10, color: '#41504a' },
  hovermode: 'x unified',
  legend: { orientation: 'h', y: 1.12 }
};

const formatArea = value => `${(value / 10000).toFixed(2)} ha`;
const qualityLabel = flag => flag === 'ok' ? '採用' : '除外（低一致）';

async function init() {
  const response = await fetch('data/open_water_27.json');
  if (!response.ok) throw new Error('開放水面データを読み込めません');
  const data = await response.json();
  const adaptive = data.timeseries.filter(row => row.threshold_mode === 'per_scene_otsu');
  const usable = adaptive.filter(row => row.buffer_m === 50 && row.pair_quality_flag === 'ok');
  const low = adaptive.filter(row => row.buffer_m === 50 && row.pair_quality_flag !== 'ok');
  const areas = usable.map(row => row.open_water_area_m2);
  const converged = usable.every(row => {
    const at20 = adaptive.find(item => item.pair_id === row.pair_id && item.buffer_m === 20);
    return Math.abs(row.open_water_area_m2 - at20.open_water_area_m2) <= data.algorithm.pixel_area_m2;
  });
  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><span>採用ペア</span><strong>${usable.length} / ${usable.length + low.length}</strong><small>昇交・降交一致率50%以上</small></div>
    <div class="summary-card"><span>推奨バッファ</span><strong>${converged ? '20 m' : '要検討'}</strong><small>20 mと50 mの確定面積が収束</small></div>
    <div class="summary-card"><span>確定水面の範囲</span><strong>${formatArea(Math.min(...areas))}–${formatArea(Math.max(...areas))}</strong><small>品質良好ペア・50 m</small></div>
    <div class="summary-card"><span>除外ペア</span><strong>${low.length}</strong><small>${low.map(row => row.pair_date).join('・')}</small></div>`;

  const bufferTraces = [0, 20, 50].map(buffer => {
    const rows = adaptive.filter(row => row.buffer_m === buffer);
    return {
      x: rows.map(row => row.pair_date),
      y: rows.map(row => row.pair_quality_flag === 'ok' ? row.open_water_area_m2 : null),
      type: 'scatter', mode: 'lines+markers', name: `${buffer} m`,
      line: { color: colors[buffer], width: buffer === 20 ? 3 : 2 },
      marker: { size: 7 },
      hovertemplate: `%{x}<br>${buffer} m: %{y:,.0f} m²<extra></extra>`
    };
  });
  const excluded = adaptive.filter(row => row.buffer_m === 50 && row.pair_quality_flag !== 'ok');
  bufferTraces.push({
    x: excluded.map(row => row.pair_date), y: excluded.map(row => row.open_water_area_m2),
    type: 'scatter', mode: 'markers', name: '低一致・除外',
    marker: { color: '#c74b44', symbol: 'x', size: 11 },
    hovertemplate: '%{x}<br>除外値: %{y:,.0f} m²<extra></extra>'
  });
  Plotly.newPlot('buffer-chart', bufferTraces, {
    ...layoutBase, yaxis: { title: '開放水面積 (m²)', rangemode: 'tozero', gridcolor: '#e6ece8' },
    xaxis: { title: '観測ペア代表日', gridcolor: '#edf1ee' }
  }, { responsive: true, displayModeBar: false });

  const fixed = data.timeseries.filter(row => row.threshold_mode === 'fixed_wet_baseline' && row.buffer_m === 50);
  const adaptive50 = adaptive.filter(row => row.buffer_m === 50);
  Plotly.newPlot('threshold-chart', [
    { x: adaptive50.map(r => r.pair_date), y: adaptive50.map(r => r.pair_quality_flag === 'ok' ? r.open_water_area_m2 : null), type: 'scatter', mode: 'lines+markers', name: '観測別Otsu', line: { color: '#237563' } },
    { x: fixed.map(r => r.pair_date), y: fixed.map(r => r.pair_quality_flag === 'ok' ? r.open_water_area_m2 : null), type: 'scatter', mode: 'lines+markers', name: '多雨期固定', line: { color: '#2878b5', dash: 'dash' } }
  ], { ...layoutBase, yaxis: { title: '面積 (m²)', rangemode: 'tozero', gridcolor: '#e6ece8' }, xaxis: { gridcolor: '#edf1ee' } }, { responsive: true, displayModeBar: false });

  Plotly.newPlot('uncertain-chart', [{
    x: adaptive50.map(r => r.pair_date), y: adaptive50.map(r => r.uncertain_area_m2),
    type: 'bar', name: '判定不能', marker: { color: adaptive50.map(r => r.pair_quality_flag === 'ok' ? '#d9a14b' : '#c74b44') },
    hovertemplate: '%{x}<br>%{y:,.0f} m²<extra></extra>'
  }], { ...layoutBase, showlegend: false, yaxis: { title: '面積 (m²)', gridcolor: '#e6ece8' }, xaxis: { gridcolor: '#edf1ee' } }, { responsive: true, displayModeBar: false });

  document.getElementById('pair-table').innerHTML = adaptive50.map(row => `
    <tr><td>${row.pair_date}</td><td>${row.rain_7d_mm.toFixed(1)} mm</td><td>${row.open_water_area_m2.toLocaleString()} m²</td><td>${row.uncertain_area_m2.toLocaleString()} m²</td><td>${(row.orbit_agreement_ratio * 100).toFixed(1)}%</td><td class="${row.pair_quality_flag === 'ok' ? 'quality-ok' : 'quality-low'}">${qualityLabel(row.pair_quality_flag)}</td></tr>`).join('');
}

init().catch(error => {
  console.error(error);
  document.getElementById('summary').textContent = 'データの読み込みに失敗しました。';
});
