# DocSys Skill — Claude 工作指南

## 项目是什么

OpenClaw 的一个 skill，帮用户管理文档/学习资料/发票。
主要场景：上传图片归档 → 搜索 → 导出可打印文件 / 生成手抄报配图。

---

## 目录结构

```
docsys/
├── agents/
│   ├── main.py          # 主 Agent（有状态，入口）
│   ├── archive/agent.py # 图片入库
│   ├── search/agent.py  # 语义搜索
│   ├── export/agent.py  # 导出 docx/pdf
│   └── image/agent.py   # 生图（OpenRouter/Flux/DALLE）
├── handlers/
│   ├── inbound.py       # 入站图片批量扫描/处理
│   └── poster.py        # 手抄报：LLM生文案 + ImageAgent生图
├── hub/
│   ├── config.py        # 配置加载器（单例，读 config.json）
│   ├── storage.py       # HubStorage / Hub / HubRecord（SQLite 封装）
│   ├── utils.py         # 共享工具：get_llm / get_multimodal_llm / get_embedding_service /
│   │                    #           save_record_vector / load_record_vector / cosine_similarity /
│   │                    #           load_hub_storage / extract_json / SPACE_MAP
│   ├── memory.py        # UserMemory（MemoryGraph + EpisodeLog + Notes）
│   ├── profile.py       # UserProfileProvider（用户画像懒加载）
│   └── prompt_builder.py # 生图 Prompt 构建（配置驱动）
├── services/
│   └── api_layer.py     # ServiceFactory / LLMService / EmbeddingService（OpenRouter/Kimi/Ollama）
├── scripts/             # 独立运维脚本
│   ├── archive.py       # 单文件归档
│   ├── search.py        # 语义搜索
│   ├── reindex.py       # 批量重新索引
│   ├── list_files.py    # 列出 Hub 记录
│   └── inspect.py       # 查看记录详情
├── references/          # 架构/业务逻辑文档
├── config.json          # 所有路径/空间/模型配置
├── SKILL.md             # 对外介绍文档（prompt 运行模式）
└── CLAUDE.md            # 开发指南（Python 运行模式）
```

---

## 核心架构

### 主从 Agent 模式
- **主 Agent**（`DocSysMainAgent`）有状态：持有 conversation_history、UserMemory、UserProfile
- **子 Agent**（archive/search/export/image）无状态：通过 `TaskContext` 接收所有参数，执行完即丢弃
- 子 Agent 结果通过 `AgentResult` 返回给主 Agent

### 任务流水线
```
用户输入
  → _auto_memorize()           # 正则提取并写入长期记忆
  → _extract_signal_memory()   # 高信号词 → fire-and-forget LLM 提取
  → _handle_onboarding()?      # 冷启动引导拦截（画像为空时）
  → _understand_intent()
      → _match_intent_keywords()  # 关键词路由（有优先级，含附件兜底）
      → _llm_classify_intent()    # fallback：关键词返回 unknown 时
      → _extract_params()
      → _needs_export()?          # search/archive → search_export/archive_export
      → _generate_plan()
  → _execute_intent()          # 按依赖图并发执行
  → _format_response()
  → _distill_session()         # 异步记忆蒸馏（fire-and-forget）
```

### 异步执行行为
- 无依赖步骤并发执行（`asyncio.gather`）
- 有 `depends_on` 的步骤等前置完成后执行
- **前置步骤失败会中止后续依赖步骤**，返回明确错误，不静默执行空结果
- **`_generate_plan` 返回空计划时报错**（`all([]) = True` 会掩盖遗漏任务）

### 并发优化（已实现）
- `_understand_intent`：`create_task(get_profile)` → 同步执行 `build_context()` → `await profile_task`（profile 文件 I/O 与 memory 计算并发）
- `ingest()`：`asyncio.gather(_save_to_space, _generate_vector)`（文件 copy 与 embedding API 并发）
- `_search_fuzzy`：`create_task(_embed_query)` → 同步执行 Path B（OCR 全文匹配）→ `await embedding_task` → Path A（embedding 网络等待与 OCR 字符串计算并发）

---

## 关键约束

### HubStorage 加载
```python
from hub import load_hub_storage
hub, mod, hub_space = load_hub_storage(space)  # space: "home"/"work"/"family"
```
`hub/storage.py` 已在本 skill 目录内，`load_hub_storage` 使用相对 import 加载。

### 空间名映射
skill 用 `"home"/"work"`，HubStorage 用 `"family"/"work"`。映射在 `hub/utils.py`：
```python
from hub import SPACE_MAP  # 不要在各处重复定义
```

### LLM / 模型获取
```python
from hub import get_llm              # 默认文本 LLM（通用对话/分类）
from hub import get_multimodal_llm   # Qwen 多模态（图片理解，via OpenRouter）
from hub import get_embedding_service  # Embedding
```
- 图片分析必须用 `get_multimodal_llm()`，`get_llm()` 无视觉能力
- 任何地方（包括 `_distill_session` 等内部方法）都通过 hub 获取，不直接调 `ServiceFactory`

### JSON 提取
```python
from hub import extract_json
data = extract_json(llm_response)  # 返回 dict/list 或 None
```

### sys.path 注入
```python
sys.path.insert(0, '/Users/kk/.openclaw/skills/docsys')  # 唯一入口，所有模块在此目录下
```

### user_profile 数据来源
`_understand_intent` 中 `self.user_profile` 同时包含：
- `UserProfileProvider`（文件来源）：`child_age`、`style_preference` 等
- `memory.graph.get_all()`（结构化事实）：`learning.grade`、`learning.current_subjects` 等

子 Agent 查 profile 时两套 key 都有，按需用 `or` 兜底：
```python
grade = user_profile.get("grade") or user_profile.get("learning.grade")
```
不要只从 `profile_provider` 取 profile 直接传给子 Agent，会缺失 memory.graph 里的年级/科目。

### 子 Agent 不保存状态
`__init__` 只初始化配置，任务数据全通过 `TaskContext.params` 传入。

---

## 记忆系统

### 分层结构
```
UserMemory
├── MemoryGraph（长期，JSON 持久化）
│   ├── user.*      基本信息（child_age, preferred_format, ...）
│   ├── learning.*  学习信息（grade, school_type, current_subjects, ...）
│   ├── work.*      工作信息（company, expense_types, ...）
│   ├── task.*      任务元信息（last_action, last_time）
│   ├── notes.*     自然语言记忆（uuid key，偏好/习惯/纠错）
│   └── onboarding.done
├── EpisodeLog（滚动 JSONL，max 10 条）
│   └── 超 10 条时 pop 最旧 5 条 → LLM 压缩 → 写回 MemoryGraph
└── ContextWindow（短期，会话级）
```

### build_context() 注入顺序（预算 900 chars）
1. `【用户画像】` — user/learning/work，90天未访问加 `（旧）`
2. `【近期记录】` — 最近 3 条 episode
3. `【相关记忆】` — 按意图关键词检索（预算剩余时）
4. `【记住的事】` — 最近 3 条 notes

### 记忆写入路径
| 来源 | 方法 | 触发时机 |
|------|------|----------|
| 正则提取 | `_auto_memorize()` | 每条用户输入 |
| LLM 提取 | `_extract_signal_memory()` | 输入含高信号词 |
| 会话蒸馏 | `_distill_session()` | 任务完成后 fire-and-forget |
| 冷启动引导 | `_complete_onboarding()` | 引导完成时 |
| 归档后学科发现 | `_format_archive_result()` | 归档后 |

### _auto_memorize 注意事项
- 科目/报销类型检测不加 `break`，一条消息可提取多个
- `current_subjects` 和 `expense_types` 是列表，追加不覆盖
- `grade <= 6` → 小学（无需显式关键词）
- `memorize_user()` 签名是 `**kwargs`，不要传 `tags=[...]`（会创建 `user.tags` 节点）
- 存在判断用 `memory.recall("learning.grade")`，不要用 `get_by_prefix()` 的返回值

### 高信号词（触发 LLM 提取）
`"不对" "不是" "应该" "每次" "以后" "习惯" "总是" "记住" "喜欢" "不喜欢" "叫" "名字"`

---

## 意图路由

### 优先级顺序
```
wake > confirm_delete/cancel_delete（有 pending_delete 时）> stats > delete
> memory_query > poster > image > search > archive > export
```

### 关键规则
- `confirm_delete`/`cancel_delete` 只在 `self._pending_delete` 不为 None 时生效
- `search_export`/`archive_export` 不是独立意图，在 `_understand_intent` 里由 `_needs_export()` 组合产生
- **附件即归档**：所有关键词不匹配且 `self._pending_attachments` 非空时返回 `"archive"`，不走 LLM fallback。冷启动阶段直接发图也会触发此逻辑并自动完成引导。
- **LLM fallback**：关键词匹配返回 `unknown` 时调 `_llm_classify_intent()`（max_tokens=10）
- `wake` 有附件时结果含 `"action": "wake_batch"`，`_format_response` 内部识别，不要新增独立意图分支

### 触发 `_needs_export()` 的关键词
`"导出" "打印" "可打印" "输出成" "生成文件" "PDF" "Word"`

### 多目标拆分（`_is_multi_target`）
- `以及/还有/分别/各自/两个/2个/多个` 直接触发
- `"和"/"与"` 需同时匹配 2 个文档类名词（错题/发票/试卷等）才触发，避免误判连词

---

## 归档流水线

```
图片文件
  → _analyze_once()          Qwen 多模态（一次调用完成所有分析）
      输出：caption / space / sub_space / doc_type / category /
            keywords / extracted_text（完整 OCR）/ difficulty / confidence
  → 确定 space/sub_space     space_hint 优先；置信度低时 _infer_sub_space() 兜底
  → _save_to_space()         copy 到 media/{space}/{sub_space}/
  → _generate_vector()       embed(caption + extracted_text + doc_type + keywords)
  → _write_to_hub()
      ├─ save_record_vector() → vectors/{record_id}.json
      └─ hub.add(HubRecord)  → SQLite
```

- `archive_export` 支持多文件：plan 为每个文件生成独立 archive 步骤，export 步骤依赖全部
- 向量只在归档流程里生成和修改，不要在 `archive/agent.py` 之外操作
- 向量读写用 `save_record_vector()` / `load_record_vector()`，不要直接操作路径

---

## 搜索逻辑

三策略由 `_select_strategy()` 自动选择：

| 策略 | 触发条件 | 核心方法 |
|------|---------|---------|
| `precise` | dims≥3 且无语义残留 | SQL过滤 + 时间倒序 |
| `partial` | dims 1~2 或标签+语义混合 | SQL粗筛 → 向量细排 → 不足时补充 |
| `fuzzy` | 无标签 | 向量(Path A) + OCR全文(Path B) + RRF融合 |

- 标签维度：`_TYPE_TAGS / _SUBJECT_TAGS / _SPACE_TAGS / _TIME_TAGS / _DETAIL_TAGS`
- `_parse_query()` 同步方法，只做正则提取，不调 LLM
- `_extract_search_query()` 只删 `INTENT_KEYWORDS["search"]` 里的词，不删其他意图词（避免查询内容被污染）
- 模糊搜索用 `_build_query_text()` 注入 user_profile 增强 embedding，不做 query 改写
- `VECTOR_SCORE_THRESHOLD=0.35`，`MIN_PARTIAL_CANDIDATES=5`，`RRF_K=60`
- 不要加 LLM query 改写（个人库规模小，embedding 语义能力已足够，改写徒增延迟）
- `search()` 接受 `user_profile` 参数，由 `_call_agent()` 注入；无 `semantic_search()` 方法

---

## 冷启动引导

入口：`handle()` 在 `_auto_memorize` 之后、`_understand_intent` 之前拦截。

状态（`self._onboarding`）：`None` = 未检测 / `{"step": N}` = 进行中 / `"done"` = 完成

- step=0：介绍两个空间，邀请发图，不追问
- step=1：完成引导，顺带捕捉偏好
- 检测到真实意图（非 unknown/wake/memory_query）或有附件时立即跳过，执行任务

`_is_cold_start()` 检查：以下全为 None 则冷启动：
`user.child_age / user.preferred_format / learning.grade / learning.current_subjects / work.expense_types / work.company`

---

## 存储路径

| 数据 | 物理路径 |
|------|---------|
| home 媒体文件 | `/Users/kk/.openclaw/media/home/` |
| work 媒体文件 | `/Users/kk/.openclaw/media/work/` |
| 导出文件 | `/Users/kk/.openclaw/media/outbound/` |
| workspace | `/Users/kk/.openclaw/workspace/` |
| HubStorage SQLite (home) | `/Users/kk/.openclaw/media/home/hub/meta/meta.db` |
| HubStorage SQLite (work) | `/Users/kk/.openclaw/media/work/hub/meta/meta.db` |
| 多模态向量 | `/Users/kk/.openclaw/media/{space}/hub/vectors/{record_id}.json` |

---

## 常见修改场景

**加新意图**：
1. `INTENT_KEYWORDS` 加关键词
2. `_match_intent_keywords` 的 priority 列表加入（注意优先级位置）
3. `_generate_plan` 加 elif 分支（**必须生成至少一个步骤，否则执行层会报错**）
4. `_call_agent` 加 elif 分支
5. `_format_response` 加 elif 分支，实现 `_format_xxx_result()`

**加新子 Agent**：
- 实现 `async def main_method(...) -> ResultDataclass`，dataclass 含 `success`/`error` 字段
- `_call_agent` 用 `AgentResult` 包装返回

**修改归档分类**：
- LLM prompt：`archive/agent.py` `_analyze_once()`
- sub_space 推断兜底：`_infer_sub_space()`
- sub_space → doc_type 映射：`_write_to_hub()` 的 `SUB_SPACE_DOC_TYPE`

**修改多模态模型**：
- 模型名：`hub/utils.py` 的 `QWEN_MULTIMODAL_MODEL` 常量（当前：`qwen/qwen2.5-vl-72b-instruct`）
