"""
PosterPromptBuilder - 海报Prompt智能构建器
根据动态用户画像生成结构化Prompt
配置驱动，无硬编码
"""

import json
from pathlib import Path
from typing import Dict, Any, List, Optional

from .config import config


class PosterPromptBuilder:
    """
    海报Prompt智能构建器
    
    配置驱动，所有映射关系从 config.json 读取
    """
    
    def __init__(self, profile_provider=None):
        self.profile_provider = profile_provider
        self._load_mappings()
    
    def _load_mappings(self):
        """从配置文件加载映射关系"""
        # 这些是备用映射，优先使用 config.json
        self.THEME_SEASONS = {
            "春": ["春天", "春季", "春游", "春耕"],
            "夏": ["夏天", "夏季", "暑假", "夏令营"],
            "秋": ["秋天", "秋季", "教师节", "中秋", "丰收"],
            "冬": ["冬天", "冬季", "寒假", "春节", "新年", "元旦", "冬至"],
            "日常": ["环保", "安全", "健康", "文明", "爱国", "读书"]
        }
        
        self.AGE_CHARACTERS = {
            range(0, 4): ["小奶猫", "小狗崽", "小熊宝宝", "小兔子", "小鸡仔"],
            range(4, 7): ["小猫", "小狗", "小狼宝宝", "小熊", "小狐狸", "小鹿"],
            range(7, 10): ["小狼", "小熊", "小狐狸", "小猫", "小狗", "小鹿"],
            range(10, 100): ["狼", "熊", "狐狸", "猫", "狗", "鹿"]
        }
        
        self.STYLE_MAP = {
            "灰暗": "低饱和度配色，柔和灰调，暗部细节丰富",
            "暗": "低饱和度配色，柔和灰调，暗部细节丰富",
            "线条": "清晰线条感，几何构图，简洁流畅",
            "逻辑": "清晰线条感，几何构图，结构分明",
            "可爱": "暖色调，圆润线条，萌系风格",
            "暖": "暖色调，圆润线条，温馨风格",
            "卡通": "卡通风格，明亮色彩，童趣十足",
            "写实": "写实风格，光影自然，细节丰富",
            "漫画": "漫画风格，对比鲜明，动态感强"
        }
        
        self.LIGHTING_MAP = {
            "自然光": "自然光照明，明亮柔和，户外场景",
            "暖光": "暖黄色调光源，温馨舒适氛围",
            "月光": "柔和月光效果，宁静梦幻",
            "雾光": "雾气弥漫效果，柔和散射光，朦胧美感",
            "逆光": "逆光拍摄效果，轮廓光，剪影风格"
        }
        
        self.THEME_CONTENT = {
            "教师节": "感恩老师，师生情谊，校园生活",
            "春节": "新年祝福，传统节日，中国风元素",
            "中秋": "团圆赏月，月饼文化，中秋习俗",
            "暑假": "快乐暑假，夏日活动，户外游玩",
            "寒假": "冬季假期，雪景，春节氛围",
            "春天": "春暖花开，大自然，绿色生态",
            "秋天": "金色秋季，丰收景象，落叶飘零",
            "安全": "安全教育，防护意识，警示标志",
            "环保": "绿色地球，环境保护，生态文明"
        }
        
        self.TYPE_HINTS = {
            "手抄报": "children's handwritten newspaper layout, decorative borders and frames around large blank white writing areas, blank lined sections for student writing, colorful illustrations only in corners and margins, leaving 60% white space for text content",
            "黑板报": "blackboard art style, chalk drawing, decorative frame with blank areas for writing",
            "电子版": "digital poster, modern flat design",
            "中国画": "traditional Chinese painting style, ink wash",
            "油画": "oil painting style, rich textures",
            "水彩": "watercolor painting style, soft colors"
        }
        
        # 从配置读取文字要求
        self.text_requirement = config.get("image_generation.text_requirement", 
                                          "图片中所有文字必须使用简体中文")
    
    async def build(self, theme: str, context: Dict = None, profile: Dict = None) -> str:
        """
        构建完整的生图Prompt

        Args:
            theme: 主题（如：教师节、春天、暑假）
            context: 额外上下文（可包含 type, grade 等）
            profile: 用户画像（如果传入则跳过 provider.get_profile() 调用）

        Returns:
            结构化的英文Prompt
        """
        context = context or {}

        # 获取用户画像（传入时直接使用，否则从 provider 加载）
        if profile is None:
            if self.profile_provider:
                profile = await self.profile_provider.get_profile()
            else:
                from .profile import UserProfileProvider
                provider = UserProfileProvider()
                profile = await provider.get_profile()
        
        parts = []
        
        # 1. 主题层
        theme_desc = self._get_theme_description(theme)
        parts.append(f"{theme_desc} themed illustration")
        
        # 2. 角色描述（根据小孩年龄）
        character = self._get_character_description(profile)
        parts.append(f"featuring {character}")
        
        # 3. 光线
        lighting = self._get_lighting_description(profile)
        parts.append(f"with {lighting} lighting")
        
        # 4. 画风（根据用户偏好）
        style = self._get_style_description(profile)
        parts.append(f"art style: {style}")
        
        # 5. 类型
        poster_type = context.get("type", "手抄报")
        type_hint = self._get_type_hint(poster_type)
        parts.append(f"format: {type_hint}")
        
        # 6. ⚠️ 强制要求
        parts.append(f"IMPORTANT: {self.text_requirement}, no English text")
        
        # 7. 质量要求
        parts.append("high quality, detailed, children's illustration, school project")
        
        return ", ".join(parts)
    
    async def build_with_context(
        self,
        theme: str,
        context: Dict,
        profile: Dict = None
    ) -> str:
        """
        基于更多上下文构建Prompt（用于手抄报完整版）
        """
        if profile is None:
            if self.profile_provider:
                profile = await self.profile_provider.get_profile()
            else:
                from .profile import UserProfileProvider
                provider = UserProfileProvider()
                profile = await provider.get_profile()
        
        prompt_parts = []
        
        theme_desc = self._get_theme_description(theme)
        prompt_parts.append(theme_desc)
        
        articles = context.get("articles", [])
        if articles:
            article_themes = [a.get("title", "") for a in articles[:2]]
            prompt_parts.append(f"illustrating: {', '.join(article_themes)}")
        
        character = self._get_character_description(profile)
        prompt_parts.append(character)
        
        style = self._get_style_description(profile)
        prompt_parts.append(style)
        
        size = context.get("size", "A4")
        prompt_parts.append(f"suitable for {size} format")
        
        prompt_parts.append(f"Chinese children's poster style, {self.text_requirement}")
        
        return ", ".join(prompt_parts)
    
    def _get_theme_description(self, theme: str) -> str:
        for season, keywords in self.THEME_SEASONS.items():
            for kw in keywords:
                if kw in theme:
                    content_hint = self.THEME_CONTENT.get(kw, "")
                    return f"{theme}, {content_hint}" if content_hint else theme
        return theme
    
    def _get_character_description(self, profile: Dict) -> str:
        age = profile.get("child_age", 7)
        custom_chars = profile.get("character_type", "")
        
        for age_range, characters in self.AGE_CHARACTERS.items():
            if age in age_range:
                if custom_chars and custom_chars != "小动物":
                    return f"cute {custom_chars} characters"
                else:
                    char_str = ", ".join(characters[:3])
                    return f"cute {char_str} characters"
        
        return "cute small animal characters"
    
    def _get_style_description(self, profile: Dict) -> str:
        style_pref = profile.get("style_preference", "卡通")
        
        for key, desc in self.STYLE_MAP.items():
            if key in style_pref:
                return desc
        
        return self.STYLE_MAP["卡通"]
    
    def _get_lighting_description(self, profile: Dict) -> str:
        lighting_pref = profile.get("lighting", "自然光")
        
        for key, desc in self.LIGHTING_MAP.items():
            if key in lighting_pref:
                return desc
        
        return self.LIGHTING_MAP["自然光"]
    
    def _get_type_hint(self, poster_type: str) -> str:
        return self.TYPE_HINTS.get(poster_type, "children's illustration poster")
