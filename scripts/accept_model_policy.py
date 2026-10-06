"""Seven real acceptance turns; retain evidence without touching old benchmarks."""
import json
from pathlib import Path
from time import perf_counter
from urllib.request import Request, urlopen

from app.clients.local_api import analyze, analyze_stream, get_chart, get_history


def amounts(result):
    return [float(row["amount"]) for step in result["steps"] if step["action"] == "sql"
            for row in json.loads(step["observation"]).get("rows", []) if "amount" in row]


def main():
    Path("outputs").mkdir(exist_ok=True)
    records, session_id = [], None
    # (question, expected SQL amounts, required python operation)
    cases = [
        ("请查询 demo_ecommerce_orders 中2026年1至3月每月销售额，按 sales_amount 求和。", [666000, 652300, 702600], None),
        ("那这三个月销售额合计多少？", [2020900], None),
        ("请分析这张表2月销售额比1月下降的情况，按渠道归因并画贡献图。", None, "attribution"),
        ("沿用当前表、指标和筛选，查看两个比较月份的月度走势并画折线图。", [666000, 652300], None),
        ("查询 demo_ecommerce_orders 中 2026 年 1 月销售额前 3 的品类。", [352000, 203000, 111000], None),
        ("查询 demo_ecommerce_orders 中 2026 年 1 月各渠道销售额占比。", [306000, 200000, 160000], "share"),
        ("查询 demo_ecommerce_orders 中 2026 年 1 月按订单金额十分位分层，看每层销售额。", None, "decile"),
    ]
    for index, (question, expected, operation) in enumerate(cases, 1):
        started = perf_counter()
        stream_metrics = None
        if index == len(cases):
            count, first, text, result = 0, None, [], None
            for event, value in analyze_stream(question, session_id):
                if event == "delta":
                    first = first if first is not None else round((perf_counter() - started) * 1000)
                    count += 1
                    text.append(value["text"])
                elif event == "result":
                    result = value
            assert result is not None and count > 1, "未收到完整流式结果"
            assert result["answer"] == "".join(text), "流式文字与入库结果不一致"
            stream_metrics = {"delta_events": count, "first_delta_ms": first}
        else:
            result = analyze(question, session_id)
        session_id = session_id or result["session_id"]
        assert result["session_id"] == session_id and result["finished"], "会话未正常完成"
        if expected is not None:
            assert amounts(result) == expected, f"数据不符：{amounts(result)} != {expected}"
        if index == 3:
            assert any(step["action"] == "python" and 'attribution' in step["tool_input"]
                       for step in result["steps"]), "归因算子未执行"
            assert len(result["charts"]) > 0, "归因图缺失"
        if operation == "share":
            assert any(step["action"] == "python" and 'share' in step["tool_input"]
                       for step in result["steps"]), "占比算子未执行"
        if operation == "decile":
            checked = [json.loads(step["observation"]).get("result", {}).get("reconciled") is True
                       for step in result["steps"] if step["action"] == "python" and "decile" in step["tool_input"]]
            assert any(checked), "十分位分层未核对各层合计与独立总额"
        for chart in result["charts"]:
            assert get_chart(chart).startswith(b"\x89PNG"), "图表不可读"
        usage = result["usage"]
        assert usage["total_tokens"] > 0 and usage["complete"], "用量统计不完整"
        records.append({"question": question, "response": result, "stream": stream_metrics,
                        "wall_ms": round((perf_counter() - started) * 1000)})
        print(json.dumps({"turn": index, "finished": result["finished"], "usage": usage,
                          "stream": stream_metrics}, ensure_ascii=False), flush=True)
        Path("outputs/model_policy_acceptance.json").write_text(
            json.dumps({"session_id": session_id, "turns": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    history = get_history(session_id)
    assert len(history["turns"]) == len(cases) and all(turn["status"] == "completed" for turn in history["turns"])
    print(f"PASS: {len(cases)} turns, persisted history, charts and streaming; session=" + session_id, flush=True)


if __name__ == "__main__":
    main()
