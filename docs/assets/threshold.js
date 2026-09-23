'use strict';

const layoutBase = {
  margin: { l: 62, r: 18, t: 25, b: 54 }, paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)', font: { family: 'Noto Sans JP, sans-serif', size: 10, color: '#41504a' },
  hovermode: 'x unified', legend: { orientation: 'h', y: 1.12 }
};
const area = value => `${(value / 10000).toFixed(2)} ha`;
const average = values => values.reduce((sum, value) => sum + value, 0) / values.length;

async function init() {
  const [response, boundaryResponse] = await Promise.all([fetch('data/threshold_27.json'), fetch('data/boundary_27.json')]);
  if (!response.ok || !boundaryResponse.ok) throw new Error('閾値検証データを読み込めません');
  const data = await response.json();
  const boundary = await boundaryResponse.json();
  const validation = data.results.filter(row => row.split === 'validation');
  const oldValidation = validation.filter(row => row.method === 'per_scene_otsu');
  const tunedValidation = validation.filter(row => row.method === 'tuned_fixed');
  const oldIou = average(oldValidation.map(row => row.iou));
  const tunedIou = average(tunedValidation.map(row => row.iou));
  const oldMae = average(oldValidation.map(row => Math.abs(row.area_error_m2)));
  const tunedMae = average(tunedValidation.map(row => Math.abs(row.area_error_m2)));
  const drought = data.results.find(row => row.optical_date === '2026-08-23' && row.method === 'tuned_fixed');
  const d = data.selected_thresholds.descending;
  const a = data.selected_thresholds.ascending;
  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><span>降交の固定閾値</span><strong>${d.hh_threshold_db} / ${d.hv_threshold_db}</strong><small>HH / HV (dB)</small></div>
    <div class="summary-card"><span>昇交の固定閾値</span><strong>${a.hh_threshold_db} / ${a.hv_threshold_db}</strong><small>HH / HV (dB)</small></div>
    <div class="summary-card"><span>未使用日 平均IoU</span><strong>${oldIou.toFixed(3)} → ${tunedIou.toFixed(3)}</strong><small>+${((tunedIou - oldIou) * 100).toFixed(1)}ポイント</small></div>
    <div class="summary-card"><span>8月に残る面積誤差</span><strong>+${area(drought.area_error_m2)}</strong><small>固定閾値でも過大</small></div>`;

  const dates = [...new Set(data.results.map(row => row.optical_date))];
  const optical = dates.map(date => data.results.find(row => row.optical_date === date).optical_area_m2);
  const methodValues = method => dates.map(date => data.results.find(row => row.optical_date === date && row.method === method).water_area_m2);
  Plotly.newPlot('area-chart', [
    { x: dates, y: optical, type: 'scatter', mode: 'lines+markers', name: 'Sentinel-2', line: { color: '#087ca2', width: 3 } },
    { x: dates, y: methodValues('per_scene_otsu'), type: 'scatter', mode: 'lines+markers', name: '観測別Otsu', line: { color: '#d98c22', dash: 'dot', width: 2 } },
    { x: dates, y: methodValues('tuned_fixed'), type: 'scatter', mode: 'lines+markers', name: '調整済み固定閾値', line: { color: '#237563', dash: 'dash', width: 3 } }
  ], { ...layoutBase, yaxis: { title: '開放水面積 (m²)', rangemode: 'tozero', gridcolor: '#e6ece8' }, xaxis: { title: 'Sentinel-2観測日', gridcolor: '#edf1ee' } }, { responsive: true, displayModeBar: false });

  const map = L.map('error-map');
  const aerial = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg', { maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>' }).addTo(map);
  const standard = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png', { maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>' });
  L.control.layers({ '航空画像': aerial, '標準地図': standard }).addTo(map);
  const pond = L.geoJSON(boundary.imagery_qa.pond_geometry, { style: { color: '#111', weight: 3, fillOpacity: 0, dashArray: '7 4' } }).addTo(map);
  map.fitBounds(pond.getBounds(), { padding: [25, 25], maxZoom: 18 });
  let errorLayers = [];
  function showErrors(date) {
    errorLayers.forEach(layer => map.removeLayer(layer)); errorLayers = [];
    const feature = data.map_features.find(item => item.optical_date === date);
    const styles = {
      true_positive: { color: '#117653', fillColor: '#1d9b70', fillOpacity: .45 },
      false_positive: { color: '#b62e28', fillColor: '#d74b45', fillOpacity: .58 },
      false_negative: { color: '#1769a5', fillColor: '#3185c7', fillOpacity: .58 }
    };
    Object.entries(styles).forEach(([key, style]) => {
      if (feature[key]) errorLayers.push(L.geoJSON(feature[key], { style: { ...style, weight: 1.5 } }).addTo(map));
    });
    const row = data.results.find(item => item.optical_date === date && item.method === 'tuned_fixed');
    document.getElementById('map-caption').textContent = `${date}（${row.split === 'calibration' ? '調整日' : '未使用検証日'}） IoU ${row.iou.toFixed(3)} / NISAR ${area(row.water_area_m2)} / 光学 ${area(row.optical_area_m2)}`;
  }
  const selector = document.getElementById('date-select');
  selector.innerHTML = data.map_features.map(feature => `<option value="${feature.optical_date}" ${feature.optical_date === '2026-08-23' ? 'selected' : ''}>${feature.optical_date}（${feature.split === 'calibration' ? '調整' : '検証'}）</option>`).join('');
  selector.addEventListener('change', () => showErrors(selector.value));
  showErrors(selector.value);

  document.getElementById('result-table').innerHTML = dates.map(date => {
    const old = data.results.find(row => row.optical_date === date && row.method === 'per_scene_otsu');
    const tuned = data.results.find(row => row.optical_date === date && row.method === 'tuned_fixed');
    return `<tr><td>${date}</td><td>${tuned.split === 'calibration' ? '調整' : '未使用検証'}</td><td>${area(tuned.optical_area_m2)}</td><td>${area(old.water_area_m2)}</td><td>${area(tuned.water_area_m2)}</td><td>${old.iou.toFixed(3)}</td><td>${tuned.iou.toFixed(3)}</td></tr>`;
  }).join('');

  const distributions = data.drought_feature_distributions;
  const fmt = stat => `${stat.median_db.toFixed(2)} (${stat.p25_db.toFixed(2)}–${stat.p75_db.toFixed(2)})`;
  const distributionRows = [
    ['persistent_optical_water', '恒常水面'], ['optical_drawdown_zone', '光学減水域']
  ];
  document.getElementById('distribution-table').innerHTML = distributionRows.map(([key, label]) => {
    const row = distributions[key];
    return `<tr><td>${label}</td><td>${fmt(row.descending.hh)}</td><td>${fmt(row.descending.hv)}</td><td>${fmt(row.ascending.hh)}</td><td>${fmt(row.ascending.hv)}</td></tr>`;
  }).join('');
}

init().catch(error => { console.error(error); document.getElementById('summary').textContent = 'データの読み込みに失敗しました。'; });
