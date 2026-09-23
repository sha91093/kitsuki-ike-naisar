'use strict';

const layoutBase = {
  margin: { l: 62, r: 18, t: 25, b: 54 },
  paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
  font: { family: 'Noto Sans JP, sans-serif', size: 10, color: '#41504a' },
  hovermode: 'x unified', legend: { orientation: 'h', y: 1.12 }
};
const area = value => `${(value / 10000).toFixed(2)} ha`;
const signed = value => `${value >= 0 ? '+' : ''}${(value / 10000).toFixed(2)} ha`;

async function init() {
  const [validationResponse, boundaryResponse] = await Promise.all([
    fetch('data/sentinel2_27.json'), fetch('data/boundary_27.json')
  ]);
  if (!validationResponse.ok || !boundaryResponse.ok) throw new Error('検証データを読み込めません');
  const data = await validationResponse.json();
  const boundary = await boundaryResponse.json();
  const observations = data.observations.filter(row => row.buffer_m === 20);
  const clear = observations.filter(row => row.quality_flag === 'ok' && row.seed_valid);
  const drought = clear.find(row => row.observation_date === '2026-08-23');
  const wet = clear.find(row => row.observation_date === '2026-06-29');
  const droughtPair = data.comparisons.find(row => row.nisar_date === '2026-08-26');
  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><span>光学候補</span><strong>${clear.length} / ${observations.length}日</strong><small>局所有効画素率95%以上</small></div>
    <div class="summary-card"><span>多雨後 6/29</span><strong>${area(wet.open_water_area_m2)}</strong><small>NISARとの差 ${signed(data.comparisons[0].difference_m2)}</small></div>
    <div class="summary-card"><span>渇水期 8/23</span><strong>${area(drought.open_water_area_m2)}</strong><small>6/29比 ${((drought.open_water_area_m2 / wet.open_water_area_m2 - 1) * 100).toFixed(0)}%</small></div>
    <div class="summary-card"><span>8月のNISAR過大差</span><strong>${signed(droughtPair.difference_m2)}</strong><small>NISAR 8/26 − 光学 8/23</small></div>`;

  const goodNisar = data.comparisons.filter(row => row.nisar_quality_flag === 'ok');
  Plotly.newPlot('comparison-chart', [
    {
      x: clear.map(row => row.observation_date), y: clear.map(row => row.open_water_area_m2),
      type: 'scatter', mode: 'lines+markers', name: 'Sentinel-2 光学水域',
      line: { color: '#087ca2', width: 3 }, marker: { size: 8 },
      hovertemplate: '%{x}<br>%{y:,.0f} m²<extra></extra>'
    },
    {
      x: goodNisar.map(row => row.nisar_date), y: goodNisar.map(row => row.nisar_area_m2),
      type: 'scatter', mode: 'lines+markers', name: 'NISAR 両軌道一致',
      line: { color: '#237563', width: 3, dash: 'dash' }, marker: { size: 8 },
      hovertemplate: '%{x}<br>%{y:,.0f} m²<extra></extra>'
    }
  ], {
    ...layoutBase,
    yaxis: { title: '開放水面積 (m²)', rangemode: 'tozero', gridcolor: '#e6ece8' },
    xaxis: { title: '観測日', gridcolor: '#edf1ee' }
  }, { responsive: true, displayModeBar: false });

  const map = L.map('optical-map');
  const aerial = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg', {
    maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>'
  }).addTo(map);
  const standard = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png', {
    maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>'
  });
  L.control.layers({ '航空画像': aerial, '標準地図': standard }).addTo(map);
  const pondLayer = L.geoJSON(boundary.imagery_qa.pond_geometry, {
    style: { color: '#111', weight: 3, fillOpacity: 0, dashArray: '7 4' }
  }).addTo(map);
  map.fitBounds(pondLayer.getBounds(), { padding: [25, 25], maxZoom: 18 });
  let waterLayer;
  function showWater(itemId) {
    const row = clear.find(item => item.item_id === itemId);
    if (waterLayer) map.removeLayer(waterLayer);
    waterLayer = L.geoJSON(row.water_geometry, {
      style: { color: '#087ca2', weight: 2, fillColor: '#23a9d4', fillOpacity: .48 }
    }).addTo(map).bindTooltip(`${row.observation_date}<br>${area(row.open_water_area_m2)}`);
    document.getElementById('map-caption').innerHTML = `<span class="legend-chip"></span>${row.observation_date} 光学水域 ${area(row.open_water_area_m2)}　／　黒破線: 現行OSMポリゴン　／　局所有効画素 ${(row.local_valid_ratio * 100).toFixed(0)}%`;
  }
  const selector = document.getElementById('date-select');
  selector.innerHTML = clear.map(row => `<option value="${row.item_id}" ${row.observation_date === '2026-08-23' ? 'selected' : ''}>${row.observation_date}（${area(row.open_water_area_m2)}）</option>`).join('');
  selector.addEventListener('change', () => showWater(selector.value));
  showWater(selector.value);

  document.getElementById('comparison-table').innerHTML = data.comparisons.map(row => {
    const noMatch = !row.sentinel2_date;
    const distant = !noMatch && Math.abs(row.day_offset) > 5;
    const low = row.nisar_quality_flag !== 'ok';
    const label = noMatch ? '光学なし' : low ? 'NISAR除外' : distant ? '日差大' : '比較可';
    const qualityClass = low || noMatch ? 'quality-low' : distant ? 'quality-distant' : 'quality-ok';
    return `<tr><td>${row.nisar_date}</td><td>${row.sentinel2_date || '—'}</td><td>${noMatch ? '—' : `${row.day_offset > 0 ? '+' : ''}${row.day_offset}日`}</td><td>${area(row.nisar_area_m2)}</td><td>${noMatch ? '—' : area(row.sentinel2_area_m2)}</td><td>${noMatch ? '—' : signed(row.difference_m2)}</td><td class="${qualityClass}">${label}</td></tr>`;
  }).join('');
}

init().catch(error => {
  console.error(error);
  document.getElementById('summary').textContent = 'データの読み込みに失敗しました。';
});
