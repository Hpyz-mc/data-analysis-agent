"""Conservative request coverage, not a general natural-language intent parser."""
import re
from dataclasses import dataclass, field

from sqlglot import exp, parse_one

from app.agent.scoped_analysis import ScopeError, identifier, literal, schema_columns
from app.agent.intent import requested_time_grain


NUMERIC = re.compile(r"numeric|decimal|integer|bigint|smallint|real|double")
ALIASES = {
    "销售额": {"sales_amount", "revenue", "销售额", "销售金额"},
    "订单量": {"order_count", "orders", "订单量", "订单数"},
    "利润": {"profit", "利润"},
}


@dataclass
class AnswerContract:
    goals: list[str] = field(default_factory=list)
    order_column: str | None = None
    stockout_column: str | None = None
    status_column: str | None = None
    diagnostic: bool = False


def _named(question, name):
    return bool(re.search(r"(?<![a-zA-Z0-9_])" + re.escape(name) + r"(?![a-zA-Z0-9_])", question, re.I))


def build_contract(question, context, schema):
    columns = schema_columns(schema).get(context.source_table, {})
    contract = AnswerContract()
    if context.kind == "period_change" and not context.dimension and re.search(
        r"归因|拆解|下钻|变化来源|贡献图|为什么|为何|原因|诊断", question,
    ):
        raise ScopeError("本轮要求维度归因，请明确按渠道、品类或其他实际字段拆解；不能只查询总额代替归因。")
    metric = parse_one(context.metric_expression, read="postgres")
    primary = {col.name for col in metric.find_all(exp.Column)}
    explicit = {name for name in columns if NUMERIC.search(columns[name]) and _named(question, name)}
    requested = set()
    for label, aliases in ALIASES.items():
        mentioned = (label in question or (label == "订单量" and "订单数" in question)
                     or any(_named(question, name) for name in aliases))
        if mentioned:
            candidates = aliases & set(columns)
            if len(candidates) == 1:
                requested.update(candidates)
            elif len(candidates) > 1 and not candidates.intersection(explicit):
                raise ScopeError(f"{label}对应多个字段，请明确本轮使用哪个字段。")
            elif not candidates and label not in context.metric_label:
                raise ScopeError(f"当前表无法唯一识别{label}字段，请明确该指标的计算口径。")
    requested.update(explicit)
    contract.diagnostic = bool(re.search(r"为什么|为何|原因|解释.*(?:下降|变化|增长)|查证|验证.*(?:缺货|断货)", question))
    # 相关性分析的第二字段已由 validate_context 校验，不触发订单量/客单价联合核验。
    if context.kind == "correlation":
        contract.goals.append("回答主指标，并保持本轮数据表、时间和筛选")
        contract.goals.append("输出相关系数、样本对数与缺失情况；相关只描述线性关联，不证明因果；样本少或缺失多时说明局限")
        return contract
    wants_aov = bool(re.search(r"客单价|平均每单|每单金额", question))
    wants_orders = bool((requested & ALIASES["订单量"]) - primary) or wants_aov
    wants_stock = bool(re.search(r"缺货|断货|供货|供应链|stockout|supply_status", question, re.I))
    wants_price = bool(re.search(r"售价|商品.*(?:单价|价格)|unit_price", question)) or (bool(re.search(r"单价|价格", question)) and not wants_aov)
    # Aggregate spend per order is not product price; do not silently substitute it.
    if wants_price and contract.diagnostic:
        raise ScopeError("商品售价与客单价不同。当前可核验订单量和销售额/订单量；若要判断商品价格效应，请明确商品、销量和售价字段及口径。")
    revenue = primary <= ALIASES["销售额"] and len(primary) == 1
    order_candidates = ALIASES["订单量"] & set(columns)
    if len(order_candidates) > 1 and len(order_candidates & explicit) == 1:
        order_candidates &= explicit
    extra = requested - primary - ALIASES["订单量"] - {"stockout_days"}
    if extra:
        raise ScopeError(f"本轮还要求 {', '.join(sorted(extra))}，当前流程不会只完成主指标。请先选择一个主指标；销售额可同时核验订单量和客单价。")
    if wants_orders and not revenue:
        raise ScopeError("销售额与订单量的联合核验需要以可加总的销售额字段为主指标，请明确销售额字段。")
    if (wants_orders or contract.diagnostic) and revenue and len(order_candidates) == 1:
        order = next(iter(order_candidates))
        known_grain = context.source_table == "public.demo_ecommerce_orders" and order == "order_count"
        confirmed_sum = bool(re.search(r"(?:订单量|订单数|order_count|orders).{0,16}(?:求和|可加总|互斥)", question, re.I)
                             or (wants_orders and re.search(r"用户补充：.*确认.*互斥.*(?:求和|加总)", question)))
        if NUMERIC.search(columns[order]) and (known_grain or confirmed_sum or context.confirmed_order_column == order):
            contract.order_column = order
            context.confirmed_order_column = order
        elif wants_orders:
            raise ScopeError("请确认订单数字段可按互斥记录求和（同一订单不能重复计算），再进行销售额、订单量与客单价联合核验。")
    elif wants_orders:
        raise ScopeError("当前表无法唯一识别订单数字段，不能将表行数当作订单量或计算客单价。")
    # Auxiliary records need known aggregation semantics, not guesses from a column name.
    if context.source_table == "public.demo_ecommerce_orders" and (contract.diagnostic or wants_stock):
        if "stockout_days" in columns and NUMERIC.search(columns["stockout_days"]):
            contract.stockout_column = "stockout_days"
        if "supply_status" in columns:
            contract.status_column = "supply_status"
    if wants_stock and not (contract.stockout_column or contract.status_column):
        raise ScopeError("当前表没有已确认口径的供货/缺货辅助字段，不能证明缺货原因；请明确可核验字段或先做变化贡献分析。")
    if (contract.order_column or contract.stockout_column or contract.status_column) and context.kind not in {"trend", "period_change", "segment_comparison"}:
        if (context.kind == "lookup" and contract.order_column and not contract.diagnostic
                and not contract.stockout_column and not contract.status_column):
            contract.goals.append("同一范围一次查询销售额、订单量与客单价（总销售额/总订单量）；单期不做变化分解，订单为零时客单价未定义")
        else:
            raise ScopeError("联合变化核验需要两个比较对象或有序时期，请明确比较范围。")
    contract.goals.append("回答主指标，并保持本轮数据表、时间和筛选")
    if context.kind == "ranking":
        contract.goals.append(
            (f"按指标降序列出前 {context.limit_n} 项" if context.limit_n else "按指标降序列出全部排名项")
            + "，并明确说明其余项未展示")
    if context.kind == "distribution":
        contract.goals.append("列出各项金额与占比，核对占比合计为 100%；占比只描述构成，不是变化归因")
    if context.kind == "decile":
        contract.goals.append("列出各分层金额、记录数与占比，核对各层合计等于独立总额；分层只描述分布，不证明因果")
    if context.kind == "outlier":
        contract.goals.append("展示 1%/99% 分位边界、原始合计、缩尾/截尾后的合计与受影响记录数；处理仅影响本轮计算视图，不改原数据")
    if context.kind == "summary":
        contract.goals.append("同一范围报告合计、逐行平均、最小值、最大值与非空行数；平均是逐行平均，不等于合计除以行数")
    if requested_time_grain(question) or context.kind == "trend":
        contract.goals.append("展示要求的时间粒度与每对返回时期的变化")
    if context.dimension and context.kind in {"trend", "period_change", "segment_comparison"}:
        contract.goals.append("核对独立总额与维度明细，解释主要变化来源")
    if contract.order_column and context.kind in {"trend", "period_change", "segment_comparison"}:
        contract.goals.append("实际查询订单量、计算总销售额/总订单量，并核对订单量与客单价的机械变化分解")
    if contract.stockout_column or contract.status_column:
        contract.goals.append("实际查询同范围供货记录；仅解释关联线索，不宣布因果根因")
    elif contract.diagnostic:
        contract.goals.append("说明现有证据只能定位贡献，不能证明原因；指出具体缺少的辅助口径")
    if re.search(r"图|画|绘制", question):
        contract.goals.append("在对应正文位置展示本轮实际图表")
    return contract


def append_driver_pipeline(pipeline, context, contract, first_round):
    if not (contract.order_column or contract.stockout_column or contract.status_column):
        return pipeline
    if context.kind == "lookup" and contract.order_column and not contract.diagnostic:
        base = pipeline[0]["sql"]
        orders = identifier(contract.order_column)
        replacement = (f"SUM({context.metric_expression}) AS amount, SUM({orders}) AS orders, "
                       f"SUM({context.metric_expression}) / NULLIF(SUM({orders}), 0) AS aov")
        if f"SUM({context.metric_expression}) AS amount" in base:
            pipeline[0] = {**pipeline[0], "sql": base.replace(
                f"SUM({context.metric_expression}) AS amount", replacement),
                "reasoning": "同一范围一次查询销售额、订单量与客单价（总销售额/总订单量），单期不做变化分解"}
        return pipeline
    # Reuse the actual total query AST to inherit its exact CASE/range/group/filter.
    total = parse_one(pipeline[0]["sql"], read="postgres")
    group = total.expressions[0].copy()
    dimension = identifier(context.dimension) if context.dimension else literal("总体")
    fields = [group.sql(dialect="postgres"), f"{dimension} AS dimension",
              f"SUM({context.metric_expression}) AS amount",
              f"COUNT(*) FILTER (WHERE ({context.metric_expression}) IS NULL) AS missing_amount"]
    if contract.order_column:
        orders = identifier(contract.order_column)
        fields += [f"SUM({orders}) AS orders", f"COUNT(*) FILTER (WHERE {orders} IS NULL OR {orders} < 0) AS invalid_orders"]
    if contract.stockout_column:
        stock = identifier(contract.stockout_column)
        fields += [f"MIN({stock}) AS stockout_min", f"MAX({stock}) AS stockout_max",
                   f"COUNT(DISTINCT {stock}) AS stockout_distinct"]
    if contract.status_column:
        status = identifier(contract.status_column)
        fields += [f"ARRAY_AGG(DISTINCT {status}::text) FILTER (WHERE {status} IS NOT NULL) AS supply_statuses"]
    query = ("SELECT " + ", ".join(fields) + " " + total.args["from_"].sql(dialect="postgres")
             + " " + total.args["where"].sql(dialect="postgres") + " GROUP BY 1, 2 ORDER BY 1, 2")
    source = first_round + len(pipeline)
    direction = ([context.baseline.label(), context.current.label()] if context.kind == "period_change"
                 else [context.baseline_group, context.current_group] if context.kind == "segment_comparison" else None)
    return [*pipeline,
            {"action": "sql", "sql": query, "reasoning": "按完全相同范围查证订单与供货辅助记录，不将缺货记录求和为自然日"},
            {"action": "python", "python": {"operation": "driver_check", "source_round": source,
                "total_source_round": first_round, "period_column": "period", "dimension_column": "dimension",
                "value_column": "amount", "order_column": "orders" if contract.order_column else None,
                "comparison_periods": direction},
             "reasoning": "辅助销售额与独立总额核对，再计算订单量/客单价变化；关联不作因果证明"}]


def append_requested_charts(pipeline, context, question, first_round):
    kinds = set()
    if re.search(r"折线图|趋势图", question):
        kinds.add("line")
    if re.search(r"贡献图", question):
        kinds.add("contribution")
    if re.search(r"柱状图", question):
        kinds.add("bar")
    if re.search(r"散点图", question) and context.kind != "correlation":
        raise ScopeError("散点图仅用于相关性分析，请先明确两个数值字段的相关性问题。")
    if re.search(r"饼图|热力图|箱线图|瀑布图", question):
        raise ScopeError("当前图表支持折线图、柱状图和相关性散点图，请选择其中一种；不会用其他图形冒充你要求的图表。")
    present = {item["chart"]["chart_type"] for item in pipeline if item["action"] == "chart"}
    grain_label = {"day": "日期", "week": "周", "month": "月份", "quarter": "季度", "year": "年份"}
    for kind in sorted(kinds - present):
        if context.kind in {"outlier", "summary"}:
            raise ScopeError("该分析输出单行汇总结果，不适合绘制图表；结果已包含处理前后对比。")
        if context.kind == "correlation":
            raise ScopeError("相关性分析使用分箱均值散点图（已自动生成），其他图表类型不适用。")
        if context.kind == "lookup" or (kind == "line" and context.kind in {"segment_comparison", "ranking", "distribution", "decile"}):
            raise ScopeError("该图需要有序时间点或明确的变化比较，请先明确展示范围。")
        if kind == "contribution":
            if any(item["action"] == "chart" and item["chart"].get("source_type")
                   in {"attribution", "sequential_attribution"} for item in pipeline):
                continue  # 归因贡献图已在 pipeline 中
            raise ScopeError("贡献图需要趋势与拆解维度，请明确按渠道还是品类归因。")
        if kind == "bar" and context.kind == "trend":
            if not context.dimension:
                item = {"chart_type": "bar", "source_round": first_round,
                        "x_column": "period", "y_column": "amount",
                        "title": f"{context.metric_label} · 按月柱状"[:240],
                        "x_label": grain_label.get(context.time_grain or "month", "时期"),
                        "y_label": context.metric_label}
            else:
                item = {"chart_type": "bar", "source_type": "sequential_attribution",
                        "source_round": first_round + 4, "x_column": "dimension", "y_column": "change",
                        "title": f"相邻时期变化贡献 · {context.metric_label}"[:240],
                        "x_label": f"相邻时期 · {field_label(context.dimension)}"[:80],
                        "y_label": f"{context.metric_label}变化额"[:80]}
        else:
            item = {"chart_type": kind, "source_round": first_round, "x_column": "period", "y_column": "amount",
                    "title": context.metric_label + " · 本轮比较", "x_label": "时期/维度", "y_label": context.metric_label}
        pipeline.append({"action": "chart", "chart": item, "reasoning": "补齐用户明确要求的图表类型，使用本轮已核验结果"})
    return pipeline

