"""
SearchAgent - 语义搜索Agent

三策略：
  precise  全精准标签（≥3维度，无语义残留）→ SQL过滤 + 时间倒序
  partial  有标签但细节不全（1~2维度 或 标签+语义混合）→ SQL粗筛 + 向量细排 + 向量补充
  fuzzy    模糊语义（无标签）→ 向量(Path A) + OCR全文(Path B) + RRF融合
"""

import asyncio
import json
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from hub import (
    config, get_memory_llm, load_hub_storage, SPACE_MAP,
    get_embedding_service, load_record_vector, cosine_similarity,
    extract_json,
)


@dataclass
class SearchResult:
    record_id: str
    caption: str
    keywords: List[str]
    score: float
    storage_path: str
    space: str = "home"
    sub_space: str = "documents"
    match_type: str = "exact"  # exact / partial / semantic


@dataclass
class SearchResponse:
    success: bool
    results: List[SearchResult]
    total: int
    query: str
    strategy: str = ""
    need_clarification: bool = False
    clarification_options: List[str] = None
    error: Optional[str] = None


class SearchAgent:

    # ── 标签库 ──────────────────────────────────────────────────────────────
    TAG_LIBRARY = {
        "错题":  ["错题", "错题集", "错题本", "错误"],
        "经典题": ["经典", "经典题", "好题"],
        "速查":  ["速查", "速背", "概念", "公式", "知识点", "背诵"],
        "数学":  ["数学", "math"],
        "语文":  ["语文", "Chinese"],
        "英语":  ["英语", "English", "英文"],
        "物理":  ["物理", "physics"],
        "化学":  ["化学", "chemistry"],
        "试卷":  ["试卷", "考试", "测验"],
        "作业":  ["作业", "homework"],
        "发票":  ["发票", "invoice"],
        "报销":  ["报销", "报销单"],
        "收据":  ["收据", "receipt"],
        "账单":  ["账单", "bill"],
        "餐饮":  ["餐饮", "餐费", "吃饭"],
        "交通":  ["交通", "打车", "打车费", "油费"],
        "差旅":  ["差旅", "出差"],
        "办公":  ["办公", "办公用品"],
        "本月":  ["本月", "这个月", "今月"],
        "上月":  ["上月", "上个月"],
        "本年":  ["本年", "今年", "本年度"],
        "去年":  ["去年", "上年度"],
        "home":  ["家庭", "home", "个人"],
        "work":  ["工作", "办公", "公司", "work"],
        "合同":  ["合同", "contract"],
        "健康":  ["健康", "体检", "报告", "health"],
        "证件":  ["证件", "证书", "certificate"],
    }

    # 标签维度分类（每个集合算1个维度）
    _TYPE_TAGS    = {"错题", "经典题", "速查", "试卷", "发票", "报销",
                     "收据", "账单", "合同", "健康", "证件", "作业"}
    _SUBJECT_TAGS = {"数学", "语文", "英语", "物理", "化学"}
    _SPACE_TAGS   = {"home", "work"}
    _TIME_TAGS    = {"本月", "上月", "本年", "去年"}
    _DETAIL_TAGS  = {"差旅", "餐饮", "交通", "办公"}

    # 向量相似度阈值
    VECTOR_SCORE_THRESHOLD = 0.35
    # 策略2：SQL候选不足时触发向量补充的阈值
    MIN_PARTIAL_CANDIDATES = 5
    # RRF k 值
    RRF_K = 60

    def __init__(self, workspace_path: str = None):
        self.workspace = Path(workspace_path or config.get("paths.workspace"))
        self.media_root = Path(config.get("paths.media", "/Users/kk/.openclaw/media/"))
        self._llm = None
        self._embedding_svc = None

    # ── 公开接口 ─────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        filters: Dict = None,
        limit: int = 20,
        user_profile: Dict = None,
    ) -> SearchResponse:
        filters = filters or {}
        user_profile = user_profile or {}

        try:
            parsed = self._parse_query(query, filters)

            # 关键词匹配不充分但查询足够复杂时，用 LLM 提取结构化条件
            if parsed["tag_dims"] <= 1 and len(query) >= 4:
                llm_parsed = await self._parse_query_llm(query)
                if llm_parsed:
                    parsed = self._merge_llm_parse(parsed, llm_parsed)

            strategy = self._select_strategy(parsed)

            print(f"   🔍 策略: {strategy} | dims={parsed['tag_dims']} | tags={parsed['exact_tags']} | keywords={parsed.get('semantic_keywords', [])}")

            if strategy == "precise":
                results = await self._search_precise(parsed, limit)
            elif strategy == "partial":
                results = await self._search_partial(parsed, limit, user_profile)
            else:
                results = await self._search_fuzzy(parsed, limit, user_profile)

            results.sort(key=lambda x: x.score, reverse=True)
            results = results[:limit]

            # 模糊搜索无结果 → 触发澄清
            if not results and strategy == "fuzzy":
                options = await self._generate_clarification_options(query, filters)
                return SearchResponse(
                    success=True, results=[], total=0, query=query,
                    strategy=strategy, need_clarification=True,
                    clarification_options=options,
                )

            return SearchResponse(
                success=True, results=results, total=len(results),
                query=query, strategy=strategy,
            )

        except Exception as e:
            return SearchResponse(
                success=False, results=[], total=0, query=query, error=str(e)
            )

    # ── 解析与策略选择 ────────────────────────────────────────────────────────

    def _parse_query(self, query: str, filters: Dict) -> Dict[str, Any]:
        """提取 exact_tags、tag_dims、semantic_keywords"""
        query_lower = query.lower()

        # 1. 精确标签匹配
        exact_tags = []
        for tag_name, variants in self.TAG_LIBRARY.items():
            for v in variants:
                if v in query or v.lower() in query_lower:
                    exact_tags.append(tag_name)
                    break

        # 2. 标签维度数
        tag_dims = self._count_tag_dims(exact_tags)

        # 3. 语义残留（去掉所有标签变体后剩余的词）
        temp = query
        for variants in self.TAG_LIBRARY.values():
            for v in variants:
                temp = temp.replace(v, "").replace(v.lower(), "")
        semantic_keywords = [w.strip() for w in temp.split() if len(w.strip()) > 1]

        return {
            "original_query": query,
            "exact_tags": exact_tags,
            "tag_dims": tag_dims,
            "semantic_keywords": semantic_keywords,
            "filters": filters,
        }

    def _count_tag_dims(self, tags: List[str]) -> int:
        """计算标签覆盖的维度数（最多5）"""
        s = set(tags)
        return sum([
            bool(s & self._TYPE_TAGS),
            bool(s & self._SUBJECT_TAGS),
            bool(s & self._SPACE_TAGS),
            bool(s & self._TIME_TAGS),
            bool(s & self._DETAIL_TAGS),
        ])

    def _select_strategy(self, parsed: Dict) -> str:
        dims = parsed["tag_dims"]
        has_tags = bool(parsed["exact_tags"])
        has_semantic = bool(parsed["semantic_keywords"])
        llm_enhanced = parsed.get("llm_enhanced", False)

        # LLM 增强后 semantic_keywords 是精确知识点，不算模糊残留
        if dims >= 3 and (not has_semantic or llm_enhanced):
            return "precise"
        elif has_tags:
            return "partial"
        else:
            return "fuzzy"

    # ── LLM 结构化提取（自然语言 → 精准条件）────────────────────────────────

    async def _parse_query_llm(self, query: str) -> Optional[Dict]:
        """用轻量 LLM 从自然语言提取结构化搜索条件"""
        try:
            llm = self._get_llm()
            system = (
                "从用户的搜索请求中提取结构化条件。只输出JSON，不要其他文字。\n"
                "字段说明：\n"
                '  time: 时间范围，只能是 "本月"/"上月"/"本年"/"去年" 或 null\n'
                '  doc_type: 文档类型，只能是 "错题"/"经典题"/"速查"/"试卷"/"作业"/"发票"/"报销"/"收据"/"账单"/"合同" 或 null\n'
                '  subject: 学科，只能是 "数学"/"语文"/"英语"/"物理"/"化学" 或 null\n'
                '  space: 空间，只能是 "home"/"work" 或 null\n'
                '  keywords: 具体知识点/主题/内容描述（数组，不超过3个）\n'
                "只提取用户明确表达的，不猜测。\n"
                '示例输入: "上个月做错的三角函数题"\n'
                '示例输出: {"time":"上月","doc_type":"错题","subject":"数学","space":null,"keywords":["三角函数"]}'
            )
            result = await llm.generate(
                query, system=system, max_tokens=100, temperature=0,
                component="Search.parse_query_llm",
            )
            data = extract_json(result)
            if isinstance(data, dict):
                print(f"   🧠 LLM解析: {data}")
                return data
        except Exception as e:
            print(f"   ⚠️ LLM解析失败，降级关键词: {e}")
        return None

    def _merge_llm_parse(self, parsed: Dict, llm_data: Dict) -> Dict:
        """将 LLM 提取的结构化字段合并到关键词解析结果"""
        new_tags = list(parsed["exact_tags"])

        # LLM 字段 → 标签名映射
        _FIELD_TAG_MAP = {
            "time": {"本月": "本月", "上月": "上月", "本年": "本年", "去年": "去年"},
            "doc_type": {
                "错题": "错题", "经典题": "经典题", "速查": "速查",
                "试卷": "试卷", "作业": "作业", "发票": "发票",
                "报销": "报销", "收据": "收据", "账单": "账单", "合同": "合同",
            },
            "subject": {
                "数学": "数学", "语文": "语文", "英语": "英语",
                "物理": "物理", "化学": "化学",
            },
            "space": {"home": "home", "work": "work"},
        }

        for field, mapping in _FIELD_TAG_MAP.items():
            val = llm_data.get(field)
            if val and val != "null" and val in mapping:
                tag = mapping[val]
                if tag not in new_tags:
                    new_tags.append(tag)

        # LLM 提取的 keywords 替换原始粗粒度残留
        # （原始残留如"做错的三角函数题"已被 LLM 精确拆解，不再需要）
        llm_kws = [k for k in (llm_data.get("keywords") or []) if k]

        parsed["exact_tags"] = new_tags
        parsed["tag_dims"] = self._count_tag_dims(new_tags)
        parsed["semantic_keywords"] = llm_kws
        parsed["llm_enhanced"] = True
        return parsed

    # ── 策略1: 全精准标签 ─────────────────────────────────────────────────────

    async def _search_precise(self, parsed: Dict, limit: int) -> List[SearchResult]:
        """
        SQL多字段过滤，按归档时间倒序。
        LLM 提取的 semantic_keywords 也传入 SQL tag 过滤。
        """
        exact_tags = parsed["exact_tags"]
        filters = parsed.get("filters", {})
        extra_kws = parsed.get("semantic_keywords", [])
        spaces = self._determine_spaces_from_tags(exact_tags, filters)

        now_ts = _time.time()
        all_results = []

        for space in spaces:
            records = await self._sql_filter(space, exact_tags, limit=limit * 2,
                                             extra_keywords=extra_kws)
            for rec in records:
                if not Path(rec.original_path).exists():
                    continue
                rec_tags = json.loads(rec.tags) if rec.tags else []
                # score = 归档时间的相对新鲜度（0~1，越新越高）
                score = min(1.0, rec.archived_at / now_ts) if rec.archived_at else 0.5
                all_results.append(SearchResult(
                    record_id=rec.record_id,
                    caption=rec.semantic_summary or rec.file_name,
                    keywords=rec_tags,
                    score=score,
                    storage_path=rec.original_path,
                    space=space,
                    sub_space=rec.doc_type or "documents",
                    match_type="exact",
                ))

        return all_results

    # ── 策略2: 有标签但细节不全 ───────────────────────────────────────────────

    async def _search_partial(self, parsed: Dict, limit: int,
                               user_profile: Dict) -> List[SearchResult]:
        """
        阶段1: SQL粗筛（已知标签过滤候选集）
        阶段2: 向量细排（在候选集内 cosine rerank）
        阶段3: 候选不足时向量全库补充（降权0.7）
        """
        exact_tags = parsed["exact_tags"]
        filters = parsed.get("filters", {})
        spaces = self._determine_spaces_from_tags(exact_tags, filters)

        # 阶段1: SQL粗筛
        extra_kws = parsed.get("semantic_keywords", [])
        candidates: List[Tuple[str, Any]] = []  # (space, rec)
        for space in spaces:
            recs = await self._sql_filter(space, exact_tags, limit=limit * 3,
                                          extra_keywords=extra_kws)
            candidates.extend((space, rec) for rec in recs)

        # 阶段2: 向量细排
        query_vector = await self._embed_query(parsed["original_query"])

        results = []
        for space, rec in candidates:
            if not Path(rec.original_path).exists():
                continue
            rec_tags = json.loads(rec.tags) if rec.tags else []

            if query_vector:
                rec_vector = load_record_vector(space, rec.record_id)
                score = cosine_similarity(query_vector, rec_vector) if rec_vector else 0.6
            else:
                score = 0.6  # SQL命中但无向量，给中等分

            results.append(SearchResult(
                record_id=rec.record_id,
                caption=rec.semantic_summary or rec.file_name,
                keywords=rec_tags,
                score=score,
                storage_path=rec.original_path,
                space=space,
                sub_space=rec.doc_type or "documents",
                match_type="partial",
            ))

        # 阶段3: 候选不足 → 向量全库补充
        if len(results) < self.MIN_PARTIAL_CANDIDATES and query_vector:
            already_ids = {r.record_id for r in results}
            supplement = await self._search_fuzzy(parsed, limit - len(results), user_profile)
            for hit in supplement:
                if hit.record_id not in already_ids:
                    hit.score *= 0.7
                    hit.match_type = "partial_supplement"
                    results.append(hit)

        return results

    # ── 策略3: 模糊语义 ───────────────────────────────────────────────────────

    async def _search_fuzzy(self, parsed: Dict, limit: int,
                             user_profile: Dict = None) -> List[SearchResult]:
        """
        Path A: 向量相似度（user_profile 上下文增强 embedding）
        Path B: OCR全文关键词匹配（extracted_text / semantic_summary）
        → RRF融合
        """
        user_profile = user_profile or {}
        filters = parsed.get("filters", {})

        # 构建增强查询文本，发起 embedding（网络）同时做本地准备工作
        query_text = self._build_query_text(parsed, user_profile)
        embedding_task = asyncio.create_task(self._embed_query(query_text))

        # 收集全库记录（sync，与 embedding 并发）
        all_records: Dict[str, Tuple[str, Any]] = {}  # record_id → (space, rec)
        for space in ["home", "work"]:
            if filters.get("space") and filters["space"] != space:
                continue
            try:
                hub = self._get_hub(space)
                records = hub.list(limit=limit * 10)
                for rec in records:
                    if Path(rec.original_path).exists():
                        all_records[rec.record_id] = (space, rec)
            except Exception as e:
                print(f"   ⚠️ 加载记录失败 [{space}]: {e}")

        path_a: Dict[str, Tuple[int, SearchResult]] = {}  # rid → (rank, result)
        path_b: Dict[str, Tuple[int, SearchResult]] = {}

        # Path B: OCR全文关键词匹配（不依赖 embedding，先跑）
        terms = [t.lower() for t in parsed.get("semantic_keywords", []) if len(t) > 1]
        if terms:
            scored = []
            for rid, (space, rec) in all_records.items():
                rec_text = " ".join(filter(None, [
                    rec.semantic_summary or "",
                    rec.extracted_text or "",
                ])).lower()
                s = sum(0.4 for t in terms if t in rec_text)
                if s > 0:
                    scored.append((s, rid, space, rec))
            scored.sort(key=lambda x: x[0], reverse=True)
            for rank, (s, rid, space, rec) in enumerate(scored):
                rec_tags = json.loads(rec.tags) if rec.tags else []
                path_b[rid] = (rank, SearchResult(
                    record_id=rid, caption=rec.semantic_summary or rec.file_name,
                    keywords=rec_tags, score=s, storage_path=rec.original_path,
                    space=space, sub_space=rec.doc_type or "documents",
                    match_type="semantic",
                ))

        # Path A: 向量相似度（等待 embedding 完成）
        query_vector = await embedding_task
        if query_vector:
            scored = []
            for rid, (space, rec) in all_records.items():
                rec_vector = load_record_vector(space, rid)
                if rec_vector:
                    s = cosine_similarity(query_vector, rec_vector)
                    if s >= self.VECTOR_SCORE_THRESHOLD:
                        scored.append((s, rid, space, rec))
            scored.sort(key=lambda x: x[0], reverse=True)
            for rank, (s, rid, space, rec) in enumerate(scored):
                rec_tags = json.loads(rec.tags) if rec.tags else []
                path_a[rid] = (rank, SearchResult(
                    record_id=rid, caption=rec.semantic_summary or rec.file_name,
                    keywords=rec_tags, score=s, storage_path=rec.original_path,
                    space=space, sub_space=rec.doc_type or "documents",
                    match_type="semantic",
                ))

        return self._rrf_merge(path_a, path_b, limit)

    # ── RRF融合 ──────────────────────────────────────────────────────────────

    def _rrf_merge(
        self,
        path_a: Dict[str, Tuple[int, SearchResult]],
        path_b: Dict[str, Tuple[int, SearchResult]],
        limit: int,
    ) -> List[SearchResult]:
        """Reciprocal Rank Fusion: score = Σ 1/(rank + k)"""
        all_ids = set(path_a) | set(path_b)
        merged = []
        for rid in all_ids:
            rrf_score = 0.0
            result = None
            if rid in path_a:
                rank, res = path_a[rid]
                rrf_score += 1.0 / (rank + self.RRF_K)
                result = res
            if rid in path_b:
                rank, res = path_b[rid]
                rrf_score += 1.0 / (rank + self.RRF_K)
                if result is None:
                    result = res
            result.score = rrf_score
            merged.append(result)
        merged.sort(key=lambda x: x.score, reverse=True)
        return merged[:limit]

    # ── SQL过滤层 ─────────────────────────────────────────────────────────────

    async def _sql_filter(self, space: str, tags: List[str], limit: int,
                          extra_keywords: List[str] = None) -> List[Any]:
        """将 TAG_LIBRARY 标签映射到 HubStorage 字段，执行SQL过滤，返回 HubRecord 列表"""
        try:
            hub = self._get_hub(space)
        except Exception as e:
            print(f"   ⚠️ HubStorage 加载失败 [{space}]: {e}")
            return []

        doc_type_hint = None
        tag_filter = []
        time_range = None

        for tag in tags:
            variants = self.TAG_LIBRARY.get(tag, [tag])
            if tag in ("错题", "经典题", "速查", "试卷", "发票", "报销"):
                doc_type_hint = doc_type_hint or variants[0]
            tag_filter.extend(variants)

        # 过滤纯空间标签
        tag_filter = [t for t in tag_filter
                      if t not in ("家庭", "home", "个人", "工作", "公司", "work")]

        # 加入 LLM 提取的具体知识点关键词（如"三角函数"、"分数加减"）
        if extra_keywords:
            tag_filter.extend(k for k in extra_keywords if k not in tag_filter)

        # 时间标签 → archived_at 范围
        if tags:
            import datetime as _dt
            now = _dt.datetime.now()
            if "本月" in tags:
                start = _dt.datetime(now.year, now.month, 1)
                time_range = (start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
            elif "上月" in tags:
                first = _dt.datetime(now.year, now.month, 1)
                end = first - _dt.timedelta(days=1)
                start = _dt.datetime(end.year, end.month, 1)
                time_range = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            elif "本年" in tags:
                time_range = (f"{now.year}-01-01", now.strftime("%Y-%m-%d"))
            elif "去年" in tags:
                time_range = (f"{now.year - 1}-01-01", f"{now.year - 1}-12-31")

        try:
            records = hub.search_by_filter(
                doc_type=doc_type_hint,
                tags=tag_filter[:5] or None,
                time_range=time_range,
                limit=limit,
            )
        except Exception as e:
            print(f"   ⚠️ SQL过滤失败: {e}")
            records = []

        # 降级：SQL无结果时全量扫描
        if not records:
            records = hub.list(limit=limit)

        return records

    # ── Embedding 工具 ────────────────────────────────────────────────────────

    # 模块级缓存：key → (vector, timestamp)，TTL=300s
    _embed_cache: Dict[str, tuple] = {}
    _EMBED_TTL = 300

    async def _embed_query(self, text: str) -> Optional[List[float]]:
        import time as _time
        now = _time.time()
        cached = self._embed_cache.get(text)
        if cached:
            vec, ts = cached
            if now - ts < self._EMBED_TTL:
                return vec
        try:
            vec = await self._get_embedding_service().embed(text, component="Search.embed")
            if vec:
                self._embed_cache[text] = (vec, now)
            return vec
        except Exception as e:
            print(f"   ⚠️ embed失败: {e}")
            return None

    def _build_query_text(self, parsed: Dict, user_profile: Dict) -> str:
        """用 user_profile 上下文增强查询文本（避免 query 改写，低成本增强语义）"""
        base = parsed["original_query"]
        parts = []

        grade = user_profile.get("grade") or user_profile.get("learning.grade")
        subjects = user_profile.get("current_subjects") or user_profile.get("learning.current_subjects")

        if grade:
            parts.append(f"{grade}年级")
        if subjects:
            if isinstance(subjects, list):
                parts.append("学科:" + "".join(subjects[:3]))
            else:
                parts.append(f"学科:{subjects}")

        return f"{base} {' '.join(parts)}".strip() if parts else base

    # ── 澄清 ─────────────────────────────────────────────────────────────────

    async def _generate_clarification_options(
        self, query: str, filters: Dict
    ) -> List[str]:
        """模糊搜索无结果时生成澄清选项"""
        space_hint = filters.get("space", "")
        options = []
        if space_hint != "work":
            options += ["找数学错题？", "找语文试卷？", "找经典题集？"]
        if space_hint != "home":
            options += ["找本月发票？", "找报销单？", "找差旅票据？"]
        return options[:4]

    # ── 空间/子空间工具 ───────────────────────────────────────────────────────

    def _determine_spaces_from_tags(self, tags: List[str], filters: Dict) -> List[str]:
        """根据标签推断要搜索的空间列表"""
        if filters.get("space"):
            return [filters["space"]]

        work_tags = {"发票", "报销", "收据", "账单", "餐饮", "交通", "差旅", "办公", "合同", "work"}
        home_tags = set(self.TAG_LIBRARY.keys()) - work_tags

        tag_set = set(tags)
        spaces = []
        if tag_set & home_tags:
            spaces.append("home")
        if tag_set & work_tags:
            spaces.append("work")
        return spaces or ["home", "work"]

    def _get_all_sub_spaces(self, space: str) -> List[str]:
        space_config = config.get_space(space)
        sub_spaces = space_config.get("sub_spaces", {})
        return list(sub_spaces.keys()) if sub_spaces else ["documents"]

    # ── 内部 lazy init ────────────────────────────────────────────────────────

    def _get_hub(self, space: str):
        hub, _mod, _hub_space = load_hub_storage(space)
        return hub

    def _get_llm(self):
        if self._llm is None:
            self._llm = get_memory_llm()
        return self._llm

    def _get_embedding_service(self):
        if self._embedding_svc is None:
            self._embedding_svc = get_embedding_service()
        return self._embedding_svc
