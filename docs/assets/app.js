'use strict';

const KITSUKI_CENTER = [33.416, 131.621];
const INITIAL_ZOOM = 12;

let map;
let pondLayers = {};
let selectedPondId = null;
let pondData = {};

async function init() {
  map = L.map('map').setView(KITSUKI_CENTER, INITIAL_ZOOM);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }).addTo(map);

  try {
    const resp = await fetch('data.json');
    if (!resp.ok) throw new Error(`data.json 読み込み失敗: ${resp.status}`);
    const data = await resp.json();
    renderData(data);
  } catch (e) {
    console.error(e);
    document.getElementById('sidebar-hint').textContent =
      'データの読み込みに失敗しました。';
  }
}

function renderData(data) {
  if (data.updated_at) {
    document.getElementById('updated-at').textContent =
      `最終更新: ${data.updated_at.replace('T', ' ')} UTC`;
  }

  data.ponds.forEach(pond => { pondData[pond.id] = pond; });

  data.ponds.forEach(pond => {
    if (pond.lat == null || pond.lng == null) return;

    const layer = L.circleMarker([pond.lat, pond.lng], {
      radius: 8,
      color: '#1565c0',
      fillColor: '#42a5f5',
      fillOpacity: 0.75,
      weight: 1.5,
    });

    layer.bindTooltip(`${pond.name}（${pond.tiiki} ${pond.ooaza}）`,
      { permanent: false, direction: 'top', offset: [0, -8] });
    layer.on('click', () => selectPond(pond.id));
    layer.addTo(map);

    pondLayers[pond.id] = layer;
  });
}

function selectPond(pondId) {
  if (selectedPondId && pondLayers[selectedPondId]) {
    pondLayers[selectedPondId].setStyle({ color: '#1565c0', fillColor: '#42a5f5' });
  }

  selectedPondId = pondId;
  const pond = pondData[pondId];
  if (!pond) return;

  pondLayers[pondId]?.setStyle({ color: '#c62828', fillColor: '#ef9a9a' });

  // 直近の非ゼロデータを最新値として使用
  const latest = pond.timeseries?.slice().reverse().find(d => d.water_area_m2 > 0)
    ?? pond.timeseries?.[pond.timeseries.length - 1]
    ?? null;
  const latestHa = latest ? (latest.water_area_m2 / 10000).toFixed(4) : null;

  document.getElementById('pond-info').innerHTML = `
    <h2>${pond.name}</h2>
    <table class="info-table">
      <tr><th>地域</th><td>${pond.tiiki || '—'}</td></tr>
      <tr><th>大字</th><td>${pond.ooaza || '—'}</td></tr>
      <tr><th>登録面積</th><td>${pond.area_ha != null ? pond.area_ha + ' ha' : '—'}</td></tr>
      <tr><th>最新水面</th><td><strong>${latestHa ? latestHa + ' ha' : 'データなし'}</strong>${latest ? `<br><span class="date-label">${latest.date}</span>` : ''}</td></tr>
    </table>
    <a class="chart-btn" href="pond.html?id=${pondId}" target="_blank">
      📈 年別比較グラフを見る
    </a>
  `;

  if (isMobile()) openSidebar();
}

function isMobile() {
  return window.innerWidth <= 700;
}

function openSidebar() {
  document.getElementById('sidebar').classList.add('mobile-open');
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('mobile-open');
}

document.addEventListener('DOMContentLoaded', () => {
  init();

  document.getElementById('sidebar-close').addEventListener('click', closeSidebar);

  // 地図の空白部分タップでサイドバーを閉じる
  document.getElementById('map').addEventListener('click', e => {
    if (isMobile() && e.target.closest('#map') && !e.target.closest('.leaflet-marker-icon, .leaflet-interactive')) {
      closeSidebar();
    }
  });
});
