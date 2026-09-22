'use strict';

// chart.js と共通パレット
const YEAR_COLORS = ['#3b82f6','#f97316','#10b981','#a855f7','#ef4444','#eab308','#06b6d4','#ec4899'];
const RAIN_COLORS = ['#93c5fd','#fdba74','#6ee7b7','#d8b4fe','#fca5a5'];

// 降水量取得対象（直近3年・終端は今日）
const _today = new Date();
const RAIN_END   = _today.toISOString().split('T')[0];
const RAIN_START = `${_today.getFullYear() - 2}-01-01`;
const KITSUKI_LAT = 33.42;
const KITSUKI_LNG = 131.62;

// アラート判定：前年・前前年の同月比で何割以下でアラート
const ALERT_THRESHOLD = 0.80;

let allPonds = [];
let selectedIds = new Set();
let printAll = false;

// ===== メイン =====
async function init() {
  const [pondData, rainData] = await Promise.all([
    fetchPondData(),
    fetchRainfall(),
  ]);
  if (!pondData) return;

  allPonds = pondData.ponds;
  document.getElementById('list-subtitle').textContent =
    `${allPonds.length} 池　／　最終更新: ${pondData.updated_at.replace('T',' ')} UTC`;

  renderAlertBar(allPonds);
  renderRainfallChart(rainData);
  renderPondGrid(allPonds, rainData);
  setupButtons();
}

// ===== データ取得 =====
async function fetchPondData() {
  try {
    const r = await fetch('data.json');
    if (!r.ok) throw new Error();
    return await r.json();
  } catch {
    document.getElementById('loading-msg').textContent = 'data.json の読み込みに失敗しました。';
    return null;
  }
}

async function fetchRainfall() {
  const url = `https://archive-api.open-meteo.com/v1/archive`
    + `?latitude=${KITSUKI_LAT}&longitude=${KITSUKI_LNG}`
    + `&start_date=${RAIN_START}&end_date=${RAIN_END}`
    + `&daily=precipitation_sum&timezone=Asia%2FTokyo`;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error();
    const json = await r.json();
    return processRainfall(json);
  } catch {
    console.warn('降水量データの取得に失敗しました');
    return null;
  }
}

// 日別 → 月別合計 × 年別に集計
function processRainfall(json) {
  const byYear = {};
  (json.daily?.time || []).forEach((dateStr, i) => {
    const val = json.daily.precipitation_sum[i];
    if (val == null) return;
    const [y, m] = dateStr.split('-');
    const year  = parseInt(y, 10);
    const month = parseInt(m, 10);
    if (!byYear[year]) byYear[year] = Array(12).fill(0);
    byYear[year][month - 1] += val;
  });
  return byYear;
}

// ===== アラート判定 =====
function calcAlert(pond) {
  if (!pond.timeseries || pond.timeseries.length === 0) return null;

  const latest = pond.timeseries.slice().reverse().find(d => d.water_area_m2 > 0);
  if (!latest) return null;

  const latestMonth = parseInt(latest.date.split('-')[1], 10);
  const latestYear  = parseInt(latest.date.split('-')[0], 10);
  const latestHa    = latest.water_area_m2 / 10000;

  // 前年・前前年の同月（±1か月）の平均値を取得
  const prevAverages = [1, 2].map(offset => {
    const targetYear = latestYear - offset;
    const vals = pond.timeseries.filter(d => {
      const y = parseInt(d.date.split('-')[0], 10);
      const m = parseInt(d.date.split('-')[1], 10);
      return y === targetYear && Math.abs(m - latestMonth) <= 1 && d.water_area_m2 > 0;
    }).map(d => d.water_area_m2 / 10000);
    return vals.length > 0 ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  });

  const validPrev = prevAverages.filter(v => v != null);
  if (validPrev.length === 0) return null;

  const avgPrev = validPrev.reduce((a, b) => a + b, 0) / validPrev.length;
  if (latestHa < avgPrev * ALERT_THRESHOLD) {
    const ratio = ((latestHa / avgPrev) * 100).toFixed(0);
    return { latestHa, avgPrev, ratio, latestDate: latest.date };
  }
  return null;
}

function renderAlertBar(ponds) {
  const alerts = ponds.filter(p => calcAlert(p) !== null);
  if (alerts.length === 0) return;
  const bar = document.getElementById('alert-bar');
  bar.style.display = 'flex';
  document.getElementById('alert-text').textContent =
    `${alerts.length} 池で前年同時期比 ${Math.round(ALERT_THRESHOLD * 100)}% 未満の水面積低下を検出：`
    + alerts.map(p => p.name).join('、');
}

// ===== 降水量グラフ =====
function renderRainfallChart(rainData) {
  if (!rainData) {
    document.querySelector('.rain-section').style.display = 'none';
    return;
  }
  const MONTHS = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
  const years = Object.keys(rainData).map(Number).sort();

  const traces = years.map((year, i) => ({
    x: MONTHS,
    y: rainData[year].map(v => Math.round(v)),
    name: `${year}年`,
    type: 'bar',
    marker: { color: RAIN_COLORS[i % RAIN_COLORS.length], opacity: 0.8 },
    hovertemplate: `<b>${year}年 %{x}</b><br>降水量: <b>%{y} mm</b><extra></extra>`,
  }));

  Plotly.newPlot('rain-chart', traces, {
    barmode: 'group',
    font: { family: '"Noto Sans JP", sans-serif', size: 11, color: '#4a5568' },
    paper_bgcolor: '#fff', plot_bgcolor: '#fafbfc',
    margin: { t: 10, r: 20, b: 50, l: 55 },
    legend: { orientation: 'h', x: 0, y: -0.22, font: { size: 11 } },
    xaxis: { gridcolor: '#edf2f7' },
    yaxis: { title: { text: '月間降水量 (mm)', standoff: 10 }, gridcolor: '#edf2f7', rangemode: 'tozero' },
    hovermode: 'x unified',
  }, { responsive: true, displayModeBar: false });
}

// ===== 池グラフ一覧 =====
function renderPondGrid(ponds, rainData) {
  document.getElementById('loading-msg').remove();
  const grid = document.getElementById('pond-grid');

  ponds.forEach(pond => {
    const alert = calcAlert(pond);
    const card = document.createElement('div');
    card.className = 'pond-card';
    card.dataset.id = pond.id;

    const latest = pond.timeseries?.slice().reverse().find(d => d.water_area_m2 > 0);
    const latestStr = latest
      ? `${(latest.water_area_m2 / 10000).toFixed(4)} ha（${latest.date}）`
      : 'データなし';

    card.innerHTML = `
      <div class="card-header no-print">
        <label class="card-check">
          <input type="checkbox" class="pond-checkbox" value="${pond.id}" />
          <span class="check-label">印刷対象</span>
        </label>
        <a class="card-link" href="pond.html?id=${pond.id}" target="_blank">詳細 →</a>
      </div>
      <div class="card-title-row">
        ${alert ? `<span class="alert-badge" title="前年同時期比 ${alert.ratio}%">⚠️ 低下</span>` : ''}
        <h3 class="card-name">${pond.name}</h3>
      </div>
      <div class="card-meta">${pond.tiiki}　${pond.ooaza}　／　登録 ${pond.area_ha ?? '—'} ha</div>
      <div class="card-latest">最新水面: <strong>${latestStr}</strong></div>
      <div class="card-chart" id="chart-${pond.id}"></div>
    `;
    grid.appendChild(card);

    renderMiniChart(pond, `chart-${pond.id}`, rainData);
  });

  // チェックボックスイベント
  grid.addEventListener('change', e => {
    if (!e.target.classList.contains('pond-checkbox')) return;
    if (e.target.checked) selectedIds.add(e.target.value);
    else selectedIds.delete(e.target.value);
  });
}

function renderMiniChart(pond, chartId, rainData) {
  const byYear = {};
  (pond.timeseries || [])
    .filter(d => d.water_area_m2 > 0)
    .forEach(d => {
      const [y, m, day] = d.date.split('-');
      const year = parseInt(y, 10);
      if (!byYear[year]) byYear[year] = [];
      byYear[year].push({ xDate: `2000-${m}-${day}`, ha: d.water_area_m2 / 10000, origDate: d.date });
    });

  const years = Object.keys(byYear).map(Number).sort();
  const traces = years.map((year, i) => {
    const pts = byYear[year].sort((a, b) => a.xDate.localeCompare(b.xDate));
    return {
      x: pts.map(p => p.xDate),
      y: pts.map(p => p.ha),
      customdata: pts.map(p => p.origDate),
      mode: 'lines+markers',
      name: `${year}年`,
      line:   { color: YEAR_COLORS[i % YEAR_COLORS.length], width: 1.5 },
      marker: { color: YEAR_COLORS[i % YEAR_COLORS.length], size: 4 },
      hovertemplate: `<b>${year}年</b> %{customdata}<br>%{y:.4f} ha<extra></extra>`,
    };
  });

  // 降水量を第2y軸に追加
  if (rainData) {
    const MONTHS_X = ['2000-01-15','2000-02-15','2000-03-15','2000-04-15','2000-05-15',
                      '2000-06-15','2000-07-15','2000-08-15','2000-09-15','2000-10-15',
                      '2000-11-15','2000-12-15'];
    const rainYears = Object.keys(rainData).map(Number).sort();
    rainYears.forEach((year, i) => {
      traces.push({
        x: MONTHS_X,
        y: rainData[year].map(v => Math.round(v)),
        name: `降水 ${year}年`,
        type: 'bar',
        yaxis: 'y2',
        marker: { color: RAIN_COLORS[i % RAIN_COLORS.length], opacity: 0.3 },
        hovertemplate: `降水 ${year}年 %{y}mm<extra></extra>`,
        showlegend: false,
      });
    });
  }

  const dataMaxHa = Math.max(0, ...traces.filter(t => !t.name.startsWith('降水')).flatMap(t => t.y));
  const yMax = dataMaxHa > 0 ? dataMaxHa * 1.3 : 1;

  Plotly.newPlot(chartId, traces, {
    font: { family: '"Noto Sans JP", sans-serif', size: 10, color: '#4a5568' },
    paper_bgcolor: '#fff', plot_bgcolor: '#fafbfc',
    margin: { t: 6, r: 50, b: 36, l: 50 },
    showlegend: false,
    xaxis: {
      type: 'date', tickformat: '%-m月', dtick: 'M2',
      gridcolor: '#edf2f7', tickfont: { size: 9 },
    },
    yaxis: {
      title: { text: 'ha', standoff: 4, font: { size: 9 } },
      range: [0, yMax], gridcolor: '#edf2f7', tickfont: { size: 9 }, tickformat: '.2f',
    },
    yaxis2: {
      title: { text: 'mm', standoff: 4, font: { size: 9 } },
      overlaying: 'y', side: 'right',
      tickfont: { size: 9 }, showgrid: false,
      rangemode: 'tozero',
    },
    hovermode: 'closest',
    barmode: 'group',
  }, { responsive: true, displayModeBar: false });
}

// ===== ボタン操作 =====
function setupButtons() {
  document.getElementById('btn-select-all').addEventListener('click', () => {
    document.querySelectorAll('.pond-checkbox').forEach(cb => {
      cb.checked = true;
      selectedIds.add(cb.value);
    });
  });

  document.getElementById('btn-deselect-all').addEventListener('click', () => {
    document.querySelectorAll('.pond-checkbox').forEach(cb => {
      cb.checked = false;
    });
    selectedIds.clear();
  });

  document.getElementById('btn-print-all').addEventListener('click', () => {
    document.querySelectorAll('.pond-card').forEach(c => c.classList.remove('print-hidden'));
    printAll = true;
    window.print();
  });

  document.getElementById('btn-print-selected').addEventListener('click', () => {
    if (selectedIds.size === 0) {
      alert('印刷する池を選択してください。');
      return;
    }
    document.querySelectorAll('.pond-card').forEach(c => {
      c.classList.toggle('print-hidden', !selectedIds.has(c.dataset.id));
    });
    printAll = false;
    window.print();
  });

  // 印刷後にクラスをリセット
  window.addEventListener('afterprint', () => {
    document.querySelectorAll('.pond-card').forEach(c => c.classList.remove('print-hidden'));
  });
}

document.addEventListener('DOMContentLoaded', init);
