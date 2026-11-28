import { init, use } from "echarts/core";
import { BarChart, HeatmapChart, LineChart } from "echarts/charts";
import { GridComponent, LegendScrollComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

use([BarChart, HeatmapChart, LineChart, GridComponent, LegendScrollComponent, TooltipComponent, VisualMapComponent, CanvasRenderer]);

export { init };
export type ECharts = ReturnType<typeof init>;
export type { SeriesOption } from "echarts";
