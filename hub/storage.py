"""
DocSys Hub - 存储层
负责: 文件存储、元数据管理、向量索引、语义摘要
"""

import os
import json
import uuid
import sqlite3
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple

from .config import config

# ============== 路径配置 ==============


def _resolve_hub_path(space: str) -> str:
    """从 config.json 动态解析 hub 路径"""
    space_key = "home" if space == "family" else space
    space_cfg = config.get(f"spaces.{space_key}", {})
    root = space_cfg.get("root", "")
    if root:
        return root.rstrip("/") + "/hub"
    return str(Path.home() / ".openclaw" / "media" / space_key / "hub")

# ============== 数据模型 ==============

@dataclass
class HubRecord:
    record_id: str
    space: str                    # family / work
    
    # 原文件
    original_path: str
    file_name: str
    file_type: str
    file_size: int
    created_at: int               # timestamp
    
    # 元数据
    archived_at: int              # timestamp
    member: Optional[str] = None  # 家庭成员
    doc_type: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[str] = None    # JSON array string
    
    # 办公空间
    project: Optional[str] = None
    business_category: Optional[str] = None
    
    # 语义
    semantic_summary: Optional[str] = None
    synonyms: Optional[str] = None  # JSON array string
    
    # 多模态扩展字段
    extracted_text: Optional[str] = None  # 图片识别文字
    difficulty: Optional[str] = None      # 难度（基础/中等/困难）
    orientation: Optional[str] = None      # 图片方向（横版/竖版）
    has_signature: Optional[bool] = None  # 是否有签名
    
    # 向量 (不存DB，只存引用)
    vector_id: Optional[str] = None
    
    # 完整元数据 JSON（LLM输出的完整记录）
    metadata_json: Optional[str] = None  # JSON string: {raw_text, structured, summary, synonyms, model_version}

    # 归档分类扩展（v3.0 新增）
    sub_space: Optional[str] = None       # wrong_questions / classic_questions / ...
    caption: Optional[str] = None         # 一句话描述（≤80字）
    keywords: Optional[str] = None        # 逗号分隔关键词
    confidence: Optional[float] = None    # 分类置信度 0.0-1.0

    def to_dict(self) -> Dict:
        d = asdict(self)
        # JSON 字段解析
        if self.tags:
            d["tags_list"] = json.loads(self.tags)
        if self.synonyms:
            d["synonyms_list"] = json.loads(self.synonyms)
        # 完整元数据 JSON
        if self.metadata_json:
            try:
                d["metadata_json_parsed"] = json.loads(self.metadata_json)
            except:
                d["metadata_json_parsed"] = None
        return d
    
    def to_filter_dict(self) -> Dict:
        """转为元数据过滤器格式"""
        f = {}
        if self.member:
            f["member"] = self.member
        if self.doc_type:
            f["doc_type"] = self.doc_type
        if self.category:
            f["category"] = self.category
        if self.project:
            f["project"] = self.project
        if self.business_category:
            f["business_category"] = self.business_category
        if self.tags:
            tags_list = json.loads(self.tags)
            f["tags"] = tags_list
        return f


class HubStorage:
    """Hub 存储管理器"""
    
    def __init__(self, space: str):
        """
        Args:
            space: "family" 或 "work"
        """
        self.space = space
        self.base_path = Path(_resolve_hub_path(space))
        
        # 确保目录存在
        self.base_path.mkdir(parents=True, exist_ok=True)
        (self.base_path / "files").mkdir(exist_ok=True)
        (self.base_path / "summary").mkdir(exist_ok=True)
        
        # 初始化 SQLite
        self.db_path = self.base_path / "meta" / "meta.db"
        self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                space TEXT NOT NULL,
                original_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                archived_at INTEGER NOT NULL,
                member TEXT,
                doc_type TEXT,
                category TEXT,
                tags TEXT,
                project TEXT,
                business_category TEXT,
                semantic_summary TEXT,
                synonyms TEXT,
                vector_id TEXT,
                extracted_text TEXT,
                difficulty TEXT,
                orientation TEXT,
                has_signature INTEGER,
                metadata_json TEXT
            )
        """)
        
        # 迁移旧数据库：添加新列（如果不存在）
        for col in [("extracted_text", "TEXT"), ("difficulty", "TEXT"),
                    ("orientation", "TEXT"), ("has_signature", "INTEGER"),
                    ("metadata_json", "TEXT"),
                    ("sub_space", "TEXT"), ("caption", "TEXT"),
                    ("keywords", "TEXT"), ("confidence", "REAL")]:
            try:
                cursor.execute(f"ALTER TABLE records ADD COLUMN {col[0]} {col[1]}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        
        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_type ON records(doc_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_category ON records(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_member ON records(member)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_project ON records(project)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_archived_at ON records(archived_at)")
        
        conn.commit()
        conn.close()
    
    def add(self, record: HubRecord) -> str:
        """添加记录"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO records (
                record_id, space, original_path, file_name, file_type, file_size,
                created_at, archived_at, member, doc_type, category, tags,
                project, business_category, semantic_summary, synonyms, vector_id,
                extracted_text, difficulty, orientation, has_signature,
                metadata_json,
                sub_space, caption, keywords, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record.record_id,
            record.space,
            record.original_path,
            record.file_name,
            record.file_type,
            record.file_size,
            record.created_at,
            record.archived_at,
            record.member,
            record.doc_type,
            record.category,
            record.tags,
            record.project,
            record.business_category,
            record.semantic_summary,
            record.synonyms,
            record.vector_id,
            record.extracted_text,
            record.difficulty,
            record.orientation,
            1 if record.has_signature else 0 if record.has_signature is not None else None,
            record.metadata_json,
            record.sub_space,
            record.caption,
            record.keywords,
            record.confidence,
        ))
        
        conn.commit()
        conn.close()
        
        # 保存摘要文件
        if record.semantic_summary:
            summary_path = self.base_path / "summary" / f"{record.record_id}.txt"
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(record.semantic_summary)
        
        return record.record_id
    
    def get(self, record_id: str) -> Optional[HubRecord]:
        """获取单条记录"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM records WHERE record_id = ?", (record_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return self._row_to_record(row)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[HubRecord]:
        """列出记录"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT * FROM records ORDER BY archived_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        rows = cursor.fetchall()
        conn.close()
        
        return [self._row_to_record(row) for row in rows]
    
    def search_by_filter(
        self,
        doc_type: Optional[str] = None,
        category: Optional[str] = None,
        member: Optional[str] = None,
        project: Optional[str] = None,
        business_category: Optional[str] = None,
        time_range: Optional[Tuple[str, str]] = None,  # (start, end)
        tags: Optional[List[str]] = None,
        limit: int = 100
    ) -> List[HubRecord]:
        """基于元数据过滤搜索"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        conditions = ["space = ?"]
        params = [self.space]
        
        if doc_type:
            conditions.append("doc_type = ?")
            params.append(doc_type)
        
        if category:
            conditions.append("category = ?")
            params.append(category)
        
        if member:
            conditions.append("member = ?")
            params.append(member)
        
        if project:
            conditions.append("project = ?")
            params.append(project)
        
        if business_category:
            conditions.append("business_category = ?")
            params.append(business_category)
        
        if time_range:
            conditions.append("archived_at >= ? AND archived_at <= ?")
            # 转换日期字符串为 timestamp
            start_ts = self._date_to_ts(time_range[0])
            end_ts = self._date_to_ts(time_range[1]) + 86400  # 加一天
            params.extend([start_ts, end_ts])
        
        query = f"SELECT * FROM records WHERE {' AND '.join(conditions)} ORDER BY archived_at DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        records = [self._row_to_record(row) for row in rows]
        
        # tags 过滤需要单独处理（子串匹配："数学" 命中 "初中数学"）
        if tags:
            filtered = []
            for r in records:
                if r.tags:
                    record_tags = json.loads(r.tags)
                    record_tags_lower = [rt.lower() for rt in record_tags]
                    if any(
                        t.lower() in record_tags_lower or
                        any(t.lower() in rt for rt in record_tags_lower)
                        for t in tags
                    ):
                        filtered.append(r)
            return filtered
        
        return records
    
    def delete(self, record_id: str) -> bool:
        """删除记录"""
        record = self.get(record_id)
        if not record:
            return False
        
        # 删除文件
        if record.original_path and Path(record.original_path).exists():
            Path(record.original_path).unlink()
        
        # 删除摘要
        summary_path = self.base_path / "summary" / f"{record_id}.txt"
        if summary_path.exists():
            summary_path.unlink()
        
        # 删除数据库记录
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("DELETE FROM records WHERE record_id = ?", (record_id,))
        conn.commit()
        conn.close()
        
        return True
    
    def update_metadata(self, record_id: str, updates: Dict) -> bool:
        """更新元数据"""
        record = self.get(record_id)
        if not record:
            return False
        
        for key, value in updates.items():
            if hasattr(record, key):
                setattr(record, key, value)
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE records SET
                member = ?,
                doc_type = ?,
                category = ?,
                tags = ?,
                project = ?,
                business_category = ?,
                semantic_summary = ?,
                synonyms = ?
            WHERE record_id = ?
        """, (
            record.member,
            record.doc_type,
            record.category,
            record.tags,
            record.project,
            record.business_category,
            record.semantic_summary,
            record.synonyms,
            record_id
        ))
        
        conn.commit()
        conn.close()
        
        return True
    
    def count(self) -> int:
        """统计记录数"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM records WHERE space = ?", (self.space,))
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    def _row_to_record(self, row: tuple) -> HubRecord:
        """数据库行转记录"""
        return HubRecord(
            record_id=row[0],
            space=row[1],
            original_path=row[2],
            file_name=row[3],
            file_type=row[4],
            file_size=row[5],
            created_at=row[6],
            archived_at=row[7],
            member=row[8],
            doc_type=row[9],
            category=row[10],
            tags=row[11],
            project=row[12],
            business_category=row[13],
            semantic_summary=row[14],
            synonyms=row[15],
            vector_id=row[16],
            extracted_text=row[17] if len(row) > 17 else None,
            difficulty=row[18] if len(row) > 18 else None,
            orientation=row[19] if len(row) > 19 else None,
            has_signature=bool(row[20]) if len(row) > 20 and row[20] is not None else None,
            metadata_json=row[21] if len(row) > 21 else None,
            sub_space=row[22] if len(row) > 22 else None,
            caption=row[23] if len(row) > 23 else None,
            keywords=row[24] if len(row) > 24 else None,
            confidence=float(row[25]) if len(row) > 25 and row[25] is not None else None,
        )
    
    def _date_to_ts(self, date_str: str) -> int:
        """日期字符串转 timestamp"""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return int(dt.timestamp())
        except:
            return 0


# ============== Hub Facade ==============

# 向量读写统一使用 hub/utils.py 的 save_record_vector / load_record_vector
# 每个 record 一个 JSON 文件: media/{space}/hub/vectors/{record_id}.json
# 归档时由 ArchiveAgent 写入，搜索时由 SearchAgent 读取

# 向量操作统一使用 hub/utils.py，避免重复实现
from .utils import load_record_vector as _load_vector, cosine_similarity as _cosine_sim


class Hub:
    """Hub 统一入口（脚本/外部调用用）"""

    def __init__(self, space: str):
        self.space = space
        self.storage = HubStorage(space)

    def search(
        self,
        filters: Dict[str, Any],
        query_vector: Optional[List[float]] = None,
        top_k: int = 10
    ) -> List[Dict]:
        """
        搜索文档 — Filter-then-Rank
        向量排序使用 per-file 向量（与 SearchAgent 同一数据源）
        """
        records = self.storage.search_by_filter(
            doc_type=filters.get("doc_type"),
            category=filters.get("category"),
            member=filters.get("member"),
            project=filters.get("project"),
            business_category=filters.get("business_category"),
            time_range=filters.get("time_range"),
            tags=filters.get("tags"),
            limit=top_k * 2,
        )

        if query_vector and records:
            scored = []
            for r in records:
                vec = _load_vector(self.space, r.record_id)
                sim = _cosine_sim(query_vector, vec) if vec else 0.0
                scored.append((sim, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            records = [r for _, r in scored]

        return [r.to_dict() for r in records[:top_k]]

    def list(self, limit: int = 100, offset: int = 0) -> List[HubRecord]:
        return self.storage.list(limit=limit, offset=offset)

    def get(self, record_id: str) -> Optional[HubRecord]:
        return self.storage.get(record_id)

    def delete(self, record_id: str) -> bool:
        return self.storage.delete(record_id)

    def update_metadata(self, record_id: str, updates: Dict) -> bool:
        return self.storage.update_metadata(record_id, updates)
