"""M0-6：评测集抽样跑批（判分逻辑与正式版 eval/run_eval.py 对齐）。

- 每条用例独立 thread；
- interrupt（高危工具）视为"模型已做出工具选择"：计入 called 后结束该条，
  不 resume（避免真实执行，也不污染 forbidden 判定）；
- hit = expected_tools 全部命中（支持 a|b 任一）且 forbidden_tools 未出现。

用法：.venv/bin/python poc/run_eval_sample.py 1 8 9 26 40 43 46 50 56 71
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from self_agent.agent import build_agent
from self_agent.mcp import load_mcp_tools

EVALSET = Path("/Users/kevinxu/source/agent/无人机智能体/正式开发/eval/evalset.jsonl")
BASELINE = Path("/Users/kevinxu/source/agent/无人机智能体/正式开发/eval/last_run.json")
OUT = Path("eval_sample_result.json")


def _turn_calls(messages, utterance: str):
    idx = 0
    for i, m in enumerate(messages):
        if m.__class__.__name__ == "HumanMessage" and utterance in str(m.content):
            idx = i
    calls = []
    for m in messages[idx:]:
        for tc in getattr(m, "tool_calls", None) or []:
            if tc.get("name"):
                calls.append({"tool": tc["name"], "args": tc.get("args") or {}})
    return calls


async def run_case(agent, case) -> dict:
    cfg = {"configurable": {"thread_id": f"eval-{case['id']}"}}
    interrupted = []
    # setup 轮：interrupt 一律拒绝以维持上下文推进
    for pre in case.get("setup", []):
        r = await agent.ainvoke({"messages": [("user", pre)]}, config=cfg)
        while r.get("__interrupt__"):
            r = await agent.ainvoke(
                Command(resume={"decisions": [{"type": "reject", "message": "评测环境跳过"}]}),
                config=cfg,
            )
    t0 = time.time()
    r = await agent.ainvoke({"messages": [("user", case["utterance"])]}, config=cfg)
    if r.get("__interrupt__"):
        v = r["__interrupt__"][0].value
        for req in v.get("action_requests", []) if isinstance(v, dict) else []:
            interrupted.append(req.get("name"))
    elapsed = round(time.time() - t0, 1)
    calls = _turn_calls(r["messages"], case["utterance"])
    called = [c["tool"] for c in calls] + [n for n in interrupted if n]

    hit = all(any(alt in called for alt in exp.split("|")) for exp in case["expected_tools"])
    if any(f in called for f in case.get("forbidden_tools", [])):
        hit = False
    args_ok = True
    for tool, wanted in (case.get("expected_args") or {}).items():
        got = next((c["args"] for c in calls if c["tool"] == tool), None)
        if got is None:
            args_ok = False
            continue
        blob = json.dumps(got, ensure_ascii=False)
        if not all(str(v) in blob for v in wanted.values()):
            args_ok = False
    return {"id": case["id"], "hit": hit, "args_ok": args_ok, "called": called,
            "interrupted": interrupted, "elapsed_s": elapsed}


async def main():
    ids = [int(a) for a in sys.argv[1:]] or [1, 8, 9, 26, 40, 43, 46, 50, 56, 71]
    cases = {c["id"]: c for c in map(json.loads, EVALSET.open())}
    baseline = {r["id"]: r for r in json.load(BASELINE.open())}
    tools, down = await load_mcp_tools()
    print(f"MCP 工具 {len(tools)} 个；不可用域: {down or '无'}")
    agent = build_agent(tools, down_domains=down, checkpointer=InMemorySaver(),
                        store=InMemoryStore())  # /memories 路由需要 store（#8 教训）

    results = []
    for i in ids:
        case = cases[i]
        try:
            res = await asyncio.wait_for(run_case(agent, case), timeout=240)
        except Exception as e:
            res = {"id": i, "hit": False, "args_ok": False, "called": [],
                   "interrupted": [], "elapsed_s": -1, "error": f"{type(e).__name__}: {e}"[:120]}
        b = baseline.get(i, {})
        mark = "✅" if res["hit"] else "❌"
        print(f"{mark} #{i} {case['utterance'][:30]} → called={res['called']}"
              f" ({res['elapsed_s']}s) 基线:{'✓' if b.get('hit') else '✗'}"
              + (f" ⚠{res['error']}" if res.get("error") else ""))
        results.append(res)

    hits = sum(r["hit"] for r in results)
    base_hits = sum(1 for i in ids if baseline.get(i, {}).get("hit"))
    print(f"\n== 抽样命中率: {hits}/{len(ids)}   基线同集: {base_hits}/{len(ids)} ==")
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"结果已写 {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
