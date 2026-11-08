"""语义模型扩展接口。默认不加载模型、不联网、不改变规则决策。"""
from typing import Protocol


class SemanticAdvisor(Protocol):
    def advise(self, text: str, references: dict) -> dict:
        """返回 game_similarity、event_similarity 等辅助分数，不能自动删除。"""
        ...


def add_advice(result, text, config, advisor=None):
    if advisor is not None and result["relevance_label"] in {"uncertain", "likely_related"}:
        advice = advisor.advise(text, config["semantic"])
        # 分数仅写入附加字段，不允许未经验证的模型覆盖类别。
        result["semantic_advice"] = advice
    else:
        result["semantic_advice"] = {}
    return result
