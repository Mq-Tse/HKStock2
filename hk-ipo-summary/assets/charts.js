(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var green = '#159a74';
  var orange = '#f5a623';
  var grey = '#8893a1';

  var common = { renderer: 'svg', animation: false };

  // 图 1 · 年度 IPO 数量
  var years = [
    ['2010', 77], ['2011', 64], ['2012', 46], ['2013', 76], ['2014', 93],
    ['2015', 102], ['2016', 103], ['2017', 138], ['2018', 198], ['2019', 155],
    ['2020', 146], ['2021', 95], ['2022', 89], ['2023', 70], ['2024', 70],
    ['2025', 117], ['2026', 124]
  ];
  var chart1 = echarts.init(document.getElementById('chart-years'), null, common);
  chart1.setOption({
    grid: { left: 50, right: 20, top: 20, bottom: 40 },
    tooltip: {
      appendToBody: true, trigger: 'axis',
      formatter: p => `${p[0].axisValue} 年<br>IPO 数量 <strong>${p[0].value}</strong> 只`
    },
    xAxis: {
      type: 'category', data: years.map(y => y[0]),
      axisLine: { lineStyle: { color: rule } },
      axisLabel: { color: muted, fontSize: 12, interval: 0, rotate: 0 }
    },
    yAxis: {
      type: 'value', name: '只', nameTextStyle: { color: muted, fontSize: 12 },
      splitLine: { lineStyle: { color: rule, type: 'dashed' } },
      axisLabel: { color: muted }
    },
    series: [{
      type: 'bar', data: years.map(y => y[1]),
      itemStyle: {
        color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: accent }, { offset: 1, color: accent + '66' }] },
        borderRadius: [4, 4, 0, 0]
      },
      barMaxWidth: 28
    }]
  });

  // 图 2 · 复权因子偏离分布饼图
  var factorBuckets = [
    { name: '无偏离 (<1%)', value: 467 },
    { name: '小幅 (1-10%)', value: 409 },
    { name: '中度 (10-50%)', value: 247 },
    { name: '大幅 (50-500%)', value: 257 },
    { name: '极端 (≥500%)', value: 153 }
  ];
  var totalFactors = 1533;
  factorBuckets.forEach(b => b.pct = (b.value / totalFactors * 100).toFixed(1));

  var chart2 = echarts.init(document.getElementById('chart-factors'), null, common);
  chart2.setOption({
    grid: { left: 20, right: 20, top: 20, bottom: 20 },
    tooltip: {
      appendToBody: true, trigger: 'item',
      formatter: p => `${p.name}<br><strong>${p.value}</strong> 只（${p.percent}%）`
    },
    legend: {
      orient: 'vertical', right: 20, top: 'center',
      textStyle: { color: ink, fontSize: 12 },
      itemWidth: 12, itemHeight: 12
    },
    series: [{
      type: 'pie', radius: ['45%', '72%'], center: ['38%', '50%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: bg2, borderWidth: 2 },
      label: { show: true, formatter: p => `${p.percent.toFixed(0)}%`, fontSize: 11, color: ink },
      labelLine: { show: true, length: 8, length2: 6, lineStyle: { color: muted } },
      color: [green, accent, orange, accent2, '#c43e3e'],
      data: factorBuckets
    }]
  });

  // 图 3 · 暗盘状态分布饼图（数据清洗后三分类）
  var greyBuckets = [
    { name: '已收录（ok）', value: 1479 },
    { name: '不适用（介绍上市等）', value: 142 },
    { name: '未覆盖（not_found）', value: 142 }
  ];

  var chart3 = echarts.init(document.getElementById('chart-grey'), null, common);
  chart3.setOption({
    grid: { left: 20, right: 20, top: 20, bottom: 20 },
    tooltip: {
      appendToBody: true, trigger: 'item',
      formatter: p => `${p.name}<br><strong>${p.value}</strong> 只（${p.percent}%）`
    },
    legend: {
      orient: 'vertical', right: 20, top: 'center',
      textStyle: { color: ink, fontSize: 12 },
      itemWidth: 12, itemHeight: 12
    },
    series: [{
      type: 'pie', radius: ['45%', '72%'], center: ['38%', '50%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: bg2, borderWidth: 2 },
      label: { show: true, formatter: p => `${p.percent.toFixed(0)}%`, fontSize: 11, color: ink },
      labelLine: { show: true, length: 8, length2: 6, lineStyle: { color: muted } },
      color: [green, grey, accent2, orange, muted],
      data: greyBuckets
    }]
  });

  window.addEventListener('resize', function() {
    chart1.resize(); chart2.resize(); chart3.resize();
  });
})();
