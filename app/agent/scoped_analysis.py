"""Generate matching, bounded queries from a validated analysis scope."""

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.agent.context import AnalysisContext, DateRange, FollowupSuggestion
from app.agent.semantics import semantic_sql_error
from app.agent.intent import requested_time_grain


class ScopeError(ValueError):
    pass


def schema_columns(schema: str) -> dict[str, dict[str, str]]:
    tables = {}
    for line in schema.splitlines():
        if ": " not in line:
            continue
        name, fields = line.split(": ", 1)
        tables[name] = dict(re.findall(r"([^,\s]+)\s+\(([^)]+)\)", fields))
    return tables


def identifier(name: str) -> str:
    return exp.Identifier(this=name, quoted=True).sql(dialect="postgres")


def literal(value: str | int | float | bool) -> str:
    return exp.convert(value).sql(dialect="postgres")


def validate_context(context: AnalysisContext, schema: str, question: str = "") -> AnalysisContext:
    context = context.model_copy(deep=True)
    if "." not in context.source_table:
        context.source_table = "public." + context.source_table
    columns = schema_columns(schema).get(context.source_table)
    if not columns or not context.source_table.startswith("public."):
        raise ScopeError("当前分析表不在可访问的表结构中，请明确要使用的数据表。")
    try:
        metric = sqlglot.parse_one(context.metric_expression, read="postgres")
    except SqlglotError as exc:
        raise ScopeError("指标表达式无法识别，请明确用于求和的数值字段。") from exc
    allowed = (exp.Column, exp.Identifier, exp.Literal, exp.Add, exp.Sub, exp.Mul,
               exp.Div, exp.Paren, exp.Neg)
    if metric is None or any(not isinstance(node, allowed) for node in metric.walk()):
        raise ScopeError("当前下钻支持数值字段或加减乘表达式的 SUM，请明确可加总的指标口径。")
    if metric.find(exp.Div) or any(not value.is_number for value in metric.find_all(exp.Literal)):
        raise ScopeError("比率和均值不能逐行求和，当前归因需要可加总的数值指标，请明确其他指标口径。")
    metric_columns = list(metric.find_all(exp.Column))
    if not metric_columns:
        raise ScopeError("指标必须引用数据表中的数值字段。")
    for column in metric_columns:
        if column.table or column.name not in columns:
            raise ScopeError("指标引用了当前表中不存在的字段。")
        if not re.search(r"numeric|decimal|integer|bigint|smallint|real|double", columns[column.name]):
            raise ScopeError(f"字段 {column.name} 不是数值类型，无法按当前口径归因。")
        column.set("this", exp.Identifier(this=column.name, quoted=True))
    context.metric_expression = metric.sql(dialect="postgres")
    for name in [context.time_column, context.group_column, context.dimension, context.confirmed_order_column,
                 *(item.column for item in context.filters)]:
        if name and name not in columns:
            raise ScopeError(f"当前表没有字段 {name}，请从可用字段中选择下钻维度。")
    if context.kind in {"period_change", "trend"}:
        if not context.time_column or not context.current:
            raise ScopeError("请明确日期字段和当前分析日期范围。")
    if context.current and not context.time_column:
        raise ScopeError("限定时间范围时需要指定日期字段。")
    if context.time_grain and not context.time_column:
        raise ScopeError("按时间分组时需要明确日期字段。")
    if context.kind == "period_change":
        if not context.baseline:
            raise ScopeError("请明确用于比较的基期日期范围。")
        if max(context.baseline.start, context.current.start) < min(context.baseline.end, context.current.end):
            raise ScopeError("两个比较时期重叠，请选择互不重叠的日期范围。")
    if context.kind == "segment_comparison":
        if not all((context.group_column, context.baseline_group, context.current_group)):
            raise ScopeError("请明确要比较的两个群体及其对应字段。")
        if context.baseline_group == context.current_group:
            raise ScopeError("要比较的两个群体不能相同。")
        if context.dimension == context.group_column:
            raise ScopeError("拆解维度不能与比较群体字段相同，请选择品类等其他维度。")
        if context.current and not context.time_column:
            raise ScopeError("限定时间范围时需要指定日期字段。")
        if any(item.column == context.group_column for item in context.filters):
            raise ScopeError("筛选条件限制了比较群体，请先取消该群体字段的筛选。")
    if (context.kind == "segment_comparison" and not context.dimension
            and re.search(r"为什么|为何|原因|归因|拆解|下钻|变化来源|贡献", question)):
        candidates = available_dimensions(context, schema)
        hint = "、".join(field_label(name) for name in candidates[:6]) or "暂无可用的分类字段"
        raise ScopeError(f"请选择用于拆解差异的维度：{hint}。")
    table = ".".join(identifier(part) for part in context.source_table.split("."))
    error = semantic_sql_error(f"SELECT SUM({context.metric_expression}) FROM {table}")
    if error:
        raise ScopeError(error)
    return context


def field_label(name: str) -> str:
    return {"channel": "渠道", "category": "品类", "product": "商品",
            "product_name": "商品", "region": "地区", "city": "城市",
            "order_date": "日期", "sales_amount": "销售额"}.get(name, name)


def available_dimensions(context: AnalysisContext, schema: str) -> list[str]:
    columns = schema_columns(schema).get(context.source_table, {})
    excluded = {context.time_column, context.group_column, context.dimension,
                *(item.column for item in context.filters), *context.drill_path}
    return [name for name, dtype in columns.items()
            if name not in excluded and re.search(r"char|text|boolean", dtype)
            and not re.search(r"(?:^id$|_id$|status|状态)", name, re.I)]


def missing_requested_dimension(question: str, context: AnalysisContext, schema: str) -> str | None:
    """Do not ask the model to invent a common drill-down field absent from the table."""
    requested = re.search(r"(?:按|以)(商品|产品|城市|地区|渠道|品类)(?:维度)?(?:下钻|拆解|归因|看|分析|细分)", question)
    if not requested:
        return None
    aliases = {
        "商品": {"product", "product_name", "sku", "商品", "商品名称"},
        "产品": {"product", "product_name", "sku", "产品", "产品名称"},
        "城市": {"city", "城市"}, "地区": {"region", "地区", "区域"},
        "渠道": {"channel", "渠道"}, "品类": {"category", "品类", "商品类别", "商品品类"},
    }
    columns = schema_columns(schema).get(context.source_table, {})
    label = requested.group(1)
    if aliases[label].intersection(columns):
        return None
    available = [field_label(name) for name, dtype in columns.items()
                 if re.search(r"char|text", dtype) and not re.search(r"status|状态|_id$", name)]
    return f"当前表没有{label}字段，无法按{label}继续下钻。可用分类字段：{'、'.join(available) or '暂无'}。"


def scope_caption(context: AnalysisContext) -> str:
    comparison = (
        f"{context.baseline.label()} → {context.current.label()}" if context.kind == "period_change"
        else f"{context.baseline_group} → {context.current_group}" if context.kind == "segment_comparison"
        else context.current.label() if context.current else "全部时间"
    )
    filters = "；".join(f"{field_label(item.column)}={','.join(map(str, item.values))}"
                        for item in context.filters)
    if context.kind == "segment_comparison" and context.current:
        comparison += " · " + context.current.label()
    return f"{context.metric_label} · {comparison}" + (f" · {filters}" if filters else "")


def requested_breakdown_labels(question: str) -> list[str]:
    return re.findall(
        r"(?:按照|按|以)\s*(?:销售|商品|产品)?\s*([\w]+?)\s*(?:维度)?\s*(?:拆解|归因|下钻|细分)",
        question,
    )


def align_breakdown_intent(context: AnalysisContext, question: str, schema: str) -> AnalysisContext:
    """Keep an explicitly requested breakdown even if the model omitted it."""
    matches = requested_breakdown_labels(question)
    if not matches:
        return context
    aliases = {
        "渠道": {"channel", "渠道", "销售渠道"},
        "品类": {"category", "品类", "商品类别", "商品品类"},
        "类别": {"category", "类别", "商品类别"},
        "地区": {"region", "地区", "区域"}, "城市": {"city", "城市"},
        "商品": {"product", "product_name", "sku", "商品", "商品名称"},
        "产品": {"product", "product_name", "sku", "产品", "产品名称"},
    }
    columns = schema_columns(schema).get(context.source_table, {})
    dimensions = set()
    for label in matches:
        candidates = aliases.get(label, {label}) & set(columns)
        if len(candidates) != 1:
            raise ScopeError(f"无法唯一确定{label}拆解字段，请明确使用当前表中的哪个字段。")
        dimensions.update(candidates)
    if len(dimensions) != 1:
        raise ScopeError("本轮支持一个拆解维度，请选择先按哪个维度分析，再继续下钻。")
    context = context.model_copy(deep=True)
    context.dimension = dimensions.pop()
    if requested_time_grain(question) and re.search(r"变化|波动|走势|趋势", question):
        if context.kind == "segment_comparison":
            raise ScopeError("本轮同时要求群体比较与时间变化，请先选择一种比较方式。")
        if context.baseline and context.current:
            context.current = DateRange(start=min(context.baseline.start, context.current.start),
                                        end=max(context.baseline.end, context.current.end))
        context.kind = "trend"
        context.baseline = None
    return context


def build_pipeline(context: AnalysisContext, first_round: int, chart_type: str | None = None,
                   *, contribution_chart: bool = False) -> list[dict]:
    """Total and detail SQL share a single scope but are executed independently."""
    table = ".".join(identifier(part) for part in context.source_table.split("."))
    clauses = [f"{identifier(item.column)} IN ({', '.join(literal(v) for v in item.values)})"
               for item in context.filters]
    amount = f"SUM({context.metric_expression})"
    def date_clause(period):
        column = identifier(context.time_column)
        return f"({column} >= DATE {literal(period.start.isoformat())} AND {column} < DATE {literal(period.end.isoformat())})"
    if context.kind == "lookup":
        if context.current:
            clauses.append(date_clause(context.current))
        where = " AND ".join(clauses) or "TRUE"
        if context.time_grain:
            group = time_bucket(context.time_column, context.time_grain)
            extra = f", {identifier(context.dimension)} AS dimension" if context.dimension else ""
            groups = "1, 2" if context.dimension else "1"
            query = f"SELECT {group} AS period{extra}, {amount} AS amount FROM {table} WHERE {where} GROUP BY {groups} ORDER BY {groups}"
        elif context.dimension:
            group = identifier(context.dimension)
            query = f"SELECT {group} AS dimension, {amount} AS amount FROM {table} WHERE {where} GROUP BY 1 ORDER BY amount DESC"
        else:
            query = f"SELECT {amount} AS amount FROM {table} WHERE {where}"
        pipeline = [{"action": "sql", "sql": query, "reasoning": "按继承的指标、时期和筛选条件重新查数"}]
        if chart_type and (context.dimension or context.time_grain):
            if context.dimension and context.time_grain:
                raise ScopeError("当前图表不支持同时按时间和类别的多系列展示，请选择一个绘图维度。")
            pipeline.append({"action": "chart", "chart": {"chart_type": chart_type,
                "source_round": first_round, "x_column": "period" if context.time_grain else "dimension", "y_column": "amount",
                "x_label": "时期" if context.time_grain else field_label(context.dimension), "y_label": context.metric_label,
                "title": scope_caption(context)[:240]}, "reasoning": "使用本轮查询绘制分类图"})
        return pipeline
    if context.kind == "period_change":
        baseline, current = context.baseline.label(), context.current.label()
        base_where, current_where = date_clause(context.baseline), date_clause(context.current)
        clauses.append(f"({base_where} OR {current_where})")
        group = f"CASE WHEN {base_where} THEN {literal(baseline)} ELSE {literal(current)} END"
    elif context.kind == "segment_comparison":
        baseline, current = context.baseline_group, context.current_group
        group = identifier(context.group_column)
        clauses.append(f"{group} IN ({literal(baseline)}, {literal(current)})")
        if context.current:
            clauses.append(date_clause(context.current))
    else:
        clauses.append(date_clause(context.current))
        group = time_bucket(context.time_column, context.time_grain or "month")
    where = " AND ".join(clauses) or "TRUE"
    total_sql = f"SELECT {group} AS period, {amount} AS amount FROM {table} WHERE {where} GROUP BY 1 ORDER BY 1"
    title = scope_caption(context)
    if context.kind == "trend":
        grain_label = {"day": "日期", "week": "周", "month": "月份", "quarter": "季度", "year": "年份"}[context.time_grain or "month"]
        pipeline = [
            {"action": "sql", "sql": total_sql, "reasoning": "按当前范围与时间粒度重新查询指标"},
            {"action": "python", "python": {"operation": "percent_change", "source_round": first_round,
                "value_column": "amount", "label_column": "period"}, "reasoning": "计算相邻返回时期的变化"},
            {"action": "chart", "chart": {"chart_type": "line", "source_round": first_round,
                "x_column": "period", "y_column": "amount", "x_label": grain_label, "y_label": context.metric_label,
                "title": title[:240]}, "reasoning": "用本轮查询结果绘制趋势图"},
        ]
        if context.dimension:
            dimension = identifier(context.dimension)
            pipeline.extend([
                {"action": "sql", "sql": f"SELECT {group} AS period, {dimension} AS dimension, {amount} AS amount FROM {table} WHERE {where} GROUP BY 1, 2 ORDER BY 1, 2",
                 "reasoning": "按相同时间范围、指标与筛选独立查询各期维度明细"},
                {"action": "python", "python": {"operation": "sequential_attribution",
                    "source_round": first_round + 3, "total_source_round": first_round,
                    "period_column": "period", "dimension_column": "dimension", "value_column": "amount"},
                 "reasoning": "逐期核对明细与独立总额，再计算每个相邻时期的维度变化与贡献"},
            ])
            if contribution_chart:
                pipeline.append({"action": "chart", "chart": {"chart_type": "bar",
                    "source_type": "sequential_attribution", "source_round": first_round + 4,
                    "x_column": "dimension", "y_column": "change",
                    "x_label": f"相邻时期 · {field_label(context.dimension)}"[:80],
                    "y_label": f"{context.metric_label}变化额"[:80], "title": f"相邻时期变化贡献 · {title}"[:240]},
                    "reasoning": "使用逐期核验后的变化额绘制贡献图"})
        return pipeline
    if context.kind == "period_change" and not context.dimension:
        # Percent-change follows row order: preserve baseline -> current even
        # when the user's baseline happens to be the later calendar period.
        comparison_sql = total_sql.rsplit(" ORDER BY ", 1)[0] + (
            f" ORDER BY CASE WHEN {group} = {literal(baseline)} THEN 0 ELSE 1 END"
        )
        pipeline = [
            {"action": "sql", "sql": comparison_sql, "reasoning": "查询用户指定的两个时期总额，不查询区间中其他月份"},
            {"action": "python", "python": {"operation": "percent_change", "source_round": first_round,
                "value_column": "amount", "label_column": "period"},
             "reasoning": "计算两个指定时期之间的差额与变化率，不冒充相邻月份环比"},
        ]
        if chart_type:
            pipeline.append({"action": "chart", "chart": {"chart_type": chart_type,
                "source_round": first_round, "x_column": "period", "y_column": "amount",
                "x_label": "比较时期", "y_label": context.metric_label, "title": title[:240]},
                "reasoning": "使用两个时期的总额绘制比较图"})
        return pipeline
    if context.kind == "segment_comparison" and not context.dimension:
        return [{"action": "sql", "sql": total_sql, "reasoning": "查询两个对比群体的独立总额，不做归因拆解"}]
    dimension = identifier(context.dimension)
    detail_sql = f"SELECT {group} AS period, {dimension} AS dimension, {amount} AS amount FROM {table} WHERE {where} GROUP BY 1, 2 ORDER BY 1, 2"
    return [
        {"action": "sql", "sql": total_sql, "reasoning": "独立查询当前下钻范围内的两个比较对象总额"},
        {"action": "sql", "sql": detail_sql, "reasoning": "使用相同筛选与指标口径查询维度明细"},
        {"action": "python", "python": {"operation": "attribution", "source_round": first_round + 1,
            "total_source_round": first_round, "period_column": "period", "dimension_column": "dimension",
            "value_column": "amount", "baseline_period": baseline, "current_period": current},
            "reasoning": "核对两组明细与独立总额并计算维度贡献"},
        {"action": "chart", "chart": {"chart_type": "bar", "source_type": "attribution",
            "source_round": first_round + 2, "x_column": "dimension", "y_column": "change",
            "x_label": field_label(context.dimension), "y_label": f"{context.metric_label}变化额"[:80],
            "title": f"{field_label(context.dimension)}变化贡献 · {title}"[:240]},
            "reasoning": "直接使用核验后的归因变化额绘制正负贡献图"},
    ]


def time_bucket(column: str, grain: str) -> str:
    formats = {"day": "YYYY-MM-DD", "week": "IYYY-IW", "month": "YYYY-MM",
               "quarter": 'YYYY-"Q"Q', "year": "YYYY"}
    return f"to_char(date_trunc({literal(grain)}, {identifier(column)}), {literal(formats[grain])})"


def align_time_intent(context: AnalysisContext, question: str, schema: str) -> AnalysisContext:
    """Explicit user grain wins over a model's accidental range-total plan."""
    grain = requested_time_grain(question)
    if grain is None:
        if context.kind == "lookup" and re.search(r"合计|总计|总额", question):
            context = context.model_copy(deep=True)
            context.time_grain = None
        return context
    context = context.model_copy(deep=True)
    if context.kind not in {"lookup", "trend"}:
        raise ScopeError("该问题同时要求时间分组和两组归因，请先明确本轮做时间走势还是比较归因。")
    if not context.time_column:
        columns = schema_columns(schema).get(context.source_table, {})
        candidates = [name for name, dtype in columns.items() if re.search(r"date|timestamp", dtype)]
        if len(candidates) != 1:
            raise ScopeError("按时间分组需要明确使用哪个日期字段。")
        context.time_column = candidates[0]
    if context.dimension == context.time_column:
        context.dimension = None
    context.time_grain = grain
    return context


def followup_suggestions(context: AnalysisContext, schema: str, evidence: dict | None) -> list[FollowupSuggestion]:
    suggestions = []
    candidates = available_dimensions(context, schema)
    if context.kind in {"period_change", "segment_comparison"} and evidence and candidates:
        details = evidence.get("details") or []
        # Prefer a declining contributor when the overall result declines.
        negative = [row for row in details if str(row.get("change", "")).startswith("-")]
        selected = (negative or details)[0] if details else None
        if selected:
            value = selected["dimension"]
            for dimension in candidates[:2]:
                label = f"{value}：按{field_label(dimension)}下钻"
                question = (f"继续分析，限定 {context.dimension} 为“{value}”，"
                            f"按 {dimension}（{field_label(dimension)}）下钻归因，"
                            "沿用当前数据表、指标和两个比较对象，并展示贡献图。")
                suggestions.append(FollowupSuggestion(label=label, question=question))
    if context.kind == "period_change":
        suggestions.append(FollowupSuggestion(
            label="查看当前范围的月度趋势",
            question="沿用当前表、指标和筛选条件，查看覆盖两个比较时期的月度走势，并画折线图。",
        ))
    if context.filters and context.kind in {"period_change", "segment_comparison"}:
        suggestions.append(FollowupSuggestion(
            label="回到总体分析",
            question="取消当前全部筛选，回到总体，沿用当前表、指标和比较时期进行归因，并展示贡献图。",
        ))
    return suggestions
