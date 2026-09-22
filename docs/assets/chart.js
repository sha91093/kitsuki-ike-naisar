'use strict';

const YEAR_COLORS = [
  '#3b82f6', // blue
  '#f97316', // orange
  '#10b981', // emerald
  '#a855f7', // purple
  '#ef4444', // red
  '#eab308', // yellow
  '#06b6d4', // cyan
  '#ec4899', // pink
];

const ORBITS = ['descending', 'ascending'];
const ORBIT_LABELS = { descending: '降交軌道', ascending: '昇交軌道' };

// デフォルトで表示する年数（これより古い年は凡例クリックで表示）
const DEFAULT_VISIBLE_YEARS = 2;

let currentPond = null;
let currentOrbit = 'descending';
let chartInitialized = false;

async function init() {
  const params = new URLSearchParams(location.search);
  const pondId = params.get('id');
  if (!pondId) {
    document.getElementById('pond-name').textContent = 'IDが指定されていません';
    return;
  }

  let data;
  try {
    const resp = await fetch('data.json');
    if (!resp.ok) throw new Error('data.json 読み込み失敗');
    data = await resp.json();
  } catch (e) {
    document.getElementById('pond-name').textContent = 'データ読み込みエラー';
    console.error(e);
    return;
  }

  const pond = data.ponds.find(p => String(p.id) === String(pondId));
  if (!pond) {
    document.getElementById('pond-name').textContent = `ID ${pondId} の池が見つかりません`;
    return;
  }

  currentPond = pond;
  renderMeta(pond);
  setupOrbitTabs(pond);
}

/**
 * 指定軌道の観測を {年: [{date, ha}, ...]} で返す。
 * orbit_data を持たない旧スキーマの data.json では timeseries を降交軌道として扱う。
 */
function getOrbitYears(pond, orbit) {
  const byYear = {};

  const orbitData = pond.orbit_data && pond.orbit_data[orbit];
  if (orbitData) {
    Object.keys(orbitData).forEach(year => {
      const pts = (orbitData[year] || [])
        .filter(d => d.area_m2 > 0)
        .map(d => ({ date: d.date, ha: d.area_m2 / 10000 }));
      if (pts.length > 0) byYear[year] = pts;
    });
    return byYear;
  }

  if (!pond.orbit_data && orbit === 'descending') {
    (pond.timeseries || [])
      .filter(d => d.water_area_m2 > 0)
      .forEach(d => {
        const year = d.date.slice(0, 4);
        if (!byYear[year]) byYear[year] = [];
        byYear[year].push({ date: d.date, ha: d.water_area_m2 / 10000 });
      });
  }
  return byYear;
}

function hasOrbitData(pond, orbit) {
  return Object.keys(getOrbitYears(pond, orbit)).length > 0;
}

function renderMeta(pond) {
  document.title = `${pond.name} — 杵築市 水面面積モニタリング`;
  document.getElementById('pond-name').textContent = pond.name;

  // 直近の非ゼロデータを最新値として使用
  const latest = pond.timeseries?.slice().reverse().find(d => d.water_area_m2 > 0)
    ?? pond.timeseries?.[pond.timeseries.length - 1]
    ?? null;
  const latestHa = latest ? (latest.water_area_m2 / 10000).toFixed(4) : null;

  const chips = [
    { label: '地域',         value: pond.tiiki   || '—' },
    { label: '大字',         value: pond.ooaza   || '—' },
    { label: '登録面積',     value: pond.area_ha != null ? `${pond.area_ha} ha` : '—' },
    { label: '最新水面面積', value: latestHa ? `${latestHa} ha（${latest.date}）` : 'データなし' },
  ];

  document.getElementById('meta-bar').innerHTML = chips.map(c => `
    <div class="meta-chip">
      <span class="label">${c.label}</span>
      <span class="value">${c.value}</span>
    </div>
  `).join('');
}

function setupOrbitTabs(pond) {
  const tabs = document.querySelectorAll('#orbit-tabs .tab');

  tabs.forEach(tab => {
    const orbit = tab.dataset.orbit;
    if (!hasOrbitData(pond, orbit)) {
      tab.disabled = true;
      tab.title = `${ORBIT_LABELS[orbit]}のデータはまだありません`;
      tab.innerHTML = `${ORBIT_LABELS[orbit]}<span class="tab-note">（データなし）</span>`;
    }
    tab.addEventListener('click', () => {
      if (tab.disabled || tab.classList.contains('active')) return;
      selectOrbit(orbit);
    });
  });

  // データのある軌道を初期表示に（降交を優先）
  const initial = ORBITS.find(o => hasOrbitData(pond, o)) ?? 'descending';
  selectOrbit(initial);
}

function selectOrbit(orbit) {
  currentOrbit = orbit;

  document.querySelectorAll('#orbit-tabs .tab').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.orbit === orbit);
  });

  const chartEl = document.getElementById('chart');
  const noDataEl = document.getElementById('no-data');

  if (!hasOrbitData(currentPond, orbit)) {
    chartEl.style.display = 'none';
    noDataEl.textContent = `この池の${ORBIT_LABELS[orbit]}の観測データがまだありません`;
    noDataEl.style.display = 'flex';
    return;
  }

  chartEl.style.display = '';
  noDataEl.style.display = 'none';
  renderChart(currentPond, orbit);
}

/** 降交・昇交で同じ年が同じ色になるよう、池全体の年リストから色を決める */
function yearColorMap(pond) {
  const years = new Set();
  ORBITS.forEach(o => Object.keys(getOrbitYears(pond, o)).forEach(y => years.add(y)));
  const sorted = [...years].sort();
  const map = {};
  sorted.forEach((y, i) => { map[y] = YEAR_COLORS[i % YEAR_COLORS.length]; });
  return map;
}

function buildTraces(pond, orbit) {
  const byYear = getOrbitYears(pond, orbit);
  const colors = yearColorMap(pond);
  const years = Object.keys(byYear).sort();

  // 直近 DEFAULT_VISIBLE_YEARS 年分だけ初期表示、それより古い年は凡例クリックで表示
  const visibleFrom = years.slice(-DEFAULT_VISIBLE_YEARS)[0];

  const traces = years.map(year => {
    const color = colors[year];
    const pts = byYear[year].slice().sort((a, b) => a.date.localeCompare(b.date));
    return {
      // 年をまたいで重ねるため、X軸は「月日」だけを 2000 年に正規化する
      x: pts.map(p => `2000-${p.date.slice(5)}`),
      y: pts.map(p => p.ha),
      customdata: pts.map(p => p.date),
      mode: 'lines+markers',
      name: `${year}年`,
      visible: year >= visibleFrom ? true : 'legendonly',
      line:   { color, width: 2 },
      marker: { color, size: 6, symbol: 'circle',
                line: { color: '#fff', width: 1.5 } },
      hovertemplate:
        `<b>${year}年</b> %{customdata}<br>水面面積: <b>%{y:.4f} ha</b><extra></extra>`,
    };
  });

  // 登録面積を参照線として追加（y軸レンジ計算には含めない）
  if (pond.area_ha != null) {
    traces.push({
      x: ['2000-01-01', '2000-12-31'],
      y: [pond.area_ha, pond.area_ha],
      mode: 'lines',
      name: `登録面積 (${pond.area_ha} ha)`,
      line: { color: '#cbd5e1', width: 1.5, dash: 'dot' },
      hoverinfo: 'skip',
    });
  }

  return traces;
}

function buildLayout(traces, orbit) {
  // 実データの最大値からy軸レンジを決定（参照線は除外）
  const dataMaxHa = Math.max(
    0,
    ...traces
      .filter(t => !t.name.startsWith('登録面積'))
      .flatMap(t => t.y.filter(v => v != null))
  );
  const yMax = dataMaxHa > 0 ? dataMaxHa * 1.25 : 1;

  return {
    title: {
      text: `NISAR L-band SAR ${ORBIT_LABELS[orbit]}`,
      font: { size: 13, color: '#718096' },
      x: 0.01, xanchor: 'left', y: 0.98, yanchor: 'top',
    },
    font: { family: '"Noto Sans JP", sans-serif', size: 12, color: '#4a5568' },
    paper_bgcolor: '#fff',
    plot_bgcolor:  '#fafbfc',
    margin: { t: 40, r: 20, b: 70, l: 70 },
    legend: {
      orientation: 'h',
      x: 0, y: -0.2,
      font: { size: 12 },
    },
    xaxis: {
      title: { text: '月', standoff: 14 },
      type: 'date',
      tickformat: '%-m月',
      dtick: 'M1',
      range: ['2000-01-01', '2000-12-31'],
      gridcolor: '#edf2f7',
      linecolor: '#e2e8f0',
      tickfont: { size: 12 },
    },
    yaxis: {
      title: { text: '水面面積 (ha)', standoff: 12 },
      gridcolor: '#edf2f7',
      linecolor: '#e2e8f0',
      range: [0, yMax],
      tickfont: { size: 12 },
      tickformat: '.3f',
    },
    hovermode: 'closest',
    hoverlabel: {
      bgcolor: '#1a202c',
      bordercolor: '#1a202c',
      font: { color: '#fff', size: 12, family: '"Noto Sans JP", sans-serif' },
    },
  };
}

const CHART_CONFIG = {
  responsive: true,
  displayModeBar: true,
  modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
  displaylogo: false,
  locale: 'ja',
};

function renderChart(pond, orbit) {
  const traces = buildTraces(pond, orbit);
  const layout = buildLayout(traces, orbit);

  // 初回のみ newPlot、以降のタブ切り替えは react で差し替える
  if (chartInitialized) {
    Plotly.react('chart', traces, layout, CHART_CONFIG);
  } else {
    Plotly.newPlot('chart', traces, layout, CHART_CONFIG);
    chartInitialized = true;
  }
}

document.addEventListener('DOMContentLoaded', init);
