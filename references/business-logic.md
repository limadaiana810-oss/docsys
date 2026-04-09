# DocSys 业务逻辑梳理

## 整体架构

```
用户消息
    ↓
┌─────────────────────────────────────────┐
│           Layer1 Engine                  │
│         意图识别 + 槽位填充               │
│         Space.FAMILY / Space.WORK       │
└─────────────────────────────────────────┘
    ↓
    Plan (包含 intent + slots + steps)
    ↓
┌─────────────────────────────────────────┐
│          Archive Agent                   │
│     多模态归档（Gemini Flash）           │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│           Hub Storage                    │
│   SQLite + 文件系统 + 向量索引 (FAISS)   │
└─────────────────────────────────────────┘
```

## 唤醒机制

### 触发词

**唤醒词：「小凯」**

用户输入包含"小凯"即唤醒 DocSys。

### 完整交互流程

```
用户: "小凯，帮我找三角函数的错题"
    ↓
┌─────────────────────────────────────┐
│  🏠 DocSys 文档管理系统               │
│                                     │
│  📂 请选择操作空间：                  │
│                                     │
│  1️⃣ 家庭空间（孩子学习资料）         │
│  2️⃣ 办公空间（发票/合同等）          │
│                                     │
│  请回复数字 1 或 2                   │
└─────────────────────────────────────┘
    ↓ 用户选择 "1"
主Agent 理解需求
    ↓
┌─────────────────────────────────────┐
│  📋 任务编排中...                     │
│                                     │
│  intent: SEARCH                     │
│  space: family                     │
│  slots: {category: 数学, tags: 三角函数} │
│                                     │
│  steps:                             │
│    1. [SearchAgent] query          │
└─────────────────────────────────────┘
    ↓
SearchAgent.query() 执行
    ↓
┌─────────────────────────────────────┐
│  🔍 搜索结果（家庭空间）              │
│                                     │
│  找到 3 条结果：                     │
│                                     │
│  1. 三角函数练习题（2024-03-15）     │
│     摘要: 三角函数基础练习...        │
│                                     │
│  2. 三角函数测试卷（2024-03-10）     │
│     摘要: 三角函数单元测试...        │
│                                     │
│  3. 解三角形专题（2024-03-05）       │
│     摘要: 正弦余弦定理应用...        │
└─────────────────────────────────────┘
```

## 主从 Agent 架构

### 角色分工

| Agent | 类型 | 职责 |
|-------|------|------|
| **主Agent** | 调度者 | 理解需求、编排Plan、组装结果 |
| **ArchiveAgent** | 执行者 | 文件归档、多模态理解 |
| **SearchAgent** | 执行者 | 语义搜索、Filter-then-Rank |
| **ExportAgent** | 执行者 | 结果整合、导出文件 |

### 主Agent 调度流程

```
1. 接收用户需求（包含空间选择）
       ↓
2. 解析意图 + 提取槽位
       ↓
3. 编排执行步骤（Plan.steps）
       ↓
4. 按依赖顺序调用子Agent
       ↓
5. 收集子Agent返回结果
       ↓
6. 组装最终回复呈现给用户
```

### 子Agent 调用方式

```python
# 主Agent 调度子Agent
if intent == "ARCHIVE":
    agent = ArchiveAgent(space)
    result = await agent.ingest(file_path, plan)

elif intent == "SEARCH":
    agent = SearchAgent(space)
    result = await agent.query(plan, top_k=10)

elif intent == "EXPORT":
    # 串行: 先搜索再导出
    search_agent = SearchAgent(space)
    search_results = await search_agent.query(plan)
    
    export_agent = ExportAgent(space)
    result = await export_agent.compile(plan, search_results)
```

## Plan 编排规则

### 单步执行

```python
# 归档
steps = [{step_id: 1, agent: "archive_agent", action: "ingest"}]

# 搜索
steps = [{step_id: 1, agent: "search_agent", action: "query"}]
```

### 串行执行

```python
# 导出（先搜索再导出）
steps = [
    {step_id: 1, agent: "search_agent", action: "query"},
    {step_id: 2, agent: "export_agent", action: "compile", depends_on: [1]}
]
```

## 双空间独立 Hub

| 空间 | 路径 | 用途 |
|------|------|------|
| **family** | `/media/home/hub/` | 错题、试卷、作业 |
| **work** | `/media/work/hub/` | 发票、合同、报销 |

两个空间**完全独立**，各有 SQLite + 文件存储 + 向量索引。

| 空间 | 用途 | 存储路径 |
|------|------|---------|
| **family** | 家庭版（孩子学习资料） | `/media/home/hub/` |
| **work** | 办公版（发票/合同等） | `/media/work/hub/` |

## 意图分类 (Intent)

| 意图 | 关键词 | 触发动作 |
|------|--------|---------|
| **ARCHIVE** | 上传、归档、存储、保存、录入、添加、这是/这张/这份、拍、拍照、扫描 | 归档文件 |
| **SEARCH** | 找、搜索、查找、检索、看一下、有没有 | 语义搜索 |
| **EXPORT** | 导出、整理、汇总、生成、输出、整合、按、分组、归类 | 检索+导出 |

## 槽位系统 (Slots)

### 家庭空间 (FamilySlots)

| 槽位 | 说明 | 提取关键词 |
|------|------|-----------|
| time_range | 时间范围 | 上周、下周、3月、2024年 |
| member | 家庭成员 | 孩子、爸爸、妈妈、老人 |
| doc_type | 文档类型 | 错题、试卷、笔记、作业 |
| category | 学科 | 数学、语文、英语、物理、化学 |
| tags | 知识点 | 三角函数、概率统计、几何 |

### 办公空间 (WorkSlots)

| 槽位 | 说明 | 提取关键词 |
|------|------|-----------|
| time_range | 时间范围 | 同上 |
| project | 项目名称 | Alpha、Beta、杭州、北京 |
| doc_type | 文档类型 | 发票、合同、报告、清单 |
| business_category | 业务分类 | 差旅、采购、招待、办公 |
| tags | 自由标签 | 自定义 |

## 槽位置信度 (Confidence)

| 级别 | 说明 | 触发条件 |
|------|------|---------|
| **HIGH** | 明确识别 | 精确匹配关键词、日期格式 |
| **LOW** | 模糊/不确定 | 模糊表达（上周、好像）、时间缩写 |

## 澄清机制 (Clarification)

当存在 **LOW** 置信度槽位时，系统进入澄清模式：

1. 返回 `needs_clarification: true`
2. 提供 `clarify_options` 供用户选择
3. 用户确认后更新槽位，重新编排 Plan

## 核心流程

### 1. 归档流程 (Archive)

```
用户: "归档这个错题"
    ↓
Layer1 识别: intent=ARCHIVE, space=family, slots={doc_type: 错题}
    ↓
Plan 编排: steps=[{agent: archive_agent, action: ingest}]
    ↓
ArchiveAgent.ingest()
    ├─ 多模态理解图片 (Gemini Flash)
    │   ├─ 提取元数据 (doc_type, category, tags, difficulty)
    │   ├─ 识别文字 (extracted_text)
    │   └─ 判断方向/签名
    ├─ 生成语义摘要
    ├─ 生成向量 (qwen3-embedding-8b)
    ├─ 存储文件到 Hub/files/
    └─ 写入 SQLite + 向量索引
    ↓
返回 record_id + warnings (误放检测)
```

### 2. 搜索流程 (Search)

```
用户: "找一下三角函数的错题"
    ↓
Layer1 识别: intent=SEARCH, slots={category: 数学, tags: 三角函数}
    ↓
Plan 编排: steps=[{agent: search_agent, action: query}]
    ↓
用户确认 space (family/work)
    ↓
Hub.search()
    ├─ Filter: 元数据过滤 (doc_type, category, member, tags)
    └─ Rank: 向量相似度排序
    ↓
返回 Top-K 结果列表
```

### 3. 导出流程 (Export)

```
用户: "导出这学期的错题，按学科分组"
    ↓
Layer1 识别: intent=EXPORT, slots={time_range: 这学期}
    ↓
Plan 编排: steps=[
    {agent: search_agent, action: query},
    {agent: export_agent, action: compile, depends_on: [1]}
]
    ↓
先 search 查询结果
    ↓
再 export 按学科分组整理
    ↓
生成导出文件
```

## HubRecord 数据模型

```python
@dataclass
class HubRecord:
    # 基础信息
    record_id: str           # 唯一ID (UUID)
    space: str               # family / work
    original_path: str       # 文件实际路径
    file_name: str           # 原始文件名
    file_type: str           # MIME类型
    file_size: int           # 文件大小(bytes)
    created_at: int          # 原文件创建时间
    archived_at: int         # 归档时间
    
    # 元数据
    member: str              # 家庭成员
    doc_type: str            # 文档类型
    category: str            # 学科/分类
    tags: str (JSON)         # 标签数组
    
    # 办公空间
    project: str             # 项目名称
    business_category: str   # 业务分类
    
    # 语义理解
    semantic_summary: str     # 语义摘要
    synonyms: str (JSON)      # 同义词数组
    
    # 多模态扩展
    extracted_text: str       # 图片识别文字
    difficulty: str           # 难度
    orientation: str          # 横版/竖版
    has_signature: bool      # 是否有签名
    
    # 向量
    vector_id: str           # FAISS 向量ID
```

## 存储结构

```
/media/
├── home/hub/
│   ├── files/              # 原始文件
│   │   └── 20260324193519_27d3b0ca.jpg
│   ├── summary/            # 语义摘要文件
│   │   └── <record_id>.txt
│   └── meta/
│       └── meta.db         # SQLite 数据库
└── work/hub/
    ├── files/
    ├── summary/
    └── meta/
        └── meta.db
```

## 多模态模型配置

| 模型 | 用途 | Provider |
|------|------|----------|
| **google/gemini-3.1-flash-image-preview** | 图片理解 | OpenRouter |
| minimax/minimax-m2.7 | 文本生成 | OpenRouter |
| qwen/qwen3-embedding-8b | 向量嵌入 | OpenRouter |

## 关键设计

### 1. Filter-then-Rank 搜索策略

```
1. Filter: 先用元数据过滤（doc_type, category, member）
2. Rank: 再用向量相似度排序
```

### 2. 误放检测

归档时检查关键词冲突：
- 家庭空间检测到办公关键词（发票/报销/合同）→ 提示是否误放
- 办公空间检测到家庭关键词（错题/试卷/孩子）→ 提示是否误放

### 3. 向量缓存

Embedding 结果缓存，避免重复计算：
```python
_cache = {text[:100]: vector}
```

### 4. 数据库迁移

新增字段时自动 ALTER TABLE：
```python
for col in [("extracted_text", "TEXT"), ...]:
    try:
        cursor.execute(f"ALTER TABLE records ADD COLUMN {col[0]} {col[1]}")
    except sqlite3.OperationalError:
        pass  # 列已存在
```

## API Key 优先级

1. 环境变量 `OPENROUTER_API_KEY`
2. `~/.openrouter/config` 配置文件
3. 环境变量 `OPENAI_API_KEY`

## 使用限制

- SQLite 适合中小规模（万级记录）
- 向量维度：1024 (qwen3-embedding)
- 单次搜索返回：默认 top_k=10
- 图片大小限制：无明确限制，建议 < 10MB
