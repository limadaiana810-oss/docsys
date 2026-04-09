# DocSys 系统架构

## 整体架构

```
用户消息/图片
     ↓
┌─────────────────────────────────────┐
│           Layer1 Engine             │
│         意图识别 + 槽位填充          │
└─────────────────────────────────────┘
     ↓
┌─────────────────────────────────────┐
│          Archive Agent              │
│      多模态归档 + 元数据提取          │
└─────────────────────────────────────┘
     ↓
┌─────────────────────────────────────┐
│           Hub Storage               │
│    文件存储 + SQLite + 向量索引      │
└─────────────────────────────────────┘
```

## 核心组件

### 1. Layer1 Engine (`layer1/engine.py`)

意图识别和槽位填充。

```python
from layer1.engine import Layer1Engine, Space

engine = Layer1Engine()
result = engine.process("归档错题", Space.FAMILY)
# result = {"intent": "...", "slots": {...}, "plan": {...}}
```

### 2. Archive Agent (`agents/archive/agent.py`)

**多模态归档**（新版）：
```python
from agents.archive.agent import ArchiveAgent

agent = ArchiveAgent("family")
result = await agent.ingest(file_path, plan)
```

**OCR 归档**（旧版，已废弃）：
```python
from agents.archive.agent import ArchiveAgentLegacy

agent = ArchiveAgentLegacy("family")
result = await agent.ingest(file_path, plan)
```

### 3. Hub Storage (`hub/storage.py`)

文件存储和检索。

```python
from hub.storage import Hub

hub = Hub("family")

# 归档
record = hub.archive(file_path=..., metadata=..., ...)

# 搜索
results = hub.search(filters={}, query_vector=vec, top_k=10)

# 列表
records = hub.storage.list(limit=50)

# 更新元数据
hub.storage.update_metadata(record_id, metadata_dict)
```

## 数据模型

### HubRecord 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| record_id | str | 唯一ID |
| space | str | family/work |
| original_path | str | 文件路径 |
| file_name | str | 文件名 |
| file_type | str | MIME类型 |
| file_size | int | 文件大小 |
| member | str | 家庭成员 |
| doc_type | str | 文档类型 |
| category | str | 学科/分类 |
| tags | str | JSON标签数组 |
| semantic_summary | str | 语义摘要 |
| synonyms | str | JSON同义词数组 |
| extracted_text | str | 图片识别文字 |
| difficulty | str | 难度 |
| orientation | str | 横版/竖版 |
| has_signature | bool | 是否有签名 |
| vector_id | str | 向量ID |

## 存储路径

```
/Users/kk/.openclaw/media/
├── home/hub/
│   ├── files/          # 原始文件
│   ├── summary/        # 摘要文件
│   └── meta/meta.db     # SQLite 数据库
└── work/hub/
    ├── files/
    ├── summary/
    └── meta/meta.db
```

## 多模态模型

| 模型 | 用途 | Provider |
|------|------|----------|
| google/gemini-3.1-flash-image-preview | 图片理解 | OpenRouter |
| minimax/minimax-m2.7 | 文本生成 | OpenRouter |
| qwen/qwen3-embedding-8b | 向量嵌入 | OpenRouter |

## API Key 配置

优先级：
1. 环境变量 `OPENROUTER_API_KEY`
2. `~/.openrouter/config` 配置文件
3. 环境变量 `OPENAI_API_KEY`
