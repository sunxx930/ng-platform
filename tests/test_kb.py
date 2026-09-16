"""知识库检索+注入 v1.2.7。"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.services import kb
from app.agents.builtin import BuiltinAgent, TaskContext


@pytest.fixture()
def kbenv(tmp_path, monkeypatch):
    monkeypatch.setenv("NG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "knowledge" / "tax-cases"
    d.mkdir(parents=True)
    (d / "a.md").write_text("---\ntitle: 出海架构\nsource: 样例.pptx\n---\n"
                            "间接控股架构可利用香港公司股息豁免，降低股息预提税。", encoding="utf-8")
    (d / "b.md").write_text("---\ntitle: 无关文档\n---\n今天天气不错。", encoding="utf-8")
    return tmp_path


def test_search_and_context(kbenv):
    hs = kb.search("香港 股息 架构")
    assert hs and hs[0]["title"] == "出海架构"
    assert "香港" in hs[0]["snippet"]
    block = kb.context_block("香港 股息")
    assert "知识库参考" in block and "样例.pptx" in block
    assert kb.context_block("完全不相关词xyz") == ""


def test_builtin_prompt_includes_knowledge(kbenv):
    class FakeLLM:
        last_user = ""
        def complete(self, system, user, **kw): type(self).last_user = user; return "交付内容"
        def usage(self): return []
    fake = FakeLLM()
    BuiltinAgent(llm=fake).execute(TaskContext(task_id="t1", title="税务架构",
                                               knowledge="【知识库参考】X 来源: 出海架构"))
    assert "知识库参考" in FakeLLM.last_user
