"""Create bounded charts from successful SQL results, not model-generated code."""

import json
import math
import os
import textwrap
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from uuid import uuid4

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from pydantic import BaseModel, Field

from app.agent.state import AgentStep


CHART_DIR = Path(__file__).resolve().parents[2] / "outputs" / "charts"


class ChartToolError(ValueError):
    """The selected SQL result cannot be plotted safely."""


class ChartRequest(BaseModel):
    chart_type: Literal["line", "bar", "scatter"]
    source_type: Literal["sql", "attribution", "sequential_attribution"] = "sql"
    source_round: int = Field(ge=1)
    x_column: str = Field(min_length=1)
    y_column: str = Field(min_length=1)
    title: str = Field(default="数据图表", max_length=240)
    x_label: str | None = Field(default=None, max_length=80)
    y_label: str | None = Field(default=None, max_length=80)


class ChartTool:
    def run(self, request: ChartRequest, steps: list[AgentStep]) -> str:
        source = next(
            (step for step in steps if step.round == request.source_round
             and step.action == ("sql" if request.source_type == "sql" else "python")),
            None,
        )
        if source is None:
            raise ChartToolError("source_round 必须指向相应的 SQL 或归因工具结果。")
        try:
            observation = json.loads(source.observation)
        except json.JSONDecodeError as exc:
            raise ChartToolError("该 SQL 轮次没有可绘图的查询结果。") from exc
        rows = observation.get("rows") if isinstance(observation, dict) else None
        if request.source_type != "sql":
            result = observation.get("result") if isinstance(observation, dict) else None
            if (
                not isinstance(observation, dict) or observation.get("operation") != request.source_type or not isinstance(result, dict)
                or result.get("reconciled") is not True or request.chart_type != "bar"
                or request.x_column != "dimension" or request.y_column != "change"
            ):
                raise ChartToolError("贡献图必须使用核验通过的归因结果与维度变化额。")
            rows = result.get("details")
            if request.source_type == "sequential_attribution":
                comparisons = result.get("comparisons")
                if not isinstance(comparisons, list) or not comparisons or any(
                    not isinstance(pair, dict) or pair.get("reconciled") is not True
                    or not isinstance(pair.get("details"), list) for pair in comparisons
                ):
                    raise ChartToolError("逐期贡献图缺少已核验的相邻时期明细。")
                rows = [{"dimension": f"{pair['baseline_period']}→{pair['current_period']} · {item['dimension']}",
                         "change": item["change"]} for pair in comparisons for item in pair["details"]]
        if not isinstance(rows, list) or not rows:
            raise ChartToolError("SQL 结果为空，无法绘图。")
        if observation.get("truncated") or len(rows) > 30:
            raise ChartToolError("绘图最多支持 30 行完整数据，请先用 SQL 汇总或缩小范围。")

        if request.chart_type == "scatter":
            xs: list[float] = []
            ys: list[float] = []
            for row in rows:
                if not isinstance(row, dict) or request.x_column not in row or request.y_column not in row:
                    raise ChartToolError("散点图的 x_column 或 y_column 不在 SQL 结果中。")
                try:
                    x = float(Decimal(str(row[request.x_column])))
                    y = float(Decimal(str(row[request.y_column])))
                except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
                    raise ChartToolError("散点图坐标包含非数值或空值。") from exc
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise ChartToolError("散点图坐标不能包含无穷大或 NaN。")
                xs.append(x)
                ys.append(y)

            font = self._chinese_font()
            font_kwargs = {"fontproperties": font} if font else {}
            figure = Figure(figsize=(9, 5.5), layout="constrained")
            FigureCanvasAgg(figure)
            axis = figure.subplots()
            axis.scatter(xs, ys, s=38, alpha=0.8, color="#167c71")
            axis.set_title("\n".join(textwrap.wrap(request.title, width=65)), fontsize=11, **font_kwargs)
            axis.set_xlabel(request.x_label or request.x_column, **font_kwargs)
            axis.set_ylabel(request.y_label or request.y_column, **font_kwargs)
            axis.grid(alpha=0.2)

            chart_id = uuid4().hex
            chart_path = CHART_DIR / f"{chart_id}.png"
            temporary_path = CHART_DIR / f"{chart_id}.tmp"
            try:
                CHART_DIR.mkdir(parents=True, exist_ok=True)
                figure.savefig(temporary_path, format="png", dpi=130)
                os.replace(temporary_path, chart_path)
            except (OSError, ValueError, RuntimeError) as exc:
                temporary_path.unlink(missing_ok=True)
                raise ChartToolError("图表保存失败。") from exc
            return json.dumps(
                {
                    "chart_url": f"/api/v1/charts/{chart_path.name}",
                    "chart_type": request.chart_type,
                    "source_round": request.source_round,
                    "source_type": request.source_type,
                    "title": request.title,
                    "point_count": len(rows),
                },
                ensure_ascii=False,
            )

        labels: list[str] = []
        values: list[float] = []
        for row in rows:
            if not isinstance(row, dict) or request.x_column not in row or request.y_column not in row:
                raise ChartToolError("图表的 x_column 或 y_column 不在 SQL 结果中。")
            if row[request.x_column] is None:
                raise ChartToolError("横轴列含空值，无法绘图。")
            label = str(row[request.x_column])
            if len(label) > 40:
                raise ChartToolError("横轴标签过长，请先在 SQL 中缩短标签。")
            try:
                value = float(Decimal(str(row[request.y_column])))
            except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
                raise ChartToolError("纵轴列包含非数值或空值。") from exc
            if not math.isfinite(value):
                raise ChartToolError("纵轴列不能包含无穷大或 NaN。")
            labels.append(label)
            values.append(value)
        if len(set(labels)) != len(labels):
            raise ChartToolError("横轴标签重复，请先在 SQL 中分组汇总。")

        font = self._chinese_font()
        font_kwargs = {"fontproperties": font} if font else {}
        figure = Figure(
            figsize=(min(16, max(8, len(labels) * 0.5)), 4.5),
            layout="constrained",
        )
        FigureCanvasAgg(figure)
        axis = figure.subplots()
        positions = list(range(len(labels)))
        if request.chart_type == "line":
            axis.plot(positions, values, marker="o", color="#167c71", linewidth=2.3)
            axis.set_xlim(-0.6, max(len(labels) - 0.4, 0.6))
        else:
            axis.bar(positions, values, width=0.45, color=["#d66767" if value < 0 else "#167c71" for value in values])
            axis.axhline(0, color="#64748b", linewidth=.8)
            axis.set_xlim(-0.8, max(len(labels) - 0.2, 0.2))
            if request.source_type != "sql":
                axis.bar_label(axis.containers[0], fmt="%.2f", padding=3)
        axis.set_xticks(positions, labels, rotation=35 if len(labels) > 5 else 0)
        if font:
            for tick in axis.get_xticklabels():
                tick.set_fontproperties(font)
        axis.set_title("\n".join(textwrap.wrap(request.title, width=65)), fontsize=11, **font_kwargs)
        axis.set_xlabel(request.x_label or ("维度" if request.source_type != "sql" else request.x_column), **font_kwargs)
        axis.set_ylabel(request.y_label or ("变化额" if request.source_type != "sql" else request.y_column), **font_kwargs)
        axis.margins(y=.18)
        axis.grid(axis="y", alpha=0.2)

        chart_id = uuid4().hex
        chart_path = CHART_DIR / f"{chart_id}.png"
        temporary_path = CHART_DIR / f"{chart_id}.tmp"
        try:
            CHART_DIR.mkdir(parents=True, exist_ok=True)
            figure.savefig(temporary_path, format="png", dpi=130)
            os.replace(temporary_path, chart_path)
        except (OSError, ValueError, RuntimeError) as exc:
            temporary_path.unlink(missing_ok=True)
            raise ChartToolError("图表保存失败。") from exc
        return json.dumps(
            {
                "chart_url": f"/api/v1/charts/{chart_path.name}",
                "chart_type": request.chart_type,
                "source_round": request.source_round,
                "source_type": request.source_type,
                "title": request.title,
                "point_count": len(rows),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _chinese_font() -> FontProperties | None:
        for path in (
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ):
            if Path(path).is_file():
                return FontProperties(fname=path)
        return None
