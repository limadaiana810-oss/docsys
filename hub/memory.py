"""
DocSys 记忆模块

分层记忆架构：
- ContextWindow：短期记忆（对话上下文）
- MemoryGraph：长期记忆（跨会话）
"""

import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MemoryNode:
    """记忆节点"""
    key: str
    value: Any
    tags: List[str] = field(default_factory=list)
    confidence: float = 1.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_access: str = field(default_factory=lambda: datetime.now().isoformat())
    access_count: int = 1
    
    def to_dict(self) -> Dict:
        return {
            "key": self.key,
            "value": self.value,
            "tags": self.tags,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "last_access": self.last_access,
            "access_count": self.access_count
        }
    
    @classmethod
    def from_dict(cls, d: Dict) -> "MemoryNode":
        return cls(
            key=d.get("key", ""),
            value=d.get("value"),
            tags=d.get("tags", []),
            confidence=d.get("confidence", 1.0),
            created_at=d.get("created_at", datetime.now().isoformat()),
            last_access=d.get("last_access", datetime.now().isoformat()),
            access_count=d.get("access_count", 1)
        )


class ContextWindow:
    """
    短期记忆 - 对话上下文窗口
    
    管理当前对话的上下文，支持压缩和摘要
    """
    
    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self.messages: List[Dict[str, str]] = []  # [{"role": "user", "content": "..."}]
        self.entities: Dict[str, Any] = {}  # 本次对话中提取的实体
        self.intent_stack: List[Dict] = []  # 意图栈
        self.task_state: Optional[Dict] = None  # 当前任务状态
    
    def add(self, role: str, content: str):
        """添加消息"""
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        self._maybe_compact()
    
    def _maybe_compact(self):
        """超过限制时压缩"""
        if len(self.messages) > self.max_turns:
            self._compact()
    
    def _compact(self):
        """压缩：保留最近的，旧的合并成摘要"""
        keep_count = self.max_turns // 2
        old_messages = self.messages[:-keep_count]
        new_messages = self.messages[-keep_count:]
        
        # 生成摘要
        summary = self._summarize(old_messages)
        self.messages = [{"role": "system", "content": f"[上文摘要] {summary}"}] + new_messages
    
    def _summarize(self, messages: List[Dict]) -> str:
        """生成摘要 - 简单实现"""
        if not messages:
            return ""
        
        user_msgs = [m["content"][:50] for m in messages if m["role"] == "user"]
        return f"用户问了{len(user_msgs)}个问题，涉及: {', '.join(user_msgs[:3])}..."
    
    def add_entity(self, key: str, value: Any):
        """添加实体"""
        self.entities[key] = value
    
    def get_entity(self, key: str) -> Optional[Any]:
        """获取实体"""
        return self.entities.get(key)
    
    def set_task_state(self, state: Dict):
        """设置任务状态"""
        self.task_state = state
    
    def get_context(self, include_system: bool = False) -> List[Dict]:
        """获取上下文"""
        if include_system:
            return self.messages
        return [m for m in self.messages if m["role"] != "system"]
    
    def clear(self):
        """清空上下文"""
        self.messages.clear()
        self.entities.clear()
        self.intent_stack.clear()
        self.task_state = None
    
    def to_dict(self) -> Dict:
        return {
            "messages": self.messages,
            "entities": self.entities,
            "intent_stack": self.intent_stack,
            "task_state": self.task_state
        }


class MemoryGraph:
    """
    长期记忆 - 记忆图谱
    
    跨会话持久化记忆，支持按tag和key检索
    """
    
    def __init__(self, storage_path: str):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory: Dict[str, MemoryNode] = {}
        self._dirty: bool = False
        self._batching: bool = False  # True 时 remember() 只标脏不写盘
        self._load()
    
    def _load(self):
        """加载记忆"""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._memory = {
                        k: MemoryNode.from_dict(v) 
                        for k, v in data.items()
                    }
            except Exception as e:
                print(f"   ⚠️ 记忆加载失败: {e}")
                self._memory = {}
    
    def _save(self):
        """保存记忆"""
        try:
            data = {k: v.to_dict() for k, v in self._memory.items()}
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"   ⚠️ 记忆保存失败: {e}")
    
    def remember(
        self, 
        key: str, 
        value: Any, 
        tags: List[str] = None,
        confidence: float = 1.0
    ):
        """
        记忆：存储理解后的信息
        
        Args:
            key: 记忆键（用点号分隔，如 "user.child.age"）
            value: 记忆值
            tags: 标签列表（用于检索）
            confidence: 置信度（0-1）
        """
        tags = tags or []
        
        # 如果key已存在，更新
        if key in self._memory:
            node = self._memory[key]
            node.value = value
            node.confidence = confidence
            node.last_access = datetime.now().isoformat()
            node.access_count += 1
            # 合并tags
            node.tags = list(set(node.tags + tags))
        else:
            # 新建
            self._memory[key] = MemoryNode(
                key=key,
                value=value,
                tags=tags,
                confidence=confidence
            )
        
        self._dirty = True
        if not self._batching:
            self._save()
            self._dirty = False
        print(f"   💾 记忆: {key} = {value}")
    
    def recall(self, key: str) -> Optional[Any]:
        """回忆：按key获取。不更新 last_access — 该字段语义为最后写入时间。"""
        if key in self._memory:
            return self._memory[key].value
        return None
    
    def recall_by_tags(self, tags: List[str]) -> List[MemoryNode]:
        """回忆：按tag获取"""
        results = []
        for node in self._memory.values():
            if any(tag in node.tags for tag in tags):
                results.append(node)
        return results
    
    def begin_batch(self):
        """开始批量写模式，多次 remember() 只在 flush() 时写一次盘"""
        self._batching = True

    def flush(self):
        """提交批量写，写盘后退出批量模式"""
        self._batching = False
        if self._dirty:
            self._save()
            self._dirty = False

    def forget(self, key: str):
        """遗忘：删除记忆"""
        if key in self._memory:
            del self._memory[key]
            self._save()
    
    def update_confidence(self, key: str, delta: float):
        """更新置信度"""
        if key in self._memory:
            node = self._memory[key]
            node.confidence = max(0, min(1, node.confidence + delta))
            node.last_access = datetime.now().isoformat()
            self._save()
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有记忆"""
        return {k: v.value for k, v in self._memory.items()}
    
    def get_by_prefix(self, prefix: str) -> Dict[str, Any]:
        """获取指定前缀的所有记忆"""
        return {
            k: v.value for k, v in self._memory.items()
            if k.startswith(prefix)
        }
    
    def search(self, keyword: str) -> List[MemoryNode]:
        """搜索：简单关键词匹配"""
        results = []
        keyword = keyword.lower()
        for node in self._memory.values():
            key_match = keyword in node.key.lower()
            value_str = str(node.value).lower()
            value_match = keyword in value_str
            tag_match = any(keyword in tag.lower() for tag in node.tags)
            
            if key_match or value_match or tag_match:
                results.append(node)
        
        return results
    
    def clear(self):
        """清空所有记忆"""
        self._memory.clear()
        self._save()


class EpisodeLog:
    """
    会话 Episode 滚动日志

    设计原则：
    - 最多保留 MAX_EPISODES 条，超出时由主 Agent 异步压缩进画像
    - append() 返回 True 表示已超限，调用方负责触发压缩
    - 压缩时取出最旧的 COMPRESS_BATCH 条，由 LLM 提炼后写入 MemoryGraph
    """

    MAX_EPISODES = 10
    COMPRESS_BATCH = 5

    def __init__(self, storage_path: str):
        self.path = Path(storage_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, summary: str, tags: List[str] = None, hook: str = "") -> bool:
        """
        追加一条 episode。
        返回 True 表示超限，调用方应触发 compress。
        """
        record = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "summary": summary,
            "tags": tags or [],
            "hook": hook
        }
        episodes = self._load()
        episodes.append(record)
        self._save(episodes)
        return len(episodes) > self.MAX_EPISODES

    def get_recent(self, n: int = 3) -> str:
        """最近 n 条，格式化为可注入 context 的字符串"""
        episodes = self._load()[-n:]
        if not episodes:
            return ""
        lines = []
        for ep in episodes:
            hook = f"  → {ep['hook']}" if ep.get("hook") else ""
            lines.append(f"- {ep['date']}: {ep['summary']}{hook}")
        return "\n".join(lines)

    def pop_oldest_batch(self) -> List[Dict]:
        """取出最旧的 COMPRESS_BATCH 条（并从文件删除），供压缩使用"""
        episodes = self._load()
        batch = episodes[:self.COMPRESS_BATCH]
        self._save(episodes[self.COMPRESS_BATCH:])
        return batch

    def _load(self) -> List[Dict]:
        if not self.path.exists():
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except Exception:
            return []

    def _save(self, episodes: List[Dict]):
        with open(self.path, "w", encoding="utf-8") as f:
            for ep in episodes:
                f.write(json.dumps(ep, ensure_ascii=False) + "\n")


class UserMemory:
    """
    用户记忆 - 整合短期记忆、长期记忆、Episode 日志

    context 健康原则：
    - 注入时按 token 预算（budget_chars）截断，不全量注入
    - Episode 滚动窗口，超限自动压缩
    - 画像由 LLM 蒸馏更新，不无限堆积
    """

    def __init__(self, storage_path: str, max_turns: int = 20):
        self.context = ContextWindow(max_turns)
        self.graph = MemoryGraph(storage_path)
        # Episode 日志与 memory.json 同目录
        episodes_path = Path(storage_path).parent / "episodes.jsonl"
        self.episodes = EpisodeLog(str(episodes_path))
    
    # ===== 上下文操作 =====
    
    def add_message(self, role: str, content: str):
        """添加对话消息"""
        self.context.add(role, content)
    
    def add_entity(self, key: str, value: Any):
        """添加实体"""
        self.context.add_entity(key, value)
    
    def get_entity(self, key: str) -> Optional[Any]:
        """获取实体"""
        return self.context.get_entity(key)
    
    def get_conversation_context(self) -> List[Dict]:
        """获取对话上下文"""
        return self.context.get_context()
    
    def clear_context(self):
        """清空短期记忆"""
        self.context.clear()
    
    # ===== 长期记忆操作 =====
    
    def memorize(self, key: str, value: Any, tags: List[str] = None, confidence: float = 1.0):
        """记忆"""
        self.graph.remember(key, value, tags, confidence)

    def flush(self):
        """批量写提交（配合 graph.begin_batch() 使用）"""
        self.graph.flush()
    
    def recall(self, key: str) -> Optional[Any]:
        """回忆"""
        return self.graph.recall(key)
    
    def recall_by_tags(self, tags: List[str]) -> List[MemoryNode]:
        """按tag回忆"""
        return self.graph.recall_by_tags(tags)
    
    def forget(self, key: str):
        """遗忘"""
        self.graph.forget(key)
    
    def search_memory(self, keyword: str) -> List[MemoryNode]:
        """搜索记忆"""
        return self.graph.search(keyword)
    
    def get_all_memory(self) -> Dict[str, Any]:
        """获取所有记忆"""
        return self.graph.get_all()
    
    # ===== 快捷方法 =====
    
    def memorize_user(self, **kwargs):
        """记忆用户基本信息"""
        for key, value in kwargs.items():
            full_key = f"user.{key}"
            tags = ["user", "profile"]
            self.memorize(full_key, value, tags)
    
    def get_user_profile(self) -> Dict[str, Any]:
        """获取用户画像"""
        return self.graph.get_by_prefix("user.")
    
    def memorize_learning(self, **kwargs):
        """记忆学习相关信息"""
        for key, value in kwargs.items():
            full_key = f"learning.{key}"
            tags = ["learning", "education"]
            self.memorize(full_key, value, tags)
    
    def get_learning_context(self) -> Dict[str, Any]:
        """获取学习上下文"""
        return self.graph.get_by_prefix("learning.")
    
    def memorize_work(self, **kwargs):
        """记忆工作相关信息"""
        for key, value in kwargs.items():
            full_key = f"work.{key}"
            tags = ["work", "job"]
            self.memorize(full_key, value, tags)
    
    def get_work_context(self) -> Dict[str, Any]:
        """获取工作上下文"""
        return self.graph.get_by_prefix("work.")

    # ===== 预算制上下文构建 =====

    def build_context(self, intent: str = "", budget_chars: int = 900) -> str:
        """
        按字符预算构建记忆上下文（~900 chars ≈ 350 tokens）

        注入顺序（优先级递减）：
        1. 用户画像摘要（永远注入）
        2. 最近 3 条 episode（永远注入）
        3. 意图相关记忆（预算剩余时注入）

        不超过 budget_chars，超出截断。
        """
        parts = []
        remaining = budget_chars

        portrait = self._build_portrait_text()
        if portrait:
            chunk = f"【用户画像】\n{portrait}"
            parts.append(chunk[:remaining])
            remaining -= len(chunk)

        if remaining > 50:
            recent = self.episodes.get_recent(3)
            if recent:
                chunk = f"【近期记录】\n{recent}"
                parts.append(chunk[:remaining])
                remaining -= len(chunk)

        if intent and remaining > 80:
            relevant = self._get_relevant_memory(intent, max_chars=remaining)
            if relevant:
                parts.append(f"【相关记忆】\n{relevant}")

        if remaining > 60:
            notes = self.get_notes(3)
            if notes:
                chunk = "【记住的事】\n" + "\n".join(f"- {n}" for n in notes)
                parts.append(chunk[:remaining])

        return "\n\n".join(parts)

    def _build_portrait_text(self) -> str:
        """把 MemoryGraph 的 user.* / learning.* / work.* 格式化为紧凑文本
        超过 90 天未更新的条目加 (旧) 标注，供 LLM 判断权重。
        """
        from datetime import datetime, timedelta
        now = datetime.now()
        stale_days = 180  # 基于写入时间（last_access = 最后 remember() 时间）判断新鲜度

        # 系统内部 flag，不属于用户信息，不注入画像
        _SYSTEM_KEYS = {
            "user.preferred_spaces",
        }
        # 超过两段的 key（如 learning.known_subject.数学、learning.archived_wrong_questions）
        # 是系统跟踪 flag，过滤掉
        def _is_user_fact(key: str) -> bool:
            if key in _SYSTEM_KEYS:
                return False
            return key.count(".") == 1  # user.grade 保留，learning.known_subject.数学 过滤

        sections = []
        for prefix, label in [("user.", "用户"), ("learning.", "学习"), ("work.", "工作")]:
            nodes = {k: v for k, v in self.graph._memory.items()
                     if k.startswith(prefix) and _is_user_fact(k)}
            if not nodes:
                continue
            kv_parts = []
            for k, node in nodes.items():
                short_key = k.split(".")[-1]
                val = node.value
                try:
                    age = (now - datetime.fromisoformat(node.last_access)).days
                    suffix = "（旧）" if age > stale_days else ""
                except Exception:
                    suffix = ""
                kv_parts.append(f"{short_key}={val}{suffix}")
            sections.append(f"{label}: {', '.join(kv_parts)}")
        return "\n".join(sections)

    def _get_relevant_memory(self, intent: str, max_chars: int) -> str:
        """按意图关键词检索相关记忆，去重后截断到预算

        中文不用空格分词，改用 2-gram 字符切片提取候选关键词。
        """
        # 2-gram 滑窗：把"帮我找数学错题"切成["帮我","我找","找数","数学","学错","错题"]
        chars = [c for c in intent if '\u4e00' <= c <= '\u9fff']  # 只取汉字
        bigrams = {intent[i:i+2] for i in range(len(intent)-1) if len(intent[i:i+2]) == 2}
        # 也保留英文/数字 token
        ascii_tokens = {w for w in intent.split() if len(w) > 1}
        keywords = list(bigrams | ascii_tokens)[:6]

        seen, lines = set(), []
        for kw in keywords:
            for node in self.graph.search(kw)[:2]:
                line = f"{node.key.split('.')[-1]}: {node.value}"
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
        return "\n".join(lines)[:max_chars]

    # ===== Notes 接口（自然语言记忆条目）=====

    def memorize_note(self, note: str, tags: List[str] = None):
        """存储一条自然语言记忆（偏好/纠错/规则）"""
        import uuid
        key = f"notes.{uuid.uuid4().hex[:8]}"
        self.graph.remember(key, note, tags=tags or ["note"])

    def get_notes(self, max_notes: int = 5) -> List[str]:
        """获取最近的自然语言记忆条目（按 last_access 倒序）"""
        nodes = sorted(
            [n for k, n in self.graph._memory.items() if k.startswith("notes.")],
            key=lambda n: n.last_access,
            reverse=True
        )
        return [n.value for n in nodes[:max_notes]]
