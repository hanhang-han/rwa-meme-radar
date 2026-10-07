// Styling never rebuilds the chart, replaces candle history or fits its range.
export function updateChartPalette({ chart, candleSeries, lineSeries, volumeSeries, priceLine, volumeData }, colors) {
  if (!chart) return;
  chart.applyOptions({layout:{background:{color:'transparent'},textColor:colors.muted},grid:{vertLines:{color:colors.grid},horzLines:{color:colors.grid}},
    rightPriceScale:{borderColor:colors.border},timeScale:{borderColor:colors.border},crosshair:{vertLine:{color:colors.muted,labelBackgroundColor:colors.surface},horzLine:{color:colors.muted,labelBackgroundColor:colors.surface}}});
  candleSeries?.applyOptions({upColor:colors.up,downColor:colors.down,borderUpColor:colors.up,borderDownColor:colors.down,wickUpColor:colors.up,wickDownColor:colors.down});
  lineSeries?.applyOptions({lineColor:colors.up,topColor:colors.areaTop,bottomColor:colors.areaBottom});
  if(volumeSeries&&volumeData)volumeSeries.setData(volumeData);
  priceLine?.applyOptions({color:candleSeries?colors.accent:colors.up});
}
