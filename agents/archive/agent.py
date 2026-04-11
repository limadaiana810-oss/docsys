"""
ArchiveAgent - 图片入库Agent
"""

import asyncio
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from hub import (
    config, get_multimodal_llm, get_embedding_service,
    save_record_vector, load_hub_storage, extract_json, SPACE_MAP,
)


@dataclass
class IngestResult:
    """入库结果"""
    success: bool
    record_id: Optional[str] = None
    caption: Optional[str] = None
    keywords: Optional[List[str]] = None
    space: str = "home"
    sub_space: str = "documents"
    storage_path: Optional[str] = None
    file_name: Optional[str] = None
    doc_type: Optional[str] = None
    category: Optional[str] = None
    extracted_text: Optional[str] = None
    difficulty: Optional[str] = None
    error: Optional[str] = None


class ArchiveAgent:
    """
    图片入库Agent

    执行 Image Ingest Pipeline：
    1. describe - 多模态模型生成描述（OCR + 分类）
    2. link - 写入存储
    """

    def __init__(self, workspace_path: str = None):
        self.workspace = Path(workspace_path or config.get("paths.workspace"))
        self.media_root = Path(config.get("paths.media", "/Users/kk/.openclaw/media/"))
        self._llm = None
        self._embedding_svc = None

    def _get_llm(self):
        if self._llm is None:
            self._llm = get_multimodal_llm()
        return self._llm

    def _get_embedding_service(self):
        if self._embedding_svc is None:
            self._embedding_svc = get_embedding_service()
        return self._embedding_svc

    async def ingest(
        self,
        file_path: str,
        space_hint: str = None,
        context: Dict = None
    ) -> IngestResult:
        """
        单个图片入库
        """
        context = context or {}

        try:
            file_path = Path(file_path)
            if not file_path.exists():
                return IngestResult(success=False, error=f"文件不存在: {file_path}")

            # 并发分析
            analysis = await self._analyze_once(file_path, space_hint, context.get("user_profile", {}))
            caption = analysis["caption"]
            keywords = analysis["keywords"]

            space = space_hint if space_hint else analysis["space"]
            sub_space = analysis["sub_space"]

            if space_hint and analysis["space"] != space_hint:
                space = space_hint
                sub_space = self._infer_sub_space(caption, space_hint)

            # 文件保存（本地 I/O）与向量生成（网络）并发
            record_id = str(uuid.uuid4())
            storage_path, vector = await asyncio.gather(
                self._save_to_space(file_path, space, sub_space),
                self._generate_vector(analysis),
            )
            # SQLite 写入放线程池，不阻塞 event loop
            await asyncio.to_thread(
                self._write_to_hub,
                record_id, space, sub_space, str(storage_path),
                caption, keywords, analysis, vector
            )

            print(f"   ✅ ArchiveAgent: {caption[:50]}... → {space}/{sub_space}")

            return IngestResult(
                success=True,
                record_id=record_id,
                caption=caption,
                keywords=keywords,
                space=space,
                sub_space=sub_space,
                storage_path=str(storage_path),
                file_name=file_path.name,
                doc_type=analysis.get("doc_type", ""),
                category=analysis.get("category", ""),
                extracted_text=analysis.get("extracted_text", ""),
                difficulty=analysis.get("difficulty", ""),
            )

        except Exception as e:
            return IngestResult(success=False, error=str(e))

    async def batch_ingest(
        self,
        file_paths: List[str],
        space_hint: str = None,
        context: Dict = None
    ) -> List[IngestResult]:
        """
        批量并发入库（核心改进）

        - 所有图片同时分析（OCR + 分类）
        - 结果按置信度排序
        - 返回汇总报告
        """
        context = context or {}

        # 过滤存在的文件
        valid_paths = [str(p) for p in file_paths if Path(p).exists()]
        if not valid_paths:
            return [IngestResult(success=False, error="没有找到有效文件")]

        print(f"   📦 BatchIngest: 批量处理 {len(valid_paths)} 个文件（并发）...")

        # 并发执行所有文件的分析
        tasks = [
            self.ingest(fp, space_hint, context)
            for fp in valid_paths
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理异常结果
        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed.append(IngestResult(
                    success=False,
                    file_name=Path(valid_paths[i]).name,
                    error=str(result)
                ))
            else:
                processed.append(result)

        # 汇总
        success_count = sum(1 for r in processed if r.success)
        print(f"   📦 BatchIngest完成: {success_count}/{len(valid_paths)} 成功")

        return processed

    async def _analyze_once(self, file_path: Path, space_hint: str = None, user_profile: Dict = None) -> dict:
        """
        单次多模态调用，同时完成：描述、分类、关键词、OCR文字提取、难度判断。

        改进点（v2）：
        - 明确要求完整 OCR 文字（不是摘要）
        - 强化知识点/错因提取
        - 增加置信度字段
        """
        llm = self._get_llm()
        user_profile = user_profile or {}

        space_constraint = ""
        if space_hint:
            space_constraint = f'\n⚠️ 用户已明确选择「{space_hint}」，space 字段必须填写「{space_hint}」。'

        profile_hint = ""
        grade = user_profile.get("grade") or user_profile.get("learning.grade")
        child_age = user_profile.get("child_age") or user_profile.get("user.child_age")
        subjects = user_profile.get("current_subjects") or user_profile.get("learning.current_subjects")
        if grade:
            profile_hint += f"\n用户背景：孩子{grade}年级。"
        if child_age:
            profile_hint += f" 约{child_age}岁。"
        if subjects:
            profile_hint += f" 常见学科：{subjects}。"
        if profile_hint:
            profile_hint = f'\n【用户背景参考】{profile_hint.strip()}'

        # 改进后的 prompt（强化 OCR 和知识点提取）
        system = f"""你是一个图像分析助手，擅长从图片中提取完整信息。

请对图片进行综合分析，一次性返回以下全部字段（必须是有效JSON）：

1. **caption**：50-80字的内容描述，包含完整学科/题型/主题
2. **space**：归属空间，只能是 "home" 或 "work"
3. **sub_space**：子空间（home下：wrong_questions/classic_questions/quick_review/documents；work下：reimbursement/documents）
4. **doc_type**：错题集/试卷/经典题集/速查手册/打印文档/照片/发票报销/其他
5. **category**：学科（语文/数学/英语/物理/化学/生物/历史/地理/道德与法治/音乐/美术/体育/信息技术/其他），无则填 ""
6. **keywords**：5-10个搜索关键词（JSON数组），含学科、题型、知识点、错误原因等
7. **extracted_text**：图片中所有可见文字的完整OCR结果（不是摘要，是原文照录），包括题目、选项、答案、批注等。无则填 ""
8. **difficulty**：难度（基础/中等/困难）或空
9. **confidence**：你对这次分析的置信度（0.0-1.0），纯视觉判断时填 0.5，明显看不清时填 0.3{space_constraint}{profile_hint}

【OCR要求】：
- 必须完整照录图片中所有可见文字
- 包括手写批注、红笔标注（用括号标注，如"[红笔：错]"）
- 题目、选项、答案、解析都要录
- 字迹潦草时根据上下文合理推断

【错题判断】：
- 有红笔叉号/扣分/红笔批注 → wrong_questions
  扣分标记包括：-1、-2、-3、-0.5、扣X分、H1、H2等
  批改符号包括：×、✗、叉号、圈出错误、下划线标注
- 全是勾/标准答案/纸面干净 → classic_questions
- 公式/概念/知识点速查 → quick_review

只输出JSON，不要任何其他文字：
{{"caption": "...", "space": "...", "sub_space": "...", "doc_type": "...", "category": "...", "keywords": [], "extracted_text": "...", "difficulty": "", "confidence": 0.9}}"""

        try:
            result = await llm.generate_multimodal(
                "请综合分析这张图片",
                str(file_path),
                system=system,
                max_tokens=1024  # 增大，保证 OCR 文字完整
            )

            data = extract_json(result)
            if data:
                return {
                    "caption": data.get("caption", "未知内容"),
                    "space": data.get("space", "home"),
                    "sub_space": data.get("sub_space", "documents"),
                    "doc_type": data.get("doc_type", ""),
                    "category": data.get("category", ""),
                    "keywords": data.get("keywords", []),
                    "extracted_text": data.get("extracted_text", ""),
                    "difficulty": data.get("difficulty", ""),
                    "confidence": data.get("confidence", 0.5),
                }
        except Exception as e:
            print(f"   ⚠️ _analyze_once 失败，降级处理: {e}")

        return {
            "caption": "未知内容",
            "space": space_hint or "home",
            "sub_space": "documents",
            "doc_type": "",
            "category": "",
            "keywords": [],
            "extracted_text": "",
            "difficulty": "",
            "confidence": 0.3,
        }

    async def _generate_vector(self, analysis: dict) -> list:
        """
        将多模态模型输出的文本（caption + OCR + 标签）编码为语义向量。
        这是"多模态向量"：图片内容经 Qwen 理解后的语义 embedding。
        """
        text = " ".join(filter(None, [
            analysis.get("caption", ""),
            analysis.get("extracted_text", ""),
            analysis.get("doc_type", ""),
            analysis.get("category", ""),
            " ".join(analysis.get("keywords", [])),
        ]))
        try:
            return await self._get_embedding_service().embed(text, component="Archive.vector")
        except Exception as e:
            print(f"   ⚠️ 向量生成失败: {e}")
            return []

    async def _save_to_space(
        self,
        file_path: Path,
        space: str,
        sub_space: str
    ) -> Path:
        """
        将文件复制到分类子文件夹。
        来自 inbound/ 的文件保留原件（由平台管理生命周期）。
        """
        import shutil

        space_config = config.get("spaces", {}).get(space, {})
        storage_base = space_config.get("root") or str(self.media_root / space)

        target_dir = Path(storage_base) / sub_space
        target_dir.mkdir(parents=True, exist_ok=True)

        timestamp = uuid.uuid4().hex[:8]
        target_path = target_dir / f"{file_path.stem}_{timestamp}{file_path.suffix}"

        shutil.copy2(file_path, target_path)

        return target_path

    def _infer_sub_space(self, caption: str, space: str) -> str:
        """根据描述推断子空间"""
        c = caption.lower()
        if space == "work":
            return "reimbursement" if any(kw in c for kw in ["发票", "收据", "报销"]) else "documents"
        if any(kw in c for kw in ["错题", "错误", "叉"]):
            return "wrong_questions"
        if any(kw in c for kw in ["经典", "好题", "标准答案"]):
            return "classic_questions"
        if any(kw in c for kw in ["公式", "概念", "知识点", "速查"]):
            return "quick_review"
        return "documents"

    def _write_to_hub(
        self,
        record_id: str,
        space: str,
        sub_space: str,
        storage_path: str,
        caption: str,
        keywords: List[str],
        analysis: dict,
        vector: list = None,
    ):
        """写入 HubStorage（在线程池中执行，勿直接 await）"""
        """写入 HubStorage SQLite，并将多模态向量保存到文件（供 SearchAgent 向量检索）"""
        import json
        import time as _time

        hub, mod, hub_space = load_hub_storage(space)

        SUB_SPACE_DOC_TYPE = {
            "wrong_questions": "错题集",
            "classic_questions": "经典题集",
            "quick_review": "速查手册",
            "documents": "打印文档",
            "health": "医疗文档",
            "reimbursement": "发票报销",
        }

        p = Path(storage_path)
        doc_type = analysis.get("doc_type") or SUB_SPACE_DOC_TYPE.get(sub_space, "其他")
        category = analysis.get("category") or ""
        now_ts = int(_time.time())

        # 保存多模态向量文件，写入 vector_id（= record_id）
        vector_id = None
        if vector:
            vector_id = save_record_vector(space, record_id, vector)

        record = mod.HubRecord(
            record_id=record_id,
            space=hub_space,
            original_path=storage_path,
            file_name=p.name,
            file_type=p.suffix.lstrip(".") or "jpg",
            file_size=p.stat().st_size if p.exists() else 0,
            created_at=int(p.stat().st_mtime) if p.exists() else now_ts,
            archived_at=now_ts,
            doc_type=doc_type,
            category=category or None,
            tags=json.dumps(keywords, ensure_ascii=False),
            semantic_summary=caption,
            extracted_text=analysis.get("extracted_text") or None,
            difficulty=analysis.get("difficulty") or None,
            vector_id=vector_id,
            sub_space=sub_space,
            caption=caption,
            keywords=",".join(keywords) if keywords else None,
            confidence=analysis.get("confidence"),
        )

        try:
            hub.add(record)
            print(f"   ✅ 写入 HubStorage [{hub_space}]: {record_id[:8]}... {doc_type}")
        except Exception as e:
            print(f"   ⚠️ HubStorage 写入失败: {e}")
            # 降级：写 jsonl 备份
            import json as _json
            from datetime import datetime
            space_config = config.get("spaces", {}).get(space, {})
            storage_base = space_config.get("storage") or str(self.media_root / space)
            hub_dir = Path(storage_base) / "hub"
            hub_dir.mkdir(parents=True, exist_ok=True)
            index_path = hub_dir / "index.jsonl"
            entry = {
                "record_id": record_id, "space": space, "sub_space": sub_space,
                "storage_path": storage_path, "caption": caption, "keywords": keywords,
                "created_at": datetime.now().isoformat()
            }
            with open(index_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
