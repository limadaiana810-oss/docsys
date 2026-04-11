"""
共享工具函数 - 避免各 Agent 重复代码
"""

import json
import math
import re
import sys
from pathlib import Path
from typing import Any, List, Optional


_SKILL_ROOT = str(Path(__file__).parent.parent)

SPACE_MAP = {"home": "family", "work": "work", "family": "family", "": "family"}


def _get_vector_path(space: str) -> str:
    """从 config.json 动态解析向量存储路径"""
    space_key = "home" if space in ("home", "family") else space
    space_cfg = config.get(f"spaces.{space_key}", {})
    root = space_cfg.get("root", "")
    if root:
        return root.rstrip("/") + "/hub/vectors"
    return str(Path.home() / ".openclaw" / "media" / space_key / "hub" / "vectors")

# ── 模型选型 ──────────────────────────────────────────────
# 主对话 / 编排          minimax/minimax-m2.7      (via ServiceFactory 默认)
# 图片理解 / OCR         qwen/qwen2.5-vl-72b-instruct
# 手抄报 / 导出长文       qwen/qwen3-32b
# 记忆蒸馏 / 信号提取    qwen/qwen2.5-14b-instruct
# Embedding             qwen/qwen3-embedding-8b   (via ServiceFactory 默认)
# 生图                  google/gemini-3.1-flash-image-preview (暂保留)
# ─────────────────────────────────────────────────────────

MULTIMODAL_MODEL = "qwen/qwen2.5-vl-72b-instruct"
WRITING_MODEL    = "qwen/qwen3-32b"
MEMORY_MODEL     = "qwen/qwen2.5-14b-instruct"

# 向后兼容别名
QWEN_MULTIMODAL_MODEL = MULTIMODAL_MODEL


def _ensure_docsys_path():
    if _SKILL_ROOT not in sys.path:
        sys.path.insert(0, _SKILL_ROOT)


def _make_llm(model: str):
    """用指定 model 覆盖默认配置，返回 LLMService 实例。"""
    _ensure_docsys_path()
    from services.api_layer import ServiceFactory, LLMService, ProviderConfig
    base = ServiceFactory.get_config()
    cfg = ProviderConfig(
        name=base.name,
        api_key=base.api_key,
        base_url=base.base_url,
        model=model,
        embedding_model=base.embedding_model,
        multimodal_model=base.multimodal_model,
    )
    return LLMService(cfg)


def get_llm():
    """主对话 / 编排 — minimax/minimax-m2.7"""
    _ensure_docsys_path()
    from services.api_layer import ServiceFactory
    return ServiceFactory.get_llm()


def get_multimodal_llm():
    """图片理解 / OCR — qwen/qwen2.5-vl-72b-instruct"""
    return _make_llm(MULTIMODAL_MODEL)


def get_writing_llm():
    """手抄报 / 导出长文 — qwen/qwen3-32b"""
    return _make_llm(WRITING_MODEL)


def get_memory_llm():
    """记忆蒸馏 / 信号提取 / 意图分类 fallback — qwen/qwen2.5-14b-instruct"""
    return _make_llm(MEMORY_MODEL)


def get_embedding_service():
    """Embedding — qwen/qwen3-embedding-8b"""
    _ensure_docsys_path()
    from services.api_layer import ServiceFactory
    return ServiceFactory.get_embedding()


def save_record_vector(space: str, record_id: str, vector: List[float]) -> str:
    """将多模态向量保存到文件，返回 vector_id（同 record_id）"""
    vec_dir = Path(_get_vector_path(space))
    vec_dir.mkdir(parents=True, exist_ok=True)
    vec_path = vec_dir / f"{record_id}.json"
    with open(vec_path, "w") as f:
        json.dump(vector, f)
    return record_id


def load_record_vector(space: str, record_id: str) -> Optional[List[float]]:
    """从文件加载多模态向量，不存在返回 None"""
    vec_path = Path(_get_vector_path(space)) / f"{record_id}.json"
    if not vec_path.exists():
        return None
    return json.loads(vec_path.read_text())


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """计算两个向量的余弦相似度"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def load_hub_storage(space: str):
    """
    加载 HubStorage。
    space: skill 侧空间名 ("home" / "work" / "family")
    """
    from . import storage as mod
    hub_space = SPACE_MAP.get(space, "family")
    return mod.HubStorage(hub_space), mod, hub_space


def extract_json(text: str) -> Any:
    """从 LLM 输出中提取第一个 JSON 对象/数组，处理控制字符"""
    m = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', text)
    if not m:
        return None
    raw = m.group()
    # 第一次尝试：直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 第二次尝试：清理 JSON 字符串值内的裸换行和控制字符
    # 在双引号内的 \n \r \t 替换为转义形式
    cleaned = re.sub(
        r'"((?:[^"\\]|\\.)*)"',
        lambda m: '"' + m.group(1)
            .replace('\n', '\\n')
            .replace('\r', '\\r')
            .replace('\t', '\\t') + '"',
        raw
    )
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"   ⚠️ JSON 解析失败: {e}")
        return None
