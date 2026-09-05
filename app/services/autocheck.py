"""数值任务自动复算比对（v1.2.1，AUTO 复核辅助层）。

背景：数值/统计类交付（数学家连中两次算术错），人工复核是唯一防线。
本模块在人工复核前加一道**独立自动复算**：拿交付物里的输入+算式/代码
再算一遍，给出 PASS / CHECK，落到 `auto.rechecked` 事件供审计与前端展示。

约定：
- 只对"数值标题"任务生效（与前端 NUMERIC_WORDS 同口径）
- **best-effort**：算力未配/网络/解析失败 → 返回 NA + reason，绝不阻塞主流程
- 人工复核仍是最终决策（本模块只出参考，不自动改判）
"""
from __future__ import annotations

from app.domain import events
from app.services.llm import LLMClient

# 与 frontend/src/App.jsx NUMERIC_WORDS 保持一致
NUMERIC_WORDS = ['计算', '统计', '权重', '比率', '胜率', '均值',
                 '检验', '概率', '金额', '算', '汇总', '指标']


def is_numeric_title(title: str) -> bool:
    return bool(title) and any(w in title for w in NUMERIC_WORDS)


_SYS = ("你是数值复核器。任务是数值/统计类。给你任务的输入，以及交付物里的算式或代码，"
        "请独立重新计算关键数值，与交付结论比对。只输出一行 JSON，不要其它文字："
        '{"verdict":"PASS"|"CHECK","note":"一句话说明：通过 / 差异多少 / 依据 / 无法判定原因"}')


def _latest_deliverable(log, task_id: str):
    fr = None
    for e in log.replay(task_id=task_id):
        if e["event_type"] == events.EventType.DELIVERABLE_SUBMITTED.value \
                and e["payload"].get("file_ref"):
            fr = e["payload"]["file_ref"]
    return fr


def _title_desc(log, task_id: str):
    title = desc = ""
    for e in log.replay(task_id=task_id):
        if e["event_type"] == events.EventType.TASK_CREATED.value:
            title = e["payload"].get("title", "")
            desc = e["payload"].get("description", "")
    return title, desc


def _parse(text: str):
    import re
    m = re.search(r'"verdict"\s*:\s*"([A-Z]+)"', text)
    verdict = m.group(1) if m else "CHECK"
    n = re.search(r'"note"\s*:\s*"([^"]*)"', text)
    return verdict, (n.group(1) if n else text[:160])


def run_autocheck(project_id: str, task_id: str, log, llm=None) -> dict:
    """对数值任务执行一次自动复算；返回 {verdict, note, checked:bool}。

    checked=True 表示已落 auto.rechecked 事件（幂等按 交付物 file_ref）。
    """
    title, desc = _title_desc(log, task_id)
    if not is_numeric_title(title):
        return {"verdict": "SKIP", "note": "非数值任务，无需自动复算", "checked": False}
    try:
        fr = _latest_deliverable(log, task_id)
        if not fr:
            return {"verdict": "NA", "note": "无交付物，无法自动复算", "checked": False}
        from app.storage.artifacts import resolve_artifact
        content = resolve_artifact(fr).read_text(encoding="utf-8", errors="replace")[:6000]
        client = llm or LLMClient()
        resp = client.complete(_SYS,
                               f"任务：{title}\n描述：{desc or '（无补充）'}\n交付物({fr})内容：\n{content}")
        verdict, note = _parse(resp)
        log.append(events.new_event(
            events.EventType.AUTO_RECHECKED, "auto",
            {"verdict": verdict, "note": note, "file_ref": fr},
            project_id=project_id, task_id=task_id,
            idempotency_key=f"auto:recheck:{task_id}:{fr}"))
        return {"verdict": verdict, "note": note, "checked": True}
    except Exception as e:      # noqa: BLE001  best-effort
        return {"verdict": "NA", "note": f"自动复算不可用：{type(e).__name__}: {e}",
                "checked": False}
