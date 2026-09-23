'use strict';

(function () {
  const orbitLabels = { ascending: '昇交', descending: '降交' };
  const order = [
    'inside_core', 'inside_20_40', 'inside_0_20',
    'outside_0_20', 'outside_20_50', 'outside_50_100',
  ];

  function render(data) {
    const section = document.getElementById('boundary-diagnostic');
    const comparison = data.comparison;
    const wet = comparison.wet;
    const dry = comparison.dry;
    section.hidden = false;
    document.getElementById('boundary-description').textContent =
      'OSMポリゴンを水際の正解ではなく基準線として、内外100 mを6帯に分けた試験解析です。';
    document.getElementById('boundary-summary').innerHTML = `
      <div class="summary-card"><span>比較軌道</span><strong>${orbitLabels[comparison.orbit]}</strong><small>同一軌道のみ</small></div>
      <div class="summary-card"><span>多雨側</span><strong>${wet.date}</strong><small>前7日 ${wet.rain_7d_mm.toFixed(1)} mm</small></div>
      <div class="summary-card"><span>少雨側</span><strong>${dry.date}</strong><small>前7日 ${dry.rain_7d_mm.toFixed(1)} mm</small></div>
      <div class="summary-card"><span>一次判定</span><strong>${data.assessment.label}</strong><small>外側0–20 m 対 50–100 m</small></div>`;

    const summaries = data.band_summary.filter(
      item => item.orbit_direction === comparison.orbit.toUpperCase()
    );
    const ranges = layer => order.map(band =>
      summaries.find(item => item.band === band && item.layer === layer)?.median_range_db ?? null
    );
    Plotly.react('band-chart', [
      { x: order.map(key => data.band_labels[key]), y: ranges('HHHH'), type: 'bar', name: 'HH', marker: { color: '#1677b8' } },
      { x: order.map(key => data.band_labels[key]), y: ranges('HVHV'), type: 'bar', name: 'HV', marker: { color: '#df7d24' } },
    ], {
      barmode: 'group', font: { family: 'Noto Sans JP', size: 9 },
      margin: { t: 15, r: 20, b: 80, l: 50 }, paper_bgcolor: '#fff', plot_bgcolor: '#fbfcfb',
      yaxis: { title: '変動幅 (dB)', gridcolor: '#e8eeea' }, xaxis: { tickangle: -28 },
      legend: { orientation: 'h', x: 0, y: 1.12 },
    }, { responsive: true, displayModeBar: false });

    const heatmaps = [
      { z: data.grid.HHHH, x: data.grid.x, y: data.grid.y, type: 'heatmap', xaxis: 'x', yaxis: 'y', zmin: -4, zmax: 4, zmid: 0, colorscale: 'RdBu', reversescale: true, colorbar: { title: 'dB', x: .46, len: .75 }, name: 'HH' },
      { z: data.grid.HVHV, x: data.grid.x, y: data.grid.y, type: 'heatmap', xaxis: 'x2', yaxis: 'y2', zmin: -4, zmax: 4, zmid: 0, colorscale: 'RdBu', reversescale: true, showscale: false, name: 'HV' },
    ];
    data.pond_boundary_paths.forEach(path => {
      const x = path.map(point => point[0]);
      const y = path.map(point => point[1]);
      heatmaps.push({ x, y, mode: 'lines', line: { color: '#111', width: 2 }, hoverinfo: 'skip', showlegend: false, xaxis: 'x', yaxis: 'y' });
      heatmaps.push({ x, y, mode: 'lines', line: { color: '#111', width: 2 }, hoverinfo: 'skip', showlegend: false, xaxis: 'x2', yaxis: 'y2' });
    });
    Plotly.react('boundary-diff-chart', heatmaps, {
      font: { family: 'Noto Sans JP', size: 9 }, margin: { t: 35, r: 15, b: 30, l: 35 },
      paper_bgcolor: '#fff', plot_bgcolor: '#fbfcfb',
      annotations: [
        { text: 'HH 少雨 − 多雨', x: .23, y: 1.08, xref: 'paper', yref: 'paper', showarrow: false, font: { size: 12 } },
        { text: 'HV 少雨 − 多雨', x: .77, y: 1.08, xref: 'paper', yref: 'paper', showarrow: false, font: { size: 12 } },
      ],
      xaxis: { domain: [0, .46], scaleanchor: 'y', showticklabels: false },
      yaxis: { domain: [0, 1], showticklabels: false },
      xaxis2: { domain: [.54, 1], scaleanchor: 'y2', showticklabels: false },
      yaxis2: { domain: [0, 1], showticklabels: false },
    }, { responsive: true, displaylogo: false });

    const cells = (band, layer, key) =>
      data.changed_fractions.find(item => item.band === band && item.layer === layer)?.[key];
    document.getElementById('boundary-table-body').innerHTML = order.map(band => `
      <tr><td>${data.band_labels[band]}</td>
      <td>${((cells(band, 'HHHH', 'changed_fraction_abs_1_5_db') ?? 0) * 100).toFixed(1)}%</td>
      <td>${((cells(band, 'HVHV', 'changed_fraction_abs_1_5_db') ?? 0) * 100).toFixed(1)}%</td>
      <td>${cells(band, 'HHHH', 'median_change_db')?.toFixed(2) ?? '—'} dB</td>
      <td>${cells(band, 'HVHV', 'median_change_db')?.toFixed(2) ?? '—'} dB</td></tr>`).join('');
  }

  async function loadWhenReady() {
    if (typeof pond === 'undefined' || !pond) {
      window.setTimeout(loadWhenReady, 100);
      return;
    }
    try {
      const response = await fetch(`data/boundary_${pond.id}.json`);
      if (response.ok) render(await response.json());
    } catch (error) {
      console.warn('境界診断データを表示できません', error);
    }
  }

  loadWhenReady();
})();
