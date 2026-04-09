"""
PosterHandler - 手抄报/海报处理器
配置驱动，无硬编码
"""

import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

from hub import config, get_writing_llm, extract_json


class PosterHandler:
    def __init__(self, workspace_path: str = None):
        self.workspace = Path(workspace_path or config.get("paths.workspace"))
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            self._llm = get_writing_llm()
        return self._llm
    
    async def generate(
        self,
        context: Dict,
        mode: str = "template"
    ) -> Dict[str, Any]:
        """
        生成手抄报文案模板。配图由 ImageAgent 作为独立步骤处理。

        Args:
            context: {"params": {"theme": "教师节", "size": "A4", "style": "手抄报", "grade": "小学"}}
            mode: 保留参数，当前只做 template

        Returns:
            { success, template, metadata }
        """
        params = context.get("params", {})
        theme = params.get("theme", "")
        size = params.get("size", "A4")
        style = params.get("style", "手抄报")
        memory_context = context.get("memory_context", "")

        # 从 user_profile 推断年级，比硬编码更准确
        user_profile = context.get("user_profile", {})
        grade = params.get("grade") or user_profile.get("learning.grade") or user_profile.get("grade") or "小学"

        if not theme or len(theme.strip()) < 2:
            return {"success": False, "error": "主题不能为空"}

        template_result = await self._generate_template(theme, size, style, grade, memory_context=memory_context)

        return {
            "success": True,
            "template": template_result,
            "metadata": {"theme": theme, "size": size, "style": style, "grade": grade}
        }
    
    async def _generate_template(
        self,
        theme: str,
        size: str = "A4",
        style: str = "手抄报",
        grade: str = "小学",
        memory_context: str = ""
    ) -> Dict[str, Any]:
        """生成文本海报模板"""
        llm = self._get_llm()

        memory_section = f"\n\n【用户背景】\n{memory_context}" if memory_context else ""

        system = f"""你是一个专业的校园手抄报设计助手。请为"{theme}"主题设计一份完整的手抄报。{memory_section}

设计要求：
1. 尺寸：{size}
2. 风格：{style}
3. 目标人群：{grade}学生

手抄报结构要求：
1. **报头** - 大标题，要醒目
2. **主题文章** - 2-3篇与主题相关的小文章，每篇100-200字
3. **小栏目** - 1-2个小栏目（如：知识角、名人名言、安全提示等）
4. **插图描述** - 为每个插图位置提供详细的描述，用于 AI 生成配图

输出格式（JSON）：
{{
    "header": {{
        "title": "主标题",
        "subtitle": "副标题",
        "decorative": "装饰建议"
    }},
    "articles": [
        {{
            "title": "文章标题1",
            "content": "文章内容1，100-200字...",
            "length": "短/中/长"
        }}
    ],
    "columns": [
        {{
            "name": "栏目名称",
            "content": "栏目内容"
        }}
    ],
    "image_prompts": [
        {{
            "position": "报头装饰",
            "prompt": "详细的插图描述，用于AI生成"
        }}
    ],
    "design_tips": ["设计建议1", "设计建议2"],
    "print_guide": "打印指导"
}}

只输出JSON。"""
        
        prompt = f"请设计一份{theme}主题的手抄报："
        
        try:
            result = await llm.generate(prompt, system=system, max_tokens=4096)

            design = extract_json(result)
            if not design or not isinstance(design, dict):
                return {"error": f"LLM 返回格式异常: {result[:200]}"}

            # 格式化输出
            design["formatted"] = self._format_poster(design, theme, size, style)

            return design
        except Exception as e:
            print(f"   ⚠️ 模板生成失败: {e}")
            return {"error": f"生成失败: {str(e)}"}
    
    def _format_poster(
        self,
        design: Dict,
        theme: str,
        size: str,
        style: str
    ) -> str:
        """格式化手抄报文本"""
        lines = []
        
        lines.append("=" * 60)
        lines.append("📰 手抄报设计稿")
        lines.append("=" * 60)
        
        header = design.get("header", {})
        if header:
            lines.append(f"\n🏆 报头：{header.get('title', '')}")
            subtitle = header.get("subtitle", "")
            if subtitle:
                lines.append(f"   副标题：{subtitle}")
        
        articles = design.get("articles", [])
        if articles:
            lines.append(f"\n📝 主题文章（共{len(articles)}篇）：")
            for i, article in enumerate(articles, 1):
                lines.append(f"\n   【文章{i}】{article.get('title', '')}")
                content = article.get("content", "")
                if len(content) > 150:
                    content = content[:150] + "..."
                lines.append(f"   {content}")
        
        columns = design.get("columns", [])
        if columns:
            lines.append(f"\n📌 小栏目（共{len(columns)}个）：")
            for col in columns:
                lines.append(f"   • {col.get('name', '')}：{col.get('content', '')}")
        
        prompts = design.get("image_prompts", [])
        if prompts:
            lines.append(f"\n🖼️ 配图提示词（共{len(prompts)}张）：")
            for i, p in enumerate(prompts, 1):
                lines.append(f"   {i}. [{p.get('position', '')}]")
                lines.append(f"      {p.get('prompt', '')}")
        
        tips = design.get("design_tips", [])
        if tips:
            lines.append(f"\n💡 设计建议：")
            for tip in tips:
                lines.append(f"   → {tip}")
        
        lines.append("\n" + "=" * 60)
        lines.append(f"\n📋 信息汇总：")
        lines.append(f"   主题：{theme}")
        lines.append(f"   尺寸：{size}")
        lines.append(f"   风格：{style}")
        
        return "\n".join(lines)
    
