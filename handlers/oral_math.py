"""
OralMathHandler - 口算题生成器

根据用户画像（age/grade）生成适配年级的口算练习题，输出 .docx 文件。
不从归档取数据，纯生成。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from hub import config, get_writing_llm, extract_json


# 年级 → 口算题类型映射
GRADE_SPECS = {
    1: {
        "range": "20以内",
        "ops": ["加法", "减法"],
        "description": "一年级：20以内加减法，不退位",
    },
    2: {
        "range": "100以内",
        "ops": ["加法", "减法", "简单乘法"],
        "description": "二年级：100以内加减法，乘法口诀表内",
    },
    3: {
        "range": "1000以内",
        "ops": ["加法", "减法", "乘法", "除法"],
        "description": "三年级：千以内加减法，两位数乘一位数，简单除法",
    },
    4: {
        "range": "万以内",
        "ops": ["加法", "减法", "乘法", "除法", "混合运算"],
        "description": "四年级：万以内运算，含括号的混合运算",
    },
    5: {
        "range": "小数运算",
        "ops": ["小数加减", "小数乘除", "混合运算"],
        "description": "五年级：小数加减乘除，简单分数",
    },
    6: {
        "range": "分数运算",
        "ops": ["分数加减", "分数乘除", "百分数"],
        "description": "六年级：分数四则运算，百分数应用",
    },
}


class OralMathHandler:
    def __init__(self):
        self.outbound = Path(config.get("paths.outbound", "/Users/kk/.openclaw/media/outbound/"))
        self._llm = None

    def _get_llm(self):
        if self._llm is None:
            self._llm = get_writing_llm()
        return self._llm

    async def generate(
        self,
        user_profile: Dict = None,
        count: int = 40,
        difficulty: str = "适中",
    ) -> Dict[str, Any]:
        """
        生成口算练习题。

        Args:
            user_profile: 用户画像 {"child_age": 8, "learning.grade": "2"}
            count: 题目数量（默认40题，一页A4）
            difficulty: "简单" / "适中" / "挑战"

        Returns:
            { success, file_path, grade, count, preview }
        """
        user_profile = user_profile or {}

        # 推断年级
        grade = self._resolve_grade(user_profile)
        spec = GRADE_SPECS.get(grade, GRADE_SPECS[2])

        # LLM 生成题目
        problems = await self._generate_problems(spec, count, difficulty, grade)
        if not problems:
            return {"success": False, "error": "题目生成失败"}

        # 输出 docx
        file_path = await self._export_docx(problems, grade, spec)

        return {
            "success": True,
            "file_path": str(file_path),
            "grade": grade,
            "count": len(problems),
            "difficulty": difficulty,
            "preview": problems[:5],
        }

    def _resolve_grade(self, profile: Dict) -> int:
        """从 user_profile 推断年级（整数 1-6）"""
        # 优先用 grade
        grade_str = (
            profile.get("grade")
            or profile.get("learning.grade")
            or ""
        )
        if grade_str:
            import re
            m = re.search(r"(\d+)", str(grade_str))
            if m:
                return min(max(int(m.group(1)), 1), 6)

        # 用 age 推算
        age = profile.get("child_age") or profile.get("user.child_age")
        if age:
            age = int(age)
            return min(max(age - 6, 1), 6)  # 7岁=1年级

        return 2  # 默认2年级

    async def _generate_problems(
        self,
        spec: Dict,
        count: int,
        difficulty: str,
        grade: int,
    ) -> list:
        """LLM 生成口算题列表"""
        llm = self._get_llm()

        system = f"""你是一个小学数学口算题出题专家。

年级：{grade}年级
难度范围：{spec['description']}
运算类型：{', '.join(spec['ops'])}
难度要求：{difficulty}

生成规则：
1. 每道题只有一个算式和一个答案
2. 答案必须是整数或简单小数/分数（视年级而定）
3. 不出现负数结果
4. 题目难度要均匀分布
5. 避免重复题目

输出 JSON 数组，每个元素：
{{"q": "算式", "a": "答案"}}

示例（2年级）：
[{{"q": "36 + 47 =", "a": "83"}}, {{"q": "5 × 8 =", "a": "40"}}]

只输出 JSON 数组，不要其他文字。生成 {count} 道题。"""

        try:
            result = await llm.generate(
                f"请生成{count}道{grade}年级口算题",
                system=system,
                max_tokens=4096,
                temperature=0.8,
            )
            data = extract_json(result)
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception as e:
            print(f"   ⚠️ 口算题生成失败: {e}")

        return []

    async def _export_docx(self, problems: list, grade: int, spec: Dict) -> Path:
        """将题目导出为 Markdown 文件（后续可转 docx/pdf）"""
        self.outbound.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = self.outbound / f"口算题_{grade}年级_{ts}.md"

        lines = [
            f"# 口算练习 — {grade}年级",
            f"",
            f"**日期**：{datetime.now().strftime('%Y年%m月%d日')}",
            f"**范围**：{spec['description']}",
            f"**题数**：{len(problems)} 题",
            f"",
            "---",
            "",
            f"**姓名**：____________  **用时**：____________  **得分**：____/{len(problems)}",
            "",
        ]

        # 排版：每行4题
        cols = 4
        for i in range(0, len(problems), cols):
            row = problems[i : i + cols]
            cells = []
            for j, p in enumerate(row):
                q = p.get("q", "") if isinstance(p, dict) else str(p)
                num = i + j + 1
                cells.append(f"({num}) {q}____")
            lines.append("  ".join(cells))
            lines.append("")

        # 答案区
        lines.extend([
            "---",
            "",
            "<details><summary>参考答案（点击展开）</summary>",
            "",
        ])
        ans_parts = []
        for i, p in enumerate(problems, 1):
            a = p.get("a", "?") if isinstance(p, dict) else "?"
            ans_parts.append(f"({i}){a}")
        # 每行10个答案
        for k in range(0, len(ans_parts), 10):
            lines.append("  ".join(ans_parts[k : k + 10]))
        lines.extend(["", "</details>", ""])

        file_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"   ✅ 口算题导出: {file_path}")
        return file_path
