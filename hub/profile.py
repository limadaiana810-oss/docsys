"""
UserProfileProvider - 动态用户画像提供者
从多个来源聚合用户偏好：memory > USER.md > defaults
配置驱动，无硬编码
"""

import re
import time
from pathlib import Path
from typing import Dict, Any, Optional

from .config import config


class UserProfileProvider:
    """
    动态用户画像提供者

    数据来源（优先级）：
    1. memory/ 最新日期文件（实时对话上下文）
    2. MEMORY.md（长期记忆）
    3. USER.md（基础信息）
    4. config.json defaults
    """

    def __init__(self, workspace_path: str = None):
        self.workspace = Path(workspace_path or config.get("paths.workspace"))
        self.cache_ttl = config.get("user_profile.cache_ttl", 300)
        # 实例变量缓存，避免不同 workspace 的实例互相污染
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0
    
    async def get_profile(self) -> Dict[str, Any]:
        """
        获取用户画像（动态读取）
        
        Returns:
            {
                "child_age": 7,
                "style_preference": "卡通",
                "user_personality": "正常",
                "lighting": "自然光",
                "character_type": "小动物"
            }
        """
        if self._cache and (time.time() - self._cache_time) < self.cache_ttl:
            return self._cache.copy()
        
        profile = config.get_user_defaults()
        
        # 1. 从 memory/ 目录读取最新记忆
        memory_profile = await self._load_latest_memory()
        if memory_profile:
            profile.update(memory_profile)
        
        # 2. 从 MEMORY.md 读取长期记忆
        longterm_profile = self._load_longterm_memory()
        if longterm_profile:
            profile.update(longterm_profile)
        
        # 3. 从 USER.md 读取基础信息
        user_profile = self._load_user_md()
        if user_profile:
            profile.update(user_profile)
        
        # 缓存
        self._cache = profile.copy()
        self._cache_time = time.time()
        
        return profile
    
    async def _load_latest_memory(self) -> Dict[str, Any]:
        """从 memory/ 目录读取最新的日记忆文件"""
        memory_dir = self.workspace / "memory"
        
        if not memory_dir.exists():
            return {}
        
        memory_files = list(memory_dir.glob("*.md"))
        if not memory_files:
            return {}
        
        latest = sorted(memory_files, key=lambda p: p.name, reverse=True)[0]
        
        try:
            content = latest.read_text()
            return self._parse_memory_content(content)
        except Exception as e:
            print(f"   ⚠️ 读取记忆文件失败: {e}")
            return {}
    
    def _load_longterm_memory(self) -> Dict[str, Any]:
        """从 MEMORY.md 读取长期记忆"""
        memory_md = self.workspace / "MEMORY.md"
        
        if not memory_md.exists():
            return {}
        
        try:
            content = memory_md.read_text()
            profile = {}
            
            age_match = re.search(
                r'(?:child.*?age|小孩.*?年龄|孩子.*?年龄)[:：]\s*(\d+)',
                content, re.IGNORECASE
            )
            if age_match:
                profile["child_age"] = int(age_match.group(1))
            
            style_match = re.search(
                r'(?:style.*?pref|风格.*?偏好|画风.*?喜欢)[:：]\s*([^\n]{2,20})',
                content, re.IGNORECASE
            )
            if style_match:
                profile["style_preference"] = style_match.group(1).strip()
            
            personality_match = re.search(
                r'(?:personality|性格)[:：]\s*([^\n]{2,30})',
                content, re.IGNORECASE
            )
            if personality_match:
                profile["user_personality"] = personality_match.group(1).strip()
            
            return profile
        except Exception as e:
            print(f"   ⚠️ 读取MEMORY.md失败: {e}")
            return {}
    
    def _load_user_md(self) -> Dict[str, Any]:
        """从 USER.md 读取基础信息"""
        user_md = self.workspace / "USER.md"
        
        if not user_md.exists():
            return {}
        
        try:
            content = user_md.read_text()
            profile = {}
            
            context_match = re.search(
                r'## Context\s*\n([\s\S]+?)(?:\n##|\Z)',
                content
            )
            if context_match:
                context = context_match.group(1)
                
                for keyword in ["child", "小孩", "孩子", "风格", "style", "偏好", "喜欢"]:
                    match = re.search(f'{keyword}[:：]\\s*([^\\n]{{2,30}})', context, re.IGNORECASE)
                    if match:
                        value = match.group(1).strip()
                        if "child" in keyword.lower() or "小孩" in keyword or "孩子" in keyword:
                            age = re.search(r'\d+', value)
                            if age:
                                profile["child_age"] = int(age.group())
                        elif "style" in keyword.lower() or "风格" in keyword:
                            profile["style_preference"] = value
                        elif "性格" in keyword or "personality" in keyword.lower():
                            profile["user_personality"] = value
            
            return profile
        except Exception as e:
            print(f"   ⚠️ 读取USER.md失败: {e}")
            return {}
    
    def _parse_memory_content(self, content: str) -> Dict[str, Any]:
        """解析记忆文件内容"""
        profile = {}
        
        patterns = {
            "child_age": r'(?:child.*?age|小孩.*?年龄|孩子.*?年龄|宝宝.*?年龄)[:：]\s*(\d+)',
            "style_preference": r'(?:style|风格|画风|喜欢.*?图)[:：]\s*([^\n]{2,20})',
            "user_personality": r'(?:personality|性格|用户.*?性格)[:：]\s*([^\n]{2,30})',
            "lighting": r'(?:光线|灯光|照明)[:：]\s*([^\n]{2,15})',
            "character_type": r'(?:角色|人物|动物)[:：]\s*([^\n]{2,20})',
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if key == "child_age":
                    num = re.search(r'\d+', value)
                    if num:
                        profile[key] = int(num.group())
                else:
                    profile[key] = value
        
        return profile
    
    def clear_cache(self):
        """清除缓存，强制重新加载"""
        self._cache = None
        self._cache_time = 0
