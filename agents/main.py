"""
DocSys 主Agent - 文档管理助手

职责：
1. 理解用户意图（跨轮次上下文）
2. 规划任务（拆解子任务）
3. 编排子Agent执行
4. 聚合结果返回用户
"""

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from hub import config, UserProfileProvider, UserMemory, load_hub_storage, SPACE_MAP


@dataclass
class Message:
    """对话消息"""
    role: str  # "user" / "assistant" / "system"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class StepResult:
    """步骤结果"""
    step: int
    agent: str
    success: bool
    data: Any = None
    error: str = None


@dataclass
class TaskContext:
    """
    子Agent接收的完整上下文（TaskContext）

    所有信息从ctx获取，子Agent无状态
    """
    task_id: str
    step: int
    params: Dict[str, Any]
    user_profile: Dict[str, Any] = field(default_factory=dict)
    space_hint: str = ""
    memory_context: str = ""  # 预算制记忆上下文字符串（用于子Agent个性化）


@dataclass
class AgentResult:
    """子Agent返回结果"""
    task_id: str
    step: int
    success: bool
    data: Any = None
    error: str = None


class DocSysMainAgent:
    """
    DocSys 主Agent - 文档管理助手
    
    核心原则：
    - 主Agent有记忆（上下文理解、任务规划）
    - 子Agent无状态（即入即出）
    """
    
    def __init__(self, user_id: str = None):
        self.user_id = user_id or "default"
        
        # ===== 短期记忆（任务级）=====
        self.conversation_history: List[Message] = []
        self.current_task: Optional[Dict] = None
        self.max_history = 20
        
        # ===== 长期记忆 =====  
        self.profile_provider = UserProfileProvider()
        self.user_profile: Dict[str, Any] = {}
        
        # ===== 记忆模块（分层记忆）=====
        memory_path = Path(config.get("paths.workspace", "/Users/kk/.openclaw/workspace")) / "memory.json"
        self.memory = UserMemory(str(memory_path))
        
        # ===== 子Agent实例（按需创建）=====
        self._agents = {}
        self._memory_context = ""  # 当前轮预算制记忆上下文
        self._pending_attachments: List[str] = []  # 当前轮文件附件

        # ===== 冷启动引导状态 =====
        # None = 未启动  Dict = 进行中  "done" = 已完成/跳过
        self._onboarding: Optional[Dict] = None

        # ===== 当前轮新学到的事实（用于 learning_signal 提示）=====
        self._new_facts_this_turn: Dict[str, str] = {}

        # ===== 待确认删除状态 =====
        # None = 无待删除  Dict = {"candidates": [...], "space": "family"/"work"}
        self._pending_delete: Optional[Dict] = None

        # ===== 会话状态 =====
        # True = 这是本 Agent 实例收到的第一条消息（尚未处理任何 handle() 调用）
        self._is_first_message: bool = True
        
        # ===== 高信号词：触发轻量 LLM 记忆提取 =====
        self._MEMORY_SIGNAL_WORDS = [
            # 纠错/确认类
            "不对", "不是", "应该", "每次", "以后", "习惯",
            "总是", "记住", "叫", "名字",
            # 偏好/强项类
            "擅长", "强项", "偏好", "最好", "更喜欢", "喜欢", "不喜欢",
            # 学习场景类
            "年级", "孩子", "小学", "初中", "高中", "同学",
            "学科", "错题", "试卷", "期中", "期末", "成绩", "分数",
            "薄弱", "强项",
            # 工作场景类
            "公司", "报销", "发票", "出差", "差旅", "职位",
            "同事", "领导", "客户", "合同", "预算",
        ]

        # ===== 意图路由表 =====
        # 注意：search_export / archive_export 不在此路由，
        # 而是在 _understand_intent 中由 search/archive + _needs_export() 组合判断
        self.INTENT_KEYWORDS = {
            "archive": ["归档", "入库", "上传", "上传图片", "发图片"],
            "search": ["搜索", "找", "查找", "有没有", "查一下", "帮我找", "想要", "给我", "拿到", "看看", "列出"],
            "export": ["导出", "生成文档", "整理成", "导出PDF", "导出Word", "生成PDF", "生成Word"],
            "poster": ["手抄报", "黑板报", "海报"],          # 完整手抄报：文案+配图
            "image": ["生成图片", "生图", "画一个", "配图"],  # 纯生图
            "delete": ["删除", "移除", "去掉", "清除"],
            "stats": ["多少", "几个", "统计", "汇总", "报告"],
            "memory_query": ["还记得", "记得吗", "你知道", "我说过"],
            "wake": ["小凯"],
        }
    
    # ==================== 公开接口 ====================
    
    async def handle(self, user_input: str, attachments: List[str] = None) -> str:
        """
        处理用户输入的主入口

        Args:
            user_input:   用户输入文本
            attachments:  文件路径列表（平台传入的附件，优先于文本提取）

        Returns:
            str: 回复用户的文本
        """
        self._pending_attachments = attachments or []

        # 记录是否为本实例的第一次调用，然后立即标记为已处理
        is_first = self._is_first_message
        self._is_first_message = False
        # 暂存供 _format_wake_result 使用（仅在本次 handle() 期间有效）
        self._current_is_first = is_first

        # 1. 添加用户消息到历史
        self.conversation_history.append(Message(
            role="user",
            content=user_input
        ))

        try:
            # 1. 自动提取并记忆用户信息
            self._auto_memorize(user_input)

            # 高信号词 → 触发轻量 LLM 记忆提取（fire-and-forget）
            if any(w in user_input for w in self._MEMORY_SIGNAL_WORDS):
                import asyncio as _aio
                _aio.create_task(self._extract_signal_memory(user_input))

            # 1.5 冷启动引导（首次使用，画像为空时）
            if self._onboarding != "done":
                if self._onboarding is None and self._is_cold_start():
                    self._onboarding = {"step": 0}
                if isinstance(self._onboarding, dict):
                    response = await self._handle_onboarding(user_input)
                    self.conversation_history.append(Message(role="assistant", content=response))
                    return response

            # 2. 理解用户意图
            intent = await self._understand_intent(user_input)

            # 3. 执行任务
            result = await self._execute_intent(intent)

            # 4. 格式化回复
            response = self._format_response(intent, result)

            # 5. 添加助手回复到历史
            self.conversation_history.append(Message(
                role="assistant",
                content=response
            ))

            # 6. 任务完成 → 先蒸馏（fire-and-forget），再清空短期记忆
            if self._is_task_complete(intent, result):
                snapshot = list(self.conversation_history)  # 拷贝，防止 cleanup 清空后读不到
                import asyncio
                # wake/stats/memory_query 不含新用户信息，跳过蒸馏避免 LLM 编造 facts
                _no_distill = {"wake", "memory_query", "unknown", "cancel_delete", "stats"}
                if intent["action"] not in _no_distill:
                    asyncio.create_task(self._distill_session(snapshot))
                # 保存本次任务信息，供下次启动时生成个性化问候
                if intent["action"] not in _no_distill:
                    self.memory.memorize("task.last_action", intent["action"], tags=["task"])
                    self.memory.memorize("task.last_time", datetime.now().isoformat(), tags=["task"])
                self._cleanup()

            return response

        except Exception as e:
            import traceback
            traceback.print_exc()
            error_msg = f"处理出错: {str(e)}"
            self.conversation_history.append(Message(
                role="assistant",
                content=error_msg
            ))
            return error_msg
    
    # ==================== 自动记忆 ====================
    
    def _auto_memorize(self, user_input: str):
        """
        自动从用户输入中提取并记忆关键信息

        批量写模式：所有 memorize() 调用结束后统一写一次磁盘。
        """
        import re
        self.memory.graph.begin_batch()
        try:
            self._auto_memorize_inner(user_input)
        finally:
            self.memory.flush()

    def _auto_memorize_inner(self, user_input: str):
        
        # ===== 用户基本信息 =====
        # 孩子年龄
        age_patterns = [
            r'孩子[是]?(\d+)岁',
            r'(\d+)岁',
            r'小孩[是]?(\d+)岁',
            r'儿子|女儿[是]?(\d+)岁'
        ]
        for pattern in age_patterns:
            match = re.search(pattern, user_input)
            if match:
                age = int(match.group(1))
                self.memory.memorize_user(child_age=age)
                break

        # 孩子名字
        name_patterns = [
            r'孩子叫(.{2,4})',
            r'小孩叫(.{2,4})',
            r'儿子叫(.{2,4})',
            r'女儿叫(.{2,4})',
            r'叫(.{2,4})的孩子',
            r'(.{2,4})同学的',
        ]
        for pattern in name_patterns:
            match = re.search(pattern, user_input)
            if match:
                name = match.group(1).strip()
                if 2 <= len(name) <= 4:
                    self.memory.memorize_user(child_name=name)
                break
        
        # 年级
        grade_patterns = [
            r'(\d+)年级',
            r'小学(\d+)年级',
            r'初中(\d+)年级'
        ]
        for pattern in grade_patterns:
            match = re.search(pattern, user_input)
            if match:
                grade = int(match.group(1))
                school_type = (
                    "初中" if ("初中" in user_input or "初" in user_input)
                    else "高中" if "高中" in user_input
                    else "小学" if ("小学" in user_input or grade <= 6)
                    else "未知"
                )
                self.memory.memorize_learning(grade=grade, school_type=school_type)
                break
        
        # ===== 学习相关 =====
        subjects = {
            "数学": ["数学", "口算", "方程", "几何"],
            "语文": ["语文", "作文", "阅读", "默写"],
            "英语": ["英语", "单词", "口语"],
            "物理": ["物理", "力学", "电学"],
            "化学": ["化学"]
        }
        for subject, keywords in subjects.items():
            if any(kw in user_input for kw in keywords):
                current_subjects = self.memory.recall("learning.current_subjects") or []
                if not isinstance(current_subjects, list):
                    current_subjects = [current_subjects] if current_subjects else []
                if subject not in current_subjects:
                    current_subjects.append(subject)
                    self.memory.memorize_learning(current_subjects=current_subjects)
        
        # 薄弱点/强项
        if "错题" in user_input:
            self.memory.memorize_learning(has_wrong_questions=True)
        
        # ===== 工作相关 =====
        company_patterns = [
            r'公司[是]?(.+?)[,，]|在(.+?)工作'
        ]
        for pattern in company_patterns:
            match = re.search(pattern, user_input)
            if match:
                company = match.group(1) or match.group(2)
                self.memory.memorize_work(company=company.strip())
                break
        
        position_patterns = [
            r'职位[是]?(.+?)',
            r'是(.+?)经理',
            r'是(.+?)产品'
        ]
        for pattern in position_patterns:
            match = re.search(pattern, user_input)
            if match:
                position = match.group(1).strip()
                self.memory.memorize_work(position=position)
                break
        
        # 报销类型
        expense_types = {
            "差旅": ["出差", "差旅", "机票", "火车票"],
            "餐饮": ["餐饮", "吃饭", "餐费"],
            "交通": ["交通", "打车", "油费"],
            "办公": ["办公", "文具", "设备"]
        }
        for exp_type, keywords in expense_types.items():
            if any(kw in user_input for kw in keywords):
                current_types = self.memory.recall("work.expense_types") or []
                if not isinstance(current_types, list):
                    current_types = [current_types] if current_types else []
                if exp_type not in current_types:
                    current_types.append(exp_type)
                    self.memory.memorize_work(expense_types=current_types)
        
        # ===== 文档偏好 =====
        if "PDF" in user_input or "pdf" in user_input:
            self.memory.memorize_user(preferred_format="pdf")
        elif "Word" in user_input or "docx" in user_input:
            self.memory.memorize_user(preferred_format="docx")

        # ===== 沟通风格 =====
        if any(kw in user_input for kw in ["简洁", "简短", "直接"]):
            self.memory.memorize_user(communication_style="简洁直接")
        elif any(kw in user_input for kw in ["详细", "完整"]):
            self.memory.memorize_user(communication_style="详细汇报")
    
    def get_memory_context(self) -> Dict[str, Any]:
        """获取记忆上下文（用于调试）"""
        return {
            "user_profile": self.memory.get_user_profile(),
            "learning_context": self.memory.get_learning_context(),
            "work_context": self.memory.get_work_context(),
            "all_memory": self.memory.get_all_memory()
        }

    # ==================== 冷启动引导 ====================
    # 设计原则：只问一件事（空间归属），其余全从使用中自动学习。
    # 每次有新发现时主动告知用户"我记住了"，让用户感受到系统在变聪明。

    def _is_cold_start(self) -> bool:
        """画像为空且从未完成引导（核心身份字段全空才触发，不依赖 preferred_format 等易得字段）"""
        if self.memory.recall("onboarding.done"):
            return False
        return not any([
            self.memory.recall("user.child_age"),
            self.memory.recall("user.child_name"),
            self.memory.recall("learning.grade"),
            self.memory.recall("learning.current_subjects"),
            self.memory.recall("work.expense_types"),
            self.memory.recall("work.company"),
        ])

    async def _handle_onboarding(self, user_input: str) -> str:
        """冷启动状态机（最多 3 步，自然对话式）

        step=0  介绍两个空间，不问问题
        step=1  根据用途问针对性的一个问题（家庭→孩子信息，工作→公司信息）
        step=2  确认学到的内容，完成引导
        任何时候检测到实际任务或附件 → 立即跳出，执行任务
        """
        step = self._onboarding.get("step", 0)

        # 有实际任务或图片附件 → 立即完成引导，执行任务
        has_real_intent = self._match_intent_keywords(user_input) not in ("unknown", "wake", "memory_query")
        if has_real_intent or self._pending_attachments:
            self._complete_onboarding()
            intent = await self._understand_intent(user_input)
            result = await self._execute_intent(intent)
            return self._format_response(intent, result)

        if step == 0:
            self._onboarding["step"] = 1
            return (
                "你好，我是小凯，你的文档管理助手。\n\n"
                "帮你管两类文件，互不干扰：\n"
                "  家庭空间 — 孩子的错题、试卷、学习资料\n"
                "  工作空间 — 发票、报销、合同文件\n\n"
                "两边各自独立，互不影响。\n\n"
                "直接发图片给我，我来判断放哪里。"
            )

        if step == 1:
            # _auto_memorize 已经处理了用户回复，解析用途并问一个自然问题
            use_hint = self._parse_usecase(user_input)
            self._onboarding["use_hint"] = use_hint
            self._onboarding["step"] = 2

            if use_hint == "family":
                return "家庭这边～孩子叫什么名字，上几年级了？"
            elif use_hint == "work":
                return "工作这边～在哪家公司呢？平时主要报销差旅、餐饮这类？"
            else:
                # 两边都有，先问家庭（更高频）
                return "家庭和工作都有，好的。孩子叫什么名字，上几年级了？"

        # step=2：_auto_memorize 已处理上一条回复，看看学到了什么，给个确认
        child_name = self.memory.recall("user.child_name")
        grade = self.memory.recall("learning.grade")
        school_type = self.memory.recall("learning.school_type") or ""
        subjects = self.memory.recall("learning.current_subjects") or []
        company = self.memory.recall("work.company")
        expense_types = self.memory.recall("work.expense_types") or []

        self._complete_onboarding()

        # 拼接确认语，只提已学到的信息
        learned = []
        if child_name:
            learned.append(child_name)
        if grade:
            learned.append(f"{school_type}{grade}年级")
        if isinstance(subjects, list) and subjects:
            learned.append("、".join(subjects[:3]))
        if company:
            learned.append(company)
        if isinstance(expense_types, list) and expense_types:
            learned.append("、".join(expense_types[:3]) + "报销")

        if learned:
            return f"好，{' / '.join(learned)}，记住了。\n以后直接发图片给我就行。"
        else:
            return "好，记住了。以后直接发图片给我就行。"

    def _complete_onboarding(self):
        """标记引导完成"""
        self.memory.memorize("onboarding.done", True, tags=["system"])
        self._onboarding = "done"

    def _parse_usecase(self, text: str) -> str:
        """解析用户选择：family / work / both"""
        if any(k in text for k in ["跳过", "算了", "直接", "不用"]):
            return "both"
        if any(k in text for k in ["3", "三", "都有", "都要", "两者", "全部"]):
            return "both"
        if any(k in text for k in ["1", "一", "家庭", "孩子", "学习", "错题", "试卷", "单词", "home"]):
            return "family"
        if any(k in text for k in ["2", "二", "工作", "发票", "报销", "合同", "公司", "work"]):
            return "work"
        if any(k in text for k in ["年级", "小学", "初中", "高中"]):
            return "family"
        return "both"

    def _learning_signal(self, new_facts: Dict[str, str]) -> str:
        """
        归档/搜索后，如果学到了新东西，生成一句「我记住了」的提示。
        new_facts: {"孩子年级": "初中2年级", "学科": "数学"} 之类
        只在首次发现时触发，不重复提示。
        """
        if not new_facts:
            return ""
        items = "、".join(f"{k}：{v}" for k, v in new_facts.items())
        return f"\n\n💡 我记住了 → {items}"
    
    # ==================== 意图理解 ====================
    
    async def _understand_intent(self, user_input: str) -> Dict[str, Any]:
        """
        理解用户意图
        
        Returns:
            {
                "action": "archive|search|export|image|search_export|...",
                "params": {...},
                "plan": [{"agent": "xxx", "params": {...}}],
                "need_clarification": bool,
                "clarification_options": [...]
            }
        """
        # 1. 快速路由匹配（提前，让 wake/stats 跳过昂贵的 build_context）
        action = self._match_intent_keywords(user_input)

        if action in ("wake", "stats"):
            # 不需要记忆上下文：wake 用 recall() 直接读，stats 只查 DB
            self._memory_context = ""
            self.user_profile = {}
        else:
            # get_profile（文件 I/O）与 build_context（内存计算）并发执行
            profile_task = asyncio.create_task(self.profile_provider.get_profile())
            self._memory_context = self.memory.build_context(
                intent=user_input,
                budget_chars=900  # ~350 tokens，硬上限
            )
            self.user_profile = await profile_task
            # 合并 memory.graph 中的结构化事实
            all_memory = self.memory.graph.get_all()
            self.user_profile.update({k: v for k, v in all_memory.items() if v is not None})

        # 1.5 LLM fallback：关键词匹配失败时用轻量 LLM 分类
        if action == "unknown":
            action = await self._llm_classify_intent(user_input)

        # 2. 提取参数
        params = self._extract_params(user_input, action)
        
        # 3. 判断是否需要组合（搜索+导出 / 上传+导出）
        if action in ["search", "archive"]:
            if self._needs_export(user_input):
                action = f"{action}_export"
                params["export_format"] = self._extract_export_format(user_input)
        
        # 4. 生成执行计划
        plan = self._generate_plan(action, params)
        
        # 5. 检查是否需要澄清
        need_clarification = self._check_need_clarification(action, params)
        clarification_options = []
        if need_clarification:
            clarification_options = self._generate_clarification_options(action, params)
        
        return {
            "action": action,
            "params": params,
            "plan": plan,
            "need_clarification": need_clarification,
            "clarification_options": clarification_options,
            "user_profile": self.user_profile
        }
    
    def _match_intent_keywords(self, user_input: str) -> str:
        """快速路由匹配（优先级：wake > confirm_delete > stats > delete > poster > image > search > archive > export）"""
        # 有待确认删除时，优先检测确认/取消
        if self._pending_delete:
            confirm_kws = ["确认删除", "确认", "是的", "全部删除", "都删"]
            cancel_kws = ["取消", "不删", "算了", "不要"]
            for kw in confirm_kws:
                if kw in user_input:
                    return "confirm_delete"
            for kw in cancel_kws:
                if kw in user_input:
                    return "cancel_delete"
            # 用户输入数字（选择某条）
            import re
            if re.match(r'^\s*\d+\s*$', user_input.strip()):
                return "confirm_delete"

        priority = ["wake", "stats", "delete", "memory_query", "poster", "image",
                    "search", "archive", "export"]
        for intent_name in priority:
            keywords = self.INTENT_KEYWORDS.get(intent_name, [])
            for kw in keywords:
                if kw in user_input:
                    return intent_name

        # 有附件且无法识别意图 → 附件即归档
        if self._pending_attachments:
            return "archive"

        return "unknown"
    
    def _extract_params(self, user_input: str, action: str) -> Dict[str, Any]:
        """提取参数"""
        params = {
            "raw_input": user_input,
            "space": self._extract_space(user_input)
        }
        
        # 提取格式（复用 _extract_export_format，避免重复逻辑）
        params["format"] = self._extract_export_format(user_input)
        
        # 提取查询内容（搜索类）
        if action in ["search", "search_export"]:
            params["query"] = self._extract_search_query(user_input)
        
        # 提取文件路径（归档类）
        if action in ["archive", "archive_export"]:
            file_path = self._extract_file_path(user_input)
            if file_path:
                params["file_path"] = file_path
            elif self._pending_attachments:
                params["file_path"] = self._pending_attachments[0]
                params["all_files"] = self._pending_attachments
            params["has_file"] = bool(params.get("file_path"))
        
        return params
    
    def _extract_space(self, user_input: str) -> str:
        """提取空间提示"""
        if "家庭" in user_input or "home" in user_input.lower():
            return "home"
        elif "办公" in user_input or "work" in user_input.lower() or "公司" in user_input:
            return "work"
        return ""  # 不明确，由Agent判断

    def _extract_file_path(self, user_input: str) -> Optional[str]:
        """从文本中提取文件路径（支持绝对路径和 ~ 路径）"""
        import re
        from pathlib import Path as _Path

        # 匹配绝对路径或 ~ 开头的路径，支持常见图片/文档扩展名
        pattern = r'([~/][^\s\'"，,。；;]+\.(?:jpg|jpeg|png|gif|webp|pdf|docx|doc|xlsx|xls|txt|heic|heif))'
        matches = re.findall(pattern, user_input, re.IGNORECASE)
        for m in matches:
            p = _Path(m).expanduser()
            if p.exists():
                return str(p)
        return None
    
    def _extract_search_query(self, user_input: str) -> str:
        """提取搜索查询：只删搜索意图词，保留其他内容（归档/删除等词可能是查询内容）"""
        query = user_input
        for kw in self.INTENT_KEYWORDS.get("search", []):
            query = query.replace(kw, "")
        return query.strip()
    
    def _extract_export_format(self, user_input: str) -> str:
        """提取导出格式"""
        u = user_input
        if "PDF" in u or "pdf" in u:
            return "pdf"
        elif "Word" in u or "word" in u or "docx" in u:
            return "docx"
        elif "ZIP" in u or "zip" in u:
            return "zip"
        return "docx"  # 默认
    
    def _needs_export(self, user_input: str) -> bool:
        """判断是否需要导出"""
        export_keywords = ["导出", "生成文档", "整理成", "PDF", "Word", "docx", "打印", "可打印", "输出成", "生成文件"]
        return any(kw in user_input for kw in export_keywords)

    def _is_multi_target(self, user_input: str) -> bool:
        """判断是否是多目标查询（需要生成多个文件）

        "和" 太泛，不单独用；需搭配文档类关键词才触发。
        明确多目标词（以及/还有/分别/各自/两个）可直接触发。
        """
        definite_kws = ["以及", "还有", "分别", "各自", "两个", "2个", "多个"]
        if any(kw in user_input for kw in definite_kws):
            return True
        # "和"/"与" 需搭配文档类名词（错题/发票/试卷等）才判为多目标
        if any(kw in user_input for kw in ["和", "与"]):
            doc_kws = ["错题", "试卷", "发票", "报销", "经典题", "速查", "合同", "账单"]
            matched = [kw for kw in doc_kws if kw in user_input]
            return len(matched) >= 2
        return False

    def _split_multi_targets(self, query: str) -> List[str]:
        """将多目标查询拆分为多个子查询（同时清理各部分的导出指令）"""
        import re
        # 先去掉尾部的导出/打印指令
        query = re.sub(r'[，,]?\s*(输出|导出|生成|整理成|打印|可打印)[^，,和与及]*$', '', query).strip()
        parts = re.split(r'[和与及、]|以及|还有', query)
        # 清理每个部分：去掉量词前缀和导出词
        cleaned = []
        export_words = {"导出", "输出", "打印", "生成", "整理", "可打印", "文件", "格式", "PDF", "Word", "个"}
        for p in parts:
            p = p.strip("，, ")
            # 去掉每部分末尾的数量/格式词
            p = re.sub(r'\s*(输出|导出|生成|整理成|可打印|文件|格式)[^\s]*\s*$', '', p).strip()
            if p:
                cleaned.append(p)
        return cleaned if len(cleaned) > 1 else [query]
    
    def _generate_plan(self, action: str, params: Dict) -> List[Dict]:
        """生成执行计划"""
        plan = []
        
        if action == "archive":
            all_files = params.get("all_files") or (
                [params["file_path"]] if params.get("file_path") else []
            )
            for i, fp in enumerate(all_files, 1):
                plan.append({
                    "step": i,
                    "agent": "archive",
                    "params": {
                        "file_path": fp,
                        "space_hint": params.get("space")
                    }
                })
        
        elif action == "search":
            plan.append({
                "step": 1,
                "agent": "search",
                "params": {
                    "query": params.get("query", ""),
                    "filters": {"space": params.get("space")} if params.get("space") else {}
                }
            })
        
        elif action == "export":
            plan.append({
                "step": 1,
                "agent": "export",
                "params": {
                    "data": params.get("data", {}),
                    "format": params.get("format", "docx")
                }
            })
        
        elif action == "poster":
            theme = params.get("query", params.get("raw_input", ""))
            # 文案生成与生图并发：两者都以 theme 为输入，无需等待对方
            plan.append({
                "step": 1,
                "agent": "poster",
                "params": {
                    "theme": theme,
                    "size": params.get("size", "A4"),
                    "style": params.get("style", "手抄报"),
                }
            })
            plan.append({
                "step": 2,
                "agent": "image",
                "params": {"theme": theme},
                # 移除 depends_on，与文案生成并发执行（省 3-5s）
            })

        elif action == "image":
            plan.append({
                "step": 1,
                "agent": "image",
                "params": {
                    "theme": params.get("query", params.get("raw_input", "")),
                    "context": params
                }
            })

        elif action == "delete":
            plan.append({
                "step": 1,
                "agent": "delete",
                "params": {
                    "query": params.get("query", params.get("raw_input", "")),
                    "space": params.get("space", ""),
                }
            })

        elif action == "confirm_delete":
            plan.append({
                "step": 1,
                "agent": "confirm_delete",
                "params": {
                    "raw_input": params.get("raw_input", ""),
                    "pending": self._pending_delete or {},
                }
            })

        elif action == "cancel_delete":
            pass  # 直接在 _execute_intent 里处理，无需 plan

        elif action == "stats":
            plan.append({
                "step": 1,
                "agent": "stats",
                "params": {
                    "space": params.get("space", ""),
                    "raw_input": params.get("raw_input", ""),
                }
            })

        elif action in ("wake", "memory_query", "unknown"):
            pass  # 由 _execute_intent 直接处理，无需 plan
        
        elif action == "search_export":
            raw_query = params.get("query", "")
            filters = {"space": params.get("space")} if params.get("space") else {}
            fmt = params.get("export_format", "docx")

            # 多目标：拆分成多组 search → export
            if self._is_multi_target(params.get("raw_input", "")):
                sub_queries = self._split_multi_targets(raw_query)
            else:
                sub_queries = [raw_query]

            step_num = 1
            for i, sq in enumerate(sub_queries):
                search_step = step_num
                plan.append({
                    "step": search_step,
                    "agent": "search",
                    "params": {"query": sq, "filters": filters}
                })
                step_num += 1
                plan.append({
                    "step": step_num,
                    "agent": "export",
                    "params": {"data": {}, "format": fmt, "label": sq},
                    "depends_on": [search_step]
                })
                step_num += 1
        
        elif action == "archive_export":
            all_files = params.get("all_files") or (
                [params["file_path"]] if params.get("file_path") else []
            )
            for i, fp in enumerate(all_files, 1):
                plan.append({
                    "step": i,
                    "agent": "archive",
                    "params": {
                        "file_path": fp,
                        "space_hint": params.get("space")
                    }
                })
            export_step = len(all_files) + 1
            plan.append({
                "step": export_step,
                "agent": "export",
                "params": {"data": {}, "format": params.get("export_format", "docx")},
                "depends_on": list(range(1, export_step))
            })
        
        return plan
    
    def _check_need_clarification(self, action: str, params: Dict) -> bool:
        """检查是否需要澄清"""
        # 归档：没有文件
        if action in ["archive", "archive_export"]:
            if not params.get("file_path"):
                return True
        # 搜索：无关键词
        if action in ["search", "search_export"]:
            query = params.get("query", "").strip()
            if len(query) < 2:
                return True
        return False
    
    def _generate_clarification_options(self, action: str, params: Dict) -> List[str]:
        """生成澄清选项"""
        space = params.get("space", "")

        # 归档：没有文件
        if action in ["archive", "archive_export"]:
            return ["请发送图片或文件，或在消息中包含文件路径（如 /Users/xxx/photo.jpg）"]

        options = []

        if action in ["search", "search_export"]:
            if space == "home" or not space:
                options.extend([
                    "找数学错题",
                    "找语文试卷",
                    "找经典题集"
                ])
            if space == "work" or not space:
                options.extend([
                    "找本月发票",
                    "找报销单据",
                    "找差旅票据"
                ])
        
        return options[:4]
    
    # ==================== 任务执行 ====================
    
    async def _execute_intent(self, intent: Dict) -> Dict[str, Any]:
        """执行意图"""
        action = intent["action"]
        plan = intent["plan"]
        
        if intent["need_clarification"]:
            return {
                "success": True,
                "need_clarification": True,
                "clarification_options": intent["clarification_options"]
            }
        
        # 无需子 Agent 的直接响应
        if action == "wake":
            # 检查是否有待处理的图片（多个附件 or inbound 目录有新图）
            from handlers.inbound import scan_inbound_images, process_inbound_batch
            inbound_imgs = scan_inbound_images(max_age_seconds=600)
            pending = self._pending_attachments or []

            if inbound_imgs or pending:
                # 有图片 → 批量处理（并发 OCR + 入库）
                batch_result = await process_inbound_batch(
                    max_age_seconds=600,
                    space_hint=intent["params"].get("space"),
                    user_profile=self.user_profile,
                )
                return {
                    "success": batch_result["success"] > 0,
                    "action": "wake_batch",
                    "batch": batch_result,
                    "images": [str(p) for p in inbound_imgs] + pending
                }
            return {"success": True, "action": "wake"}

        if action == "memory_query":
            raw = intent["params"].get("raw_input", "")
            answer = self._answer_memory_query(raw)
            return {"success": True, "action": "memory_query", "answer": answer}

        if action == "unknown":
            return {
                "success": False,
                "error": "我理解不了你的意思，请告诉我你想做什么（归档/搜索/导出/生图/生成手抄报）"
            }

        if action == "cancel_delete":
            self._pending_delete = None
            return {"success": True, "action": "cancel_delete"}
        
        # 计划为空但不是合法的无操作意图 → 报错而非静默成功
        _no_plan_actions = {"wake", "memory_query", "unknown", "cancel_delete"}
        if not plan and action not in _no_plan_actions:
            return {"success": False, "error": f"意图 '{action}' 未生成执行计划，请检查参数"}

        # 保存当前任务
        task_id = str(uuid.uuid4())[:8]
        self.current_task = {
            "task_id": task_id,
            "action": action,
            "plan": plan,
            "results": []
        }
        
        # 按依赖层级并发执行：无依赖关系的步骤并发，有依赖的等前置完成
        import asyncio
        results_map: Dict[int, StepResult] = {}
        remaining = list(plan)

        while remaining:
            # 找出所有前置依赖已完成的步骤
            ready = [
                s for s in remaining
                if not s.get("depends_on") or
                all(dep in results_map for dep in s["depends_on"])
            ]

            if not ready:
                # 依赖死锁，强制顺序执行剩余步骤
                ready = remaining[:1]

            # 并发执行同一层的步骤
            batch = await asyncio.gather(
                *[self._execute_step(s, results_map) for s in ready],
                return_exceptions=False
            )

            for step, result in zip(ready, batch):
                results_map[step["step"]] = result
                remaining.remove(step)

        results = [results_map[s["step"]] for s in plan]
        self.current_task["results"] = results

        # 判断是否全部成功
        all_success = all(r.success for r in results)

        return {
            "success": all_success,
            "task_id": task_id,
            "action": action,
            "steps": results
        }
    
    async def _execute_step(self, step: Dict, results_map: Dict[int, "StepResult"]) -> StepResult:
        """执行单个步骤"""
        agent_name = step["agent"]
        params = step["params"].copy()

        # 如果有前置依赖，填入前置结果；前置失败则中止本步骤
        if step.get("depends_on"):
            for dep in step["depends_on"]:
                prev = results_map.get(dep)
                if prev is None:
                    continue
                if not prev.success:
                    return StepResult(
                        step=step["step"],
                        agent=agent_name,
                        success=False,
                        error=f"前置步骤 {dep}({prev.agent}) 失败，已跳过：{prev.error}",
                    )
                if prev.agent == "search":
                    params["data"] = {
                        "results": prev.data.get("results", []) if prev.data else [],
                        "type": self._infer_data_type(prev.data)
                    }
                elif prev.agent == "archive":
                    # 多文件 archive_export：累积所有前置归档结果
                    existing = params.get("data", {}).get("results", [])
                    if prev.data:
                        existing = existing + [prev.data]
                    params["data"] = {"results": existing, "type": "general"}
                elif prev.agent == "poster":
                    # 把文案模板的结构信息传给 ImageAgent，用于生成更贴合的配图
                    template = (prev.data or {}).get("template") or {}
                    params["context"] = {
                        "type": params.get("style", "手抄报"),
                        "articles": template.get("articles", []),
                        "columns": template.get("columns", []),
                    }
        
        # 构建TaskContext
        ctx = TaskContext(
            task_id=self.current_task["task_id"],
            step=step["step"],
            params=params,
            user_profile=self.user_profile,
            space_hint=params.get("space_hint", ""),
            memory_context=self._memory_context
        )
        
        # 调用子Agent
        try:
            result = await self._call_agent(agent_name, ctx)
            
            return StepResult(
                step=step["step"],
                agent=agent_name,
                success=result.success if hasattr(result, 'success') else True,
                data=result.data if hasattr(result, 'data') else result,
                error=result.error if hasattr(result, 'error') else None
            )
        except Exception as e:
            return StepResult(
                step=step["step"],
                agent=agent_name,
                success=False,
                error=str(e)
            )
    
    async def _call_agent(self, agent_name: str, ctx: TaskContext) -> AgentResult:
        """调用子Agent"""
        # 延迟导入，避免循环
        if agent_name == "archive":
            if "archive" not in self._agents:
                from agents.archive import ArchiveAgent
                self._agents["archive"] = ArchiveAgent()
            agent = self._agents["archive"]
            result = await agent.ingest(
                ctx.params.get("file_path"),
                ctx.params.get("space_hint"),
                context={
                    "user_profile": ctx.user_profile,
                    "memory_context": ctx.memory_context,
                }
            )
            return AgentResult(
                task_id=ctx.task_id,
                step=ctx.step,
                success=result.success,
                data=result.__dict__,
                error=result.error
            )
        
        elif agent_name == "search":
            if "search" not in self._agents:
                from agents.search import SearchAgent
                self._agents["search"] = SearchAgent()
            agent = self._agents["search"]
            result = await agent.search(
                ctx.params.get("query", ""),
                ctx.params.get("filters", {}),
                limit=20,
                user_profile=ctx.user_profile,
            )
            # 将 SearchResult 数据类转为 dict，供 ExportAgent 使用
            results_as_dicts = [
                {
                    "storage_path": r.storage_path,
                    "caption": r.caption,
                    "keywords": r.keywords,
                    "score": r.score,
                    "space": r.space,
                    "sub_space": r.sub_space,
                    "match_type": r.match_type,
                    "record_id": r.record_id,
                }
                for r in result.results
            ]
            return AgentResult(
                task_id=ctx.task_id,
                step=ctx.step,
                success=result.success,
                data={
                    "results": results_as_dicts,
                    "total": result.total,
                    "query": result.query,
                    "need_clarification": result.need_clarification,
                    "clarification_options": result.clarification_options,
                },
                error=result.error
            )
        
        elif agent_name == "export":
            if "export" not in self._agents:
                from agents.export import ExportAgent
                self._agents["export"] = ExportAgent()
            agent = self._agents["export"]
            data = ctx.params.get("data", {})
            result = await agent.export(
                data.get("results", []),
                doc_type=data.get("type", "general"),
                format=ctx.params.get("format", "docx"),
            )
            return AgentResult(
                task_id=ctx.task_id,
                step=ctx.step,
                success=result.success,
                data=result.__dict__,
                error=result.error
            )
        
        elif agent_name == "image":
            if "image" not in self._agents:
                from agents.image import ImageAgent
                self._agents["image"] = ImageAgent()
            agent = self._agents["image"]
            result = await agent.generate(
                ctx.params.get("theme", ""),
                ctx.params.get("context", {})
            )
            return AgentResult(
                task_id=ctx.task_id,
                step=ctx.step,
                success=result.success,
                data=result.__dict__,
                error=result.error
            )

        elif agent_name == "poster":
            if "poster" not in self._agents:
                from handlers.poster import PosterHandler
                self._agents["poster"] = PosterHandler()
            handler = self._agents["poster"]
            result = await handler.generate(
                context={
                    "params": ctx.params,
                    "user_profile": ctx.user_profile,
                    "memory_context": ctx.memory_context,
                },
                mode="template"
            )
            return AgentResult(
                task_id=ctx.task_id,
                step=ctx.step,
                success=result.get("success", False),
                data=result,
                error=result.get("error")
            )

        elif agent_name == "stats":
            space_param = ctx.params.get("space", "")
            spaces_to_query = (
                [SPACE_MAP.get(space_param, "family")] if space_param else ["family", "work"]
            )
            stats = {}
            for sp in spaces_to_query:
                hub, _mod, _ = load_hub_storage(sp)
                records = hub.list(limit=9999)
                by_type: Dict[str, int] = {}
                for r in records:
                    dt = getattr(r, "doc_type", None) or "其他"
                    by_type[dt] = by_type.get(dt, 0) + 1
                stats[sp] = {"total": len(records), "by_type": by_type}
            return AgentResult(task_id=ctx.task_id, step=ctx.step, success=True,
                               data={"stats": stats})

        elif agent_name == "delete":
            import json as _j
            query = ctx.params.get("query", "")
            hub, _mod, hub_space = load_hub_storage(ctx.params.get("space", ""))
            records = hub.list(limit=9999)
            candidates = []
            for r in records:
                caption = getattr(r, "semantic_summary", "") or ""
                try:
                    tags = _j.loads(getattr(r, "tags", "[]") or "[]")
                except Exception:
                    tags = []
                if query and (query in caption or any(query in t for t in tags)):
                    candidates.append({
                        "record_id": r.record_id,
                        "caption": caption[:60],
                        "doc_type": getattr(r, "doc_type", ""),
                    })
            # 保存到 pending state，等用户确认
            if candidates:
                self._pending_delete = {"candidates": candidates, "space": hub_space}
            return AgentResult(task_id=ctx.task_id, step=ctx.step, success=True,
                               data={"candidates": candidates, "query": query, "space": hub_space})

        elif agent_name == "confirm_delete":
            import re as _re
            pending = ctx.params.get("pending", {})
            candidates = pending.get("candidates", [])
            space = pending.get("space", "family")
            raw_input = ctx.params.get("raw_input", "")

            if not candidates:
                self._pending_delete = None
                return AgentResult(task_id=ctx.task_id, step=ctx.step, success=False,
                                   error="没有待删除的记录")

            hub, _mod, _ = load_hub_storage(space)

            # 判断是删全部还是指定某条
            num_match = _re.match(r'^\s*(\d+)\s*$', raw_input.strip())
            if num_match:
                idx = int(num_match.group(1)) - 1
                to_delete = [candidates[idx]] if 0 <= idx < len(candidates) else []
            else:
                to_delete = candidates  # 确认全部删除

            deleted, failed = [], []
            for c in to_delete:
                try:
                    hub.delete(c["record_id"])
                    deleted.append(c)
                except Exception as e:
                    failed.append({"record": c, "error": str(e)})

            self._pending_delete = None
            return AgentResult(task_id=ctx.task_id, step=ctx.step, success=True,
                               data={"deleted": deleted, "failed": failed})

        else:
            return AgentResult(
                task_id=ctx.task_id,
                step=ctx.step,
                success=False,
                error=f"未知Agent: {agent_name}"
            )
    
    def _answer_memory_query(self, user_input: str) -> str:
        """从 UserMemory 中检索用户问题的答案"""
        profile = self.memory.get_user_profile()
        learning = self.memory.get_learning_context()
        work = self.memory.get_work_context()

        facts = []
        if profile:
            for k, v in profile.items():
                if v:
                    facts.append(f"{k}: {v}")
        if learning:
            for k, v in learning.items():
                if v:
                    facts.append(f"{k}: {v}")
        if work:
            for k, v in work.items():
                if v:
                    facts.append(f"{k}: {v}")

        if not facts:
            return "我还没有记录你的任何信息呢，聊多了我就记住了~"

        # 简单关键词匹配找最相关的条目
        relevant = [f for f in facts if any(kw in user_input for kw in f.split(": ")[0].split("."))]
        answer_facts = relevant or facts[:5]
        return "我记得：\n" + "\n".join(f"• {f}" for f in answer_facts)

    def _infer_data_type(self, data) -> str:
        """推断数据类型"""
        if not data:
            return "general"
        results = data.get("results", [])
        if results:
            first = results[0]
            caption = first.get("caption", "").lower()
            if any(kw in caption for kw in ["错题", "错误", "叉"]):
                return "wrong_questions"
            elif any(kw in caption for kw in ["发票", "收据", "报销"]):
                return "reimbursement"
        return "general"
    
    # ==================== 结果格式化 ====================
    
    async def _llm_classify_intent(self, user_input: str) -> str:
        """关键词匹配失败时的 LLM 轻量分类 fallback"""
        try:
            from hub import get_memory_llm
            llm = get_memory_llm()
            system = (
                "你是意图分类器。根据用户输入，只输出以下之一（一个英文词）：\n"
                "archive search search_export export delete stats poster image unknown\n\n"
                "archive=归档/上传文件  search=搜索/查找  search_export=搜索并整理导出\n"
                "export=导出文件  delete=删除  stats=统计  poster=手抄报/海报\n"
                "image=生成图片  unknown=无法判断\n\n"
                "只输出一个英文词，不要解释。"
            )
            result = await llm.generate(user_input, system=system, max_tokens=10)
            action = result.strip().split()[0].lower()
            valid = {"archive", "search", "search_export", "export",
                     "delete", "stats", "poster", "image"}
            return action if action in valid else "unknown"
        except Exception:
            return "unknown"

    async def _extract_signal_memory(self, user_input: str):
        """高信号词触发的轻量 LLM 记忆提取（fire-and-forget）

        设计原则：高信号词场景通常是用户在纠错或声明，可以覆盖已有值。
        但只写用户在本条输入里明确提到的字段，防止 LLM 顺带推断无关字段。
        facts 写入前做 key 白名单校验（只允许两段 key，如 user.child_name）。
        """
        # 允许写入的 key 集合（两段，防止 LLM 编造多级 key）
        _ALLOWED_PREFIXES = ("user.", "learning.", "work.")

        try:
            from hub import get_memory_llm, extract_json
            llm = get_memory_llm()
            system = (
                "从用户输入中提取用户明确说出的信息，输出JSON：\n"
                '{"facts": {"key": "value"}, "notes": ["自然语言描述"]}\n\n'
                "facts 的 key 用点号分隔（user.xxx / learning.xxx / work.xxx），只写两段。\n"
                "只提取用户在这条消息里明确陈述的内容，不要推断或补全其他字段。\n"
                "notes 存偏好、习惯、纠错等不适合结构化的内容。\n"
                "没有明确信息则输出 {\"facts\": {}, \"notes\": []}\n"
                "只输出JSON。"
            )
            result = await llm.generate(user_input, system=system, max_tokens=200)
            data = extract_json(result)
            if not data:
                return
            for key, value in (data.get("facts") or {}).items():
                # 白名单：只允许两段 key（user.xxx），拒绝 learning.known_subject.数学 等
                if (key and value is not None
                        and any(key.startswith(p) for p in _ALLOWED_PREFIXES)
                        and key.count(".") == 1):
                    self.memory.memorize(key, value, tags=["signal"])
            for note in (data.get("notes") or []):
                if note and len(note) > 4:
                    self.memory.memorize_note(note, tags=["signal", "preference"])
                    print(f"   🧠 记住了: {note}")
        except Exception as e:
            print(f"   ⚠️ 信号提取失败: {e}")

    def _get_hook(self) -> str:
        """从最近一条 episode 取出钩子，用于个性化问候"""
        try:
            recent = self.memory.episodes.get_recent(1)
            for line in recent.splitlines():
                if "→" in line:
                    return line.split("→")[-1].strip()
        except Exception:
            pass
        return ""

    def _get_session_greeting(self) -> str:
        """
        第一次 handle() 调用时生成动态问候语。
        - 有上次任务记录 → 引导继续
        - 无任何记忆 → 返回空字符串（走冷启动流程）
        """
        last_action = self.memory.recall("task.last_action")
        last_time = self.memory.recall("task.last_time")

        if not last_action:
            return ""

        # 计算距上次多久
        time_hint = ""
        if last_time:
            try:
                delta = datetime.now() - datetime.fromisoformat(last_time)
                days = delta.days
                if days == 0:
                    time_hint = "今天"
                elif days == 1:
                    time_hint = "昨天"
                elif days < 7:
                    time_hint = f"{days}天前"
                else:
                    time_hint = f"{days // 7}周前"
            except Exception:
                pass

        child_name = self.memory.recall("user.child_name")
        recent_topics = self.memory.recall("learning.recent_topics")
        topic_hint = f"（{recent_topics[0]}）" if isinstance(recent_topics, list) and recent_topics else ""

        action_labels = {
            "archive": f"归档了{child_name + '的' if child_name else ''}资料{topic_hint}",
            "search": "搜索了文档",
            "search_export": "整理了文档",
            "archive_export": f"归档并导出了{child_name + '的' if child_name else ''}文件",
            "poster": f"生成了{'手抄报' + topic_hint}",
            "image": "生成了配图",
            "delete": "整理了文件",
            "stats": "查看了统计",
        }
        label = action_labels.get(last_action, "用了一下")

        if time_hint:
            return f"{time_hint}{label}，今天有什么要整理的？"
        return f"上次{label}，今天有什么要整理的？"

    def _format_response(self, intent: Dict, result: Dict) -> str:
        """格式化回复（带记忆上下文的个性化版本）"""
        action = intent["action"]

        # 需要澄清
        if result.get("need_clarification"):
            options = result.get("clarification_options", [])
            options_text = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(options)])
            hook = self._get_hook()
            prefix = f"{hook}\n\n" if hook else ""
            return f"{prefix}请告诉我你想找什么？\n\n{options_text}"

        # 执行失败
        if not result.get("success"):
            errors = [s.error for s in result.get("steps", []) if s.error]
            return f"执行出错：{'；'.join(errors)}"
        
        # 成功 - 根据action格式化
        if action == "wake":
            if result.get("action") == "wake_batch":
                # 批量图片处理结果（不发单张结果，发汇总报告）
                from handlers.inbound import format_inbound_report
                return format_inbound_report(result.get("batch", {}))
            return self._format_wake_result()
        elif action == "memory_query":
            return result.get("answer", "我还没记住什么~")
        elif action == "archive":
            return self._format_archive_result(result)
        elif action == "search":
            return self._format_search_result(result)
        elif action == "search_export":
            return self._format_search_export_result(result)
        elif action == "archive_export":
            return self._format_archive_export_result(result)
        elif action == "image":
            return self._format_image_result(result)
        elif action == "poster":
            return self._format_poster_result(result)
        elif action == "export":
            return self._format_export_result(result)
        elif action == "stats":
            return self._format_stats_result(result)
        elif action == "delete":
            return self._format_delete_result(result)
        elif action == "confirm_delete":
            return self._format_confirm_delete_result(result)
        elif action == "cancel_delete":
            return "好的，已取消删除操作。"
        else:
            return "任务完成"
    
    def _format_archive_result(self, result: Dict) -> str:
        """归档结果格式化（附学习信号）"""
        steps = result.get("steps", [])
        if not (steps and steps[0].data):
            return "✅ 已归档完成！"
        data = steps[0].data
        sub_space_labels = {
            "wrong_questions": "错题集", "classic_questions": "经典题集",
            "quick_review": "速查手册", "documents": "文档",
            "reimbursement": "发票报销",
        }
        sub = sub_space_labels.get(data.get("sub_space", ""), data.get("sub_space", ""))
        caption = data.get("caption", "")[:40]
        child_name = self.memory.recall("user.child_name")
        name_part = f"{child_name}的" if child_name else ""
        msg = f"✅ {name_part}归档完成 — {caption}\n📂 {sub}"

        # 主动引用记忆：归档时说明用了什么背景
        memory_hints = []
        grade = self.memory.recall("learning.grade")
        school_type = self.memory.recall("learning.school_type")
        if grade and data.get("space") == "home":
            label = f"{school_type or ''}{grade}年级" if grade else ""
            memory_hints.append(f"已按{label}标记")
        # 首次归档某个 sub_space → 提醒用户
        sub_key = f"learning.archived_{data.get('sub_space', '')}"
        if not self.memory.recall(sub_key):
            self.memory.memorize(sub_key, True, tags=["system"])
            sub_labels = {
                "wrong_questions": "错题集", "classic_questions": "经典题集",
                "quick_review": "速查手册", "reimbursement": "发票报销",
            }
            sub_label = sub_labels.get(data.get("sub_space", ""), "")
            if sub_label:
                memory_hints.append(f"首次存入{sub_label}")
        if memory_hints:
            msg += f"（{'，'.join(memory_hints)}）"

        # recent_topics：把归档文档的关键词追加到最近知识点（FIFO，max 5）
        keywords = data.get("keywords", [])
        if keywords and data.get("space") == "home":
            topic_words = [k for k in keywords if isinstance(k, str) and len(k) > 1][:3]
            if topic_words:
                recent = self.memory.recall("learning.recent_topics") or []
                if not isinstance(recent, list):
                    recent = []
                for t in topic_words:
                    if t not in recent:
                        recent.insert(0, t)
                self.memory.memorize("learning.recent_topics", recent[:5], tags=["learning"])

        # 学习信号：发现新信息时告知用户
        new_facts = {}
        category = data.get("category", "")
        if category and not self.memory.recall(f"learning.known_subject.{category}"):
            new_facts["学科"] = category
            self.memory.memorize(f"learning.known_subject.{category}", True, tags=["learning"])
        difficulty = data.get("difficulty", "")
        if difficulty and sub == "错题集":
            new_facts["难度"] = difficulty

        return msg + self._learning_signal(new_facts)
    
    def _format_wake_result(self) -> str:
        """唤醒响应（带记忆上下文个性化）"""
        # 首次消息：优先使用上次任务记录生成个性化问候
        if getattr(self, "_current_is_first", False):
            session_line = self._get_session_greeting()
            if session_line:
                return session_line

        # fallback：基于 episode hook 或默认欢迎语
        hook = self._get_hook()
        return (hook + "\n\n" if hook else "") + "在的，有什么要整理的？"

    def _format_search_result(self, result: Dict) -> str:
        """搜索结果格式化（展示前5条摘要）"""
        steps = result.get("steps", [])
        if not steps:
            return "🔍 搜索完成"
        data = steps[0].data
        if not isinstance(data, dict):
            return "🔍 搜索完成"
        total = data.get("total", 0)
        if total == 0:
            return "🔍 没有找到相关内容，换个关键词试试？"

        # 主动说明用了哪些记忆背景做过滤
        filter_notes = []
        grade = self.memory.recall("learning.grade")
        school_type = self.memory.recall("learning.school_type")
        if grade and total > 0:
            filter_notes.append(f"{school_type or ''}{grade}年级的记录")
        subjects = self.memory.recall("learning.current_subjects")
        if subjects and isinstance(subjects, list) and total > 0:
            filter_notes.append(f"{subjects[0]}相关")
        filter_line = f"（基于{' / '.join(filter_notes)}）" if filter_notes else ""

        child_name = self.memory.recall("user.child_name")
        name_part = f"{child_name}的" if child_name else ""
        results = data.get("results", [])
        lines = [f"🔍 找到 {name_part}{total} 个结果：\n"]
        if filter_line:
            lines.append(filter_line)
        for i, r in enumerate(results[:5], 1):
            caption = r.get("caption", "")[:40]
            sub = r.get("sub_space", "")
            lines.append(f"  {i}. {caption}（{sub}）")
        if total > 5:
            lines.append(f"  …还有 {total - 5} 个，使用导出功能查看全部")
        return "\n".join(lines)

    def _format_poster_result(self, result: Dict) -> str:
        """手抄报结果格式化（step1=文案，step2=配图）"""
        steps = result.get("steps", [])
        lines = ["✅ 手抄报生成完成！\n"]

        for s in steps:
            if s.agent == "poster" and s.data:
                template = s.data.get("template") or {}
                header = template.get("header", {})
                title = header.get("title", "")
                if title:
                    lines.append(f"📝 文案：{title}")
                formatted = template.get("formatted", "")
                if formatted:
                    lines.append(formatted[:200] + ("…" if len(formatted) > 200 else ""))
            elif s.agent == "image" and s.data:
                local_path = s.data.get("local_path", "")
                if local_path:
                    lines.append(f"\n🖼️ 配图：{Path(local_path).name}")

        if len(lines) == 1:
            lines.append("请检查 workspace 目录查看生成结果")
        return "\n".join(lines)

    def _format_stats_result(self, result: Dict) -> str:
        """统计结果格式化"""
        steps = result.get("steps", [])
        if not steps:
            return "📊 统计完成"
        data = steps[0].data or {}
        stats = data.get("stats", {})
        if not stats:
            return "📊 暂无数据"
        child_name = self.memory.recall("user.child_name")
        lines = ["📊 文档统计：\n"]
        SPACE_LABEL = {"family": "家庭/学习", "work": "工作"}
        for sp, info in stats.items():
            label = SPACE_LABEL.get(sp, sp)
            name_part = f"（{child_name}）" if child_name and sp == "family" else ""
            lines.append(f"【{label}{name_part}】共 {info['total']} 份")
            for doc_type, cnt in info.get("by_type", {}).items():
                lines.append(f"  • {doc_type}：{cnt} 份")
        return "\n".join(lines)

    def _format_delete_result(self, result: Dict) -> str:
        """删除确认格式化（返回候选列表供用户确认）"""
        steps = result.get("steps", [])
        if not steps:
            return "🗑️ 删除操作完成"
        data = steps[0].data or {}
        candidates = data.get("candidates", [])
        query = data.get("query", "")
        child_name = self.memory.recall("user.child_name")
        name_part = f"{child_name}的" if child_name else ""
        if not candidates:
            return f"🔍 没有找到与「{query}」匹配的文件"
        lines = [f"⚠️ 找到 {name_part}{len(candidates)} 个匹配「{query}」的文件，请确认是否删除：\n"]
        for i, c in enumerate(candidates[:10], 1):
            lines.append(f"  {i}. {c['caption']}（{c['doc_type']}）")
        lines.append("\n回复「确认删除」或输入序号删除某一个，回复「取消」放弃")
        return "\n".join(lines)

    def _format_confirm_delete_result(self, result: Dict) -> str:
        """实际删除结果格式化"""
        steps = result.get("steps", [])
        if not steps:
            return "🗑️ 删除操作完成"
        data = steps[0].data or {}
        deleted = data.get("deleted", [])
        failed = data.get("failed", [])
        lines = []
        if deleted:
            lines.append(f"✅ 已删除 {len(deleted)} 个文件：")
            for c in deleted:
                lines.append(f"  • {c['caption']}（{c['doc_type']}）")
        if failed:
            lines.append(f"\n⚠️ 以下 {len(failed)} 个删除失败：")
            for f in failed:
                lines.append(f"  • {f['record']['caption']}：{f['error']}")
        return "\n".join(lines) if lines else "操作完成"

    def _format_search_export_result(self, result: Dict) -> str:
        """搜索+导出结果格式化（支持多文件）"""
        steps = result.get("steps", [])
        if not steps:
            return "✅ 导出完成！"

        # 收集所有导出结果
        export_files = []
        total_items = 0
        for s in steps:
            if s.agent == "search" and isinstance(s.data, dict):
                total_items += s.data.get("total", 0)
            if s.agent == "export" and isinstance(s.data, dict) and s.data.get("file_path"):
                export_files.append(s.data["file_path"])

        if not export_files:
            return "✅ 搜索完成，但导出失败"

        files_text = "\n".join([f"  📄 {Path(p).name}" for p in export_files])
        return f"✅ 导出完成！共生成 {len(export_files)} 个文件\n\n📊 共整理 {total_items} 项内容\n{files_text}"
    
    def _format_archive_export_result(self, result: Dict) -> str:
        """归档+导出结果格式化"""
        steps = result.get("steps", [])
        export_step = next((s for s in steps if s.agent == "export"), None)
        if export_step:
            export_data = export_step.data
            file_path = export_data.get("file_path", "") if isinstance(export_data, dict) else ""
            if file_path:
                filename = Path(file_path).name
                return f"✅ 已归档并导出！\n\n📄 文件：{filename}"
        return "✅ 已归档并导出！"

    def _format_export_result(self, result: Dict) -> str:
        """独立导出结果格式化"""
        steps = result.get("steps", [])
        if not steps:
            return "✅ 导出完成！"
        data = steps[0].data
        if not isinstance(data, dict):
            return "✅ 导出完成！"
        if not data.get("success", True):
            return f"⚠️ 导出失败：{data.get('error', '未知错误')}"
        file_path = data.get("file_path", "")
        files_count = data.get("files_count", 0)
        if file_path:
            return f"✅ 导出完成！\n\n📄 文件：{Path(file_path).name}（共 {files_count} 项）"
        return "✅ 导出完成！"

    def _format_image_result(self, result: Dict) -> str:
        """生图结果格式化"""
        steps = result.get("steps", [])
        if steps and steps[0].data:
            data = steps[0].data
            local_path = data.get("local_path", "")
            if local_path:
                return f"✅ 图片已生成！\n\n🖼️ 保存至：{local_path}"
        return "✅ 图片已生成！"
    
    # ==================== 辅助方法 ====================
    
    def _is_task_complete(self, intent: Dict, result: Dict) -> bool:
        """判断任务是否完成"""
        # 有clarification则未完成
        if result.get("need_clarification"):
            return False
        # 执行失败需要用户后续输入
        if not result.get("success"):
            return True  # 错误处理完毕，可清空
        # 成功则完成
        return True
    
    def _cleanup(self):
        """清空短期记忆"""
        self.conversation_history.clear()
        self.current_task = None

    # ==================== 记忆蒸馏（异步，不阻塞响应）====================

    async def _distill_session(self, conversation: List[Message]):
        """
        Memory Journalist：会话结束后异步提炼记忆。

        流程：
        1. 用 LLM 提问式提取本次会话的事实/情绪/钩子
        2. 更新 MemoryGraph（覆盖旧值，不堆积）
        3. 写入 EpisodeLog，超限时触发压缩
        """
        if len(conversation) < 2:
            return

        # 最多取最近 10 轮，避免 prompt 过长
        turns = []
        for msg in conversation[-10:]:
            role = "用户" if msg.role == "user" else "小凯"
            turns.append(f"{role}: {msg.content[:300]}")
        conv_text = "\n".join(turns)

        try:
            from hub import get_memory_llm
            llm = get_memory_llm()
        except Exception as e:
            print(f"   ⚠️ 蒸馏跳过（LLM不可用）: {e}")
            return

        # 快照当前已有的非空记忆 key，用于约束蒸馏不覆盖
        existing_keys = [k for k, v in self.memory.graph.get_all().items()
                         if v is not None and not k.startswith("task.") and not k.startswith("onboarding.")]
        existing_snapshot = ", ".join(existing_keys) if existing_keys else "（无）"

        system = f"""你是记忆整理助手。根据对话简洁提取信息，输出JSON：

{{
  "summary": "本次对话一句话摘要（20字内）",
  "tags": ["标签"],
  "facts": {{"user.child_age": 7}},
  "hook": "下次可主动提及的一句话（没有则留空）"
}}

facts 的 key 用点号分隔（user.xxx / learning.xxx / work.xxx）。
列表字段必须用JSON数组：learning.current_subjects=["数学","语文"]，work.expense_types=["差旅"]。
只提取有长期价值的信息，忽略闲聊。只输出JSON。

【重要规则】
1. 以下字段已有值，不要在 facts 中输出它们——除非用户在本次对话中用了"不对""应该是""其实是"等纠错词明确修正：
   已有字段：{existing_snapshot}
2. 只有字段当前为空时，才从对话推断并填写。
3. 列表字段（如 current_subjects）：只补充新增项，不要缩减已有项。"""

        try:
            result = await llm.generate(conv_text, system=system, max_tokens=256)

            m = re.search(r'\{[\s\S]*\}', result)
            if not m:
                return

            data = json.loads(m.group())

            # 更新事实记忆：空字段填充 / 显式纠错覆盖，已有值受 prompt 约束不会被改写
            for key, value in (data.get("facts") or {}).items():
                if key and value is not None:
                    self.memory.memorize(key, value, tags=data.get("tags", []))

            # 写入 Episode，超限则触发压缩
            needs_compress = self.memory.episodes.append(
                summary=data.get("summary", ""),
                tags=data.get("tags", []),
                hook=data.get("hook", "")
            )

            if needs_compress:
                await self._compress_episodes(llm)

            print(f"   🧠 蒸馏完成: {data.get('summary', '')}")

        except Exception as e:
            print(f"   ⚠️ 蒸馏失败: {e}")

    async def _compress_episodes(self, llm):
        """
        将最旧的 5 条 episode 压缩进用户画像，保持 EpisodeLog 不超限。

        关键：压缩后把结论写回 MemoryGraph（覆盖），老 episodes 物理删除。
        """
        batch = self.memory.episodes.pop_oldest_batch()
        if not batch:
            return

        batch_text = "\n".join(f"- {ep['date']}: {ep['summary']}" for ep in batch)
        current_portrait = self.memory._build_portrait_text()

        # 已有字段快照，压缩只补空、不覆盖
        existing_keys = set(k for k, v in self.memory.graph.get_all().items() if v is not None)

        system = """你是记忆压缩助手。把旧会话记录提炼为用户画像补充。

只输出当前画像中【还没有的字段】，JSON格式：
{"user.xxx": "值", "learning.xxx": "值"}

key 只写两段（user.xxx / learning.xxx / work.xxx）。
只保留规律性信息，丢弃偶发事件。只输出JSON。"""

        try:
            result = await llm.generate(
                f"【当前画像】\n{current_portrait}\n\n【旧记录】\n{batch_text}",
                system=system,
                max_tokens=200
            )
            m = re.search(r'\{[\s\S]*\}', result)
            if m:
                for key, value in json.loads(m.group()).items():
                    # 只写两段 key，且当前为空的字段
                    if (key and value is not None
                            and key.count(".") == 1
                            and key not in existing_keys):
                        self.memory.memorize(key, value, tags=["portrait", "compressed"])
            print(f"   🗜️ Episodes 压缩完成")
        except Exception as e:
            print(f"   ⚠️ 压缩失败: {e}")
