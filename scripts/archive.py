#!/usr/bin/env python3
"""
DocSys 归档脚本
用法: python3 archive.py <文件路径> [family|work]
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, '/Users/kk/.openclaw/skills/docsys')

from agents.archive.agent import ArchiveAgent


async def main():
    if len(sys.argv) < 2:
        print("用法: python3 archive.py <文件路径> [family|work]")
        sys.exit(1)

    file_path = sys.argv[1]
    space = sys.argv[2] if len(sys.argv) > 2 else "family"

    # 映射 family → home（新 API 使用 home/work）
    space_hint = "home" if space == "family" else space

    if not Path(file_path).exists():
        print(f"错误: 文件不存在: {file_path}")
        sys.exit(1)

    print(f"=== 归档文件 ===")
    print(f"文件: {file_path}")
    print(f"空间: {space}")
    print()

    agent = ArchiveAgent()
    result = await agent.ingest(file_path, space_hint=space_hint)

    print()
    print(f"结果: {'成功' if result.success else '失败'}")

    if result.success:
        print(f"记录ID: {result.record_id[:8]}...")
        print(f"文档类型: {result.doc_type}")
        print(f"分类: {result.category}")
        print(f"关键词: {result.keywords}")
        print(f"描述: {(result.caption or '')[:100]}...")
    else:
        print(f"错误: {result.error}")


if __name__ == "__main__":
    asyncio.run(main())
