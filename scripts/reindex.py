#!/usr/bin/env python3
"""
DocSys 批量重新索引脚本
用法: python3 reindex.py [family|work]
"""

import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, '/Users/kk/.openclaw/skills/docsys')

from hub.storage import Hub
from agents.archive.agent import ArchiveAgent


async def reindex_record(record, hub, agent):
    """重新索引单条记录"""
    print(f"\n处理: {record.file_name}")
    print(f"  当前: doc_type={record.doc_type}, category={record.category}")

    if not Path(record.original_path).exists():
        print(f"  ⚠️ 文件不存在，跳过")
        return False

    result = await agent.ingest(record.original_path, space_hint=hub.space)

    if result.success:
        print(f"  ✅ 新: doc_type={result.doc_type}, caption={result.caption[:50] if result.caption else ''}")

        hub.update_metadata(record.record_id, {
            "doc_type": result.doc_type,
            "category": result.category,
            "tags": json.dumps(result.keywords or []),
            "semantic_summary": result.caption,
        })
        return True
    else:
        print(f"  ❌ 失败: {result.error}")
        return False


async def main():
    space = "family"
    if len(sys.argv) > 1 and sys.argv[1] in ["family", "work"]:
        space = sys.argv[1]

    hub = Hub(space)
    agent = ArchiveAgent()

    records = hub.list(limit=100)

    print(f"=== 批量重新索引 ({len(records)} 条) ===")
    print(f"空间: {space}")
    print()

    success = 0
    failed = 0

    for record in records:
        ok = await reindex_record(record, hub, agent)
        if ok:
            success += 1
        else:
            failed += 1

    print(f"\n完成! 成功: {success}, 失败: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
