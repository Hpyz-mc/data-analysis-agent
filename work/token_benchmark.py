"""Sequential, real API benchmark. Writes generated results, never credentials."""
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

QUESTIONS = [
    "请查询 demo_ecommerce_orders 中2026年1至3月每月销售额，按 sales_amount 求和。",
    "请分析这张表2月销售额比1月下降的情况，按渠道归因并画贡献图。",
    "线下继续按品类下钻，并画贡献图。",
    "沿用当前表、指标和筛选，查看两个比较月份的月度走势并画折线图。",
    "那改为3月比2月，仍看线下，按品类归因并画图。",
    "那线下这两个月销售额合计多少？",
    "继续按商品下钻。",
    "没有商品字段，那继续按品类分析3月比2月，仍只看线下。",
    "回到总体，分析3月比2月销售额，按渠道归因。",
    "线上商城继续按品类下钻。",
    "那这两个比较月份线上商城销售额合计多少？",
    "沿用当前筛选，查看2月和3月销售额走势并画折线图。",
    "改为2月比1月，仍看线上商城，按品类归因。",
    "那当期线上商城销售额总计多少？",
    "回到总体，比较2月比1月销售额，按渠道归因。",
    "第三方平台继续按品类下钻。",
    "那第三方平台这两个比较月份销售额合计多少？",
    "改为3月比2月，仍看第三方平台，按品类归因并画图。",
    "沿用当前范围，查看第三方平台2月和3月销售额走势并画折线图。",
    "回到总体，分析3月比2月销售额变化，按渠道归因并画图。",
]


def main():
    rows, session_id = [], None
    for index, question in enumerate(QUESTIONS, 1):
        body = {"question": question}
        if session_id:
            body["session_id"] = session_id
        started = time.perf_counter()
        request = Request("http://127.0.0.1:8001/api/v1/analyze",
                          data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=180) as response:
                result, status = json.load(response), response.status
        except HTTPError as error:
            result, status = json.load(error), error.code
        session_id = result.get("session_id") or session_id
        row = {"turn": index, "question": question, "http_status": status,
               "wall_ms": round((time.perf_counter() - started) * 1000),
               "response": result}
        rows.append(row)
        output = {"session_id": session_id, "turns": rows}
        Path("outputs/token_benchmark_20_turns.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"turn": index, "status": status, "finished": result.get("finished"),
                          "clarification": result.get("requires_clarification"),
                          "total_tokens": (result.get("usage") or {}).get("total_tokens"),
                          "ms": row["wall_ms"]}, ensure_ascii=False), flush=True)
        if index == 1 and not session_id:
            raise RuntimeError("首轮未创建会话，停止以避免测试变成独立请求。")
    print("session_id=" + session_id)


if __name__ == "__main__":
    main()
