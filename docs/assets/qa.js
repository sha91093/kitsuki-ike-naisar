'use strict';

const map = L.map('qa-map', { zoomControl: true });
const aerial = L.tileLayer(
  'https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg',
  { maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>' }
).addTo(map);
const standard = L.tileLayer(
  'https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png',
  { maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">国土地理院</a>' }
);
L.control.layers({ '航空画像': aerial, '標準地図': standard }).addTo(map);

function signLabel(feature) {
  const share = feature.properties.negative_share;
  if (feature.properties.sign === 'negative') return '負方向';
  if (share > 0) return `混合（負${Math.round(share * 100)}%）`;
  return '混合';
}

async function init() {
  const response = await fetch('data/boundary_27.json');
  if (!response.ok) throw new Error('境界診断データを読み込めません');
  const data = await response.json();
  const qa = data.imagery_qa;
  const pond = L.geoJSON(qa.pond_geometry, {
    style: { color: '#111', weight: 3, fillOpacity: 0, dashArray: '7 4' }
  }).addTo(map);
  const componentLayers = new Map();
  const changes = L.geoJSON(qa.components, {
    style: feature => ({
      color: feature.properties.negative_share > 0 ? '#1976d2' : '#00a86b',
      weight: 3,
      fillColor: feature.properties.negative_share > 0 ? '#1976d2' : '#00a86b',
      fillOpacity: .32
    }),
    onEachFeature: (feature, layer) => {
      const p = feature.properties;
      layer.bindTooltip(`変化領域 ${p.component_id}<br>${p.area_m2.toFixed(0)} m² / ${signLabel(feature)}`);
      componentLayers.set(String(p.component_id), layer);
      const center = layer.getBounds().getCenter();
      L.marker(center, {
        icon: L.divIcon({ className: 'component-label', html: String(p.component_id), iconSize: [24, 24], iconAnchor: [12, 12] })
      }).addTo(map);
    }
  }).addTo(map);
  const group = L.featureGroup([pond, changes]);
  map.fitBounds(group.getBounds(), { padding: [35, 35], maxZoom: 18 });

  document.getElementById('component-list').innerHTML = qa.components.features.map(feature => {
    const p = feature.properties;
    const mapsUrl = `https://www.google.com/maps/@${p.latitude},${p.longitude},20z/data=!3m1!1e3`;
    return `<article class="component-card" data-component="${p.component_id}">
      <h2>変化領域 ${p.component_id}<span class="sign ${p.negative_share > 0 ? 'negative' : ''}">${signLabel(feature)}</span></h2>
      <p><strong>${p.classification_label}</strong> / ${p.area_m2.toFixed(0)} m²<br>${p.review_note}<br>${p.latitude.toFixed(6)}, ${p.longitude.toFixed(6)}</p>
      <a href="${mapsUrl}" target="_blank" rel="noopener">Google Mapsでも確認 ↗</a>
    </article>`;
  }).join('');
  document.querySelectorAll('.component-card').forEach(card => {
    card.addEventListener('click', event => {
      if (event.target.closest('a')) return;
      const layer = componentLayers.get(card.dataset.component);
      map.fitBounds(layer.getBounds(), { padding: [80, 80], maxZoom: 20 });
      layer.openTooltip();
    });
  });
}

init().catch(error => {
  console.error(error);
  document.getElementById('component-list').textContent = 'データの読み込みに失敗しました。';
});
