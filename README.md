# DocSys - AI-Native Document Management Plugin

智能文档管理系统，支持图片归档、语义搜索、文档导出、AI 生图等功能。

## 特性

- **智能归档**：自动识别图片内容（错题、发票等），生成向量索引
- **三策略搜索**：精确/部分/模糊三种搜索策略，自动选择最优方案
- **文档导出**：支持错题本、发票报销等多种模板导出为 DOCX
- **AI 生图**：根据描述生成手抄报、海报等图片
- **双空间隔离**：home（学习）/ work（办公）数据完全隔离
- **记忆系统**：自动记忆用户偏好，支持冷启动引导

## 安装

1. 克隆仓库到你的 skills 目录：
```bash
cd ~/.openclaw/skills
git clone https://github.com/YOUR_USERNAME/docsys.git
```

2. 复制配置模板：
```bash
cd docsys
cp config.template.json config.json
```

3. 编辑 `config.json`，配置路径和模型：
   - 修改 `{HOME}` 为你的实际路径
   - 配置 `multimodal_model` 和 `image_gen_model`

## 配置

### 必需配置

在 `config.json` 中配置：

```json
{
  "paths": {
    "media": "/path/to/media/",
    "outbound": "/path/to/outbound/",
    "workspace": "/path/to/workspace/"
  },
  "db": {
    "home": "/path/to/home/hub/meta.db",
    "work": "/path/to/work/hub/meta.db"
  },
  "multimodal_model": "qwen/qwen2.5-vl-72b-instruct",
  "image_gen_model": "google/gemini-3.1-flash-image-preview"
}
```

### Hub 依赖

DocSys 依赖 OpenClaw Hub 系统提供：
- LLM 服务（通过 OpenRouter）
- 向量存储
- 记忆系统

确保你的环境中已配置 Hub 相关服务。

## 使用

### 归档图片

发送图片附件，系统自动识别并归档：
```
用户：[上传错题图片]
系统：已归档到 learning/wrong_questions/
```

### 搜索文档

```
用户：搜索三角函数的题目
系统：找到 3 条相关记录...
```

### 导出文档

```
用户：导出最近的错题
系统：已生成错题本 DOCX
```

### 生成图片

```
用户：生成一张关于春天的手抄报
系统：[生成的图片]
```

## 架构

```
docsys/
├── agents/          # 子 Agent
│   ├── main.py      # 主编排 Agent
│   ├── archive/     # 归档 Agent
│   ├── search/      # 搜索 Agent
│   ├── export/      # 导出 Agent
│   └── image/       # 生图 Agent
├── handlers/        # 入站处理器
│   ├── inbound.py   # 批量扫描归档
│   └── oral_math.py # 口算题生成
├── hub/             # Hub 工具
│   ├── utils.py     # LLM/向量/记忆工具
│   ├── memory.py    # 记忆系统
│   └── storage.py   # 存储接口
└── config.json      # 配置文件
```

## 开发

### 添加新的文档类型

1. 在 `agents/archive/agent.py` 中添加识别逻辑
2. 在 `agents/export/agent.py` 中添加导出模板
3. 更新 `config.json` 中的 `sub_spaces` 配置

### 自定义搜索策略

编辑 `agents/search/agent.py` 中的 `_select_strategy()` 方法。

## License

MIT
