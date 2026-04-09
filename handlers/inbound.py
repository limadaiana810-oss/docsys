"""
InboundImageHandler - 入站图片批量处理器

职责：
1. 扫描 inbound/ 目录的新图片（按时间排序，取最近 N 张）
2. 并发批量分析 + 入库
3. 聚合结果一次性返回

改进点：
- 并发处理，不逐张阻塞
- 完整 OCR，不漏文字
- 合并结果，统一回复
"""

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from hub import config, get_llm, load_hub_storage, extract_json
from agents.archive import ArchiveAgent, IngestResult


# 支持的图片格式
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp"}


def scan_inbound_images(
    max_age_seconds: int = 300,
    max_count: int = 20,
    space: str = None,
) -> List[Path]:
    """
    扫描 raw/ inbox 目录，返回最近新图片列表。
    如果指定 space，只扫该空间的 raw/；否则扫所有空间。

    Args:
        max_age_seconds: 只取最近 N 秒内新增的图片（避免误取旧图）
        max_count: 最多返回 N 张
        space: "home" / "work"，为 None 时扫所有空间
    """
    raw_dirs = []
    if space:
        raw_path = config.get(f"spaces.{space}.raw")
        if raw_path:
            raw_dirs.append(Path(raw_path))
    else:
        for sp in ["home", "work"]:
            raw_path = config.get(f"spaces.{sp}.raw")
            if raw_path:
                raw_dirs.append(Path(raw_path))

    now = time.time()
    images = []

    for raw_dir in raw_dirs:
        if not raw_dir.exists():
            continue
        for p in raw_dir.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                if now - p.stat().st_mtime < max_age_seconds:
                    images.append(p)

    images.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return images[:max_count]


async def process_inbound_batch(
    max_age_seconds: int = 300,
    space_hint: str = None,
    user_profile: dict = None
) -> dict:
    """
    入站图片批量处理主入口。

    Returns:
        {
            "total": int,
            "success": int,
            "failed": int,
            "results": [IngestResult, ...],
            "by_space": {"home": [...], "work": [...]},
            "by_type": {"错题集": N, "试卷": N, ...}
        }
    """
    # 1. 扫描
    images = scan_inbound_images(max_age_seconds=max_age_seconds)
    if not images:
        return {
            "total": 0,
            "success": 0,
            "failed": 0,
            "results": [],
            "by_space": {},
            "by_type": {}
        }

    print(f"\n📦 InboundHandler: 发现 {len(images)} 张新图片，开始并发处理...")

    # 2. 并发批量入库
    agent = ArchiveAgent()
    results = await agent.batch_ingest(
        file_paths=[str(p) for p in images],
        space_hint=space_hint,
        context={"user_profile": user_profile or {}}
    )

    # 3. 聚合统计
    success_results = [r for r in results if r.success]
    failed_results = [r for r in results if not r.success]

    by_space = {"home": [], "work": []}
    by_type = {}

    for r in success_results:
        by_space.get(r.space, by_space.setdefault(r.space, [])).append(r)
        doc_type = r.doc_type or "其他"
        by_type[doc_type] = by_type.get(doc_type, 0) + 1

    # 4. 清理已处理的原图（可选：移动到 .processed/）
    # 不删除，只标记，避免用户投诉

    return {
        "total": len(results),
        "success": len(success_results),
        "failed": len(failed_results),
        "results": results,
        "by_space": by_space,
        "by_type": by_type,
        "images": [str(p) for p in images]  # 原始路径列表
    }


def format_inbound_report(batch_result: dict) -> str:
    """
    格式化批量入库报告。
    - 显示每个任务的执行状态
    - 完成后附上示例查询语句
    """
    if batch_result["total"] == 0:
        return "📭 没有发现新图片"

    total = batch_result["total"]
    success = batch_result["success"]
    failed = batch_result["failed"]
    results = batch_result.get("results", [])
    by_type = batch_result.get("by_type", {})
    by_space = batch_result.get("by_space", {})

    lines = []

    # ── 1. 并行执行状态（逐任务显示）────────────────────────────
    lines.append(f"🚀 开始并发处理 {total} 个任务...\n")
    for i, r in enumerate(results, 1):
        if r.success:
            doc_type = r.doc_type or "文档"
            lines.append(f"  任务 {i}: [{doc_type}] {r.caption[:40]}... ✅ 完成")
        else:
            lines.append(f"  任务 {i}: ❌ 失败 ({r.error})")

    # ── 2. 汇总统计 ─────────────────────────────────────────────
    lines.append(f"\n✅ 归档完成！成功 {success}/{total} 张")

    if by_type:
        type_lines = []
        for doc_type, count in by_type.items():
            type_lines.append(f"  • {doc_type}：{count}张")
        lines.append("📊 分类统计：\n" + "\n".join(type_lines))

    # ── 3. 归档位置 ────────────────────────────────────────────
    space_labels = {"home": "家庭空间", "work": "工作空间"}
    space_parts = []
    for space, space_results in by_space.items():
        if not space_results:
            continue
        label = space_labels.get(space, space)
        sub_spaces = {}
        for r in space_results:
            sub = r.sub_space or "documents"
            sub_spaces[sub] = sub_spaces.get(sub, 0) + 1
        sub_text = "、".join([f"{k}({v})" for k, v in sub_spaces.items()])
        space_parts.append(f"  📂 {label}：{sub_text}")

    if space_parts:
        lines.append("\n📍 归档位置：\n" + "\n".join(space_parts))

    # ── 4. 失败提醒 ────────────────────────────────────────────
    if failed > 0:
        lines.append(f"\n⚠️ 有 {failed} 张处理失败，请检查原图是否清晰")

    # ── 5. 示例查询语句 ────────────────────────────────────────
    search_examples = _build_search_examples(results, by_space)
    if search_examples:
        lines.append(f"\n💡 下次可以这样说：")
        for ex in search_examples:
            lines.append(f"   「{ex}」")

    return "\n".join(lines)


def _build_search_examples(results: list, by_space: dict) -> list:
    """
    根据归档结果生成示例查询语句。
    规则：
    - 有月份 → "X月的错题"
    - 有学科 → "数学/语文错题"
    - 有题型 → "计算题/应用题"
    - 无具体信息 → "上次那个"
    """
    examples = []
    from datetime import datetime
    month = datetime.now().strftime("%m")
    month_name = {"01":"1月","02":"2月","03":"3月","04":"4月",
                   "05":"5月","06":"6月","07":"7月","08":"8月",
                   "09":"9月","10":"10月","11":"11月","12":"12月"}
    current_month = month_name.get(month, month + "月")

    categories = set()
    doc_types = set()
    has_application = False   # 应用题
    has_calculation = False   # 计算题
    has_concept = False       # 概念/公式

    for r in results:
        if not r.success:
            continue
        caption = r.caption or ""
        keywords = r.keywords or []
        text = caption + " " + " ".join(keywords)

        if "数学" in text or "math" in text.lower():
            categories.add("数学")
        elif "语文" in text or "Chinese" in text.lower():
            categories.add("语文")
        elif "英语" in text or "English" in text.lower():
            categories.add("英语")

        if "错题" in text or "错" in caption:
            doc_types.add("错题")
        if "应用题" in text or "应用" in caption:
            has_application = True
        if "计算" in text or "口算" in text:
            has_calculation = True
        if "概念" in text or "公式" in text or "速查" in text:
            has_concept = True

    # 生成 1-2 个最相关的示例
    if categories and doc_types:
        for cat in list(categories)[:1]:
            for dtype in list(doc_types)[:1]:
                examples.append(f"找{current_month}的{cat}{dtype}")

    if has_application:
        examples.append("上次那个应用题")
    elif has_calculation:
        examples.append("上次那个计算题")
    elif has_concept:
        examples.append("速查速背的内容")

    if not examples:
        # fallback：按空间推荐
        if "home" in by_space:
            examples.append("找找今天的错题")
        elif "work" in by_space:
            examples.append("找本月的发票报销")

    return examples[:3]
