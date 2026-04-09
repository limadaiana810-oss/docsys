#!/usr/bin/env python3
"""
DocSys 语义搜索脚本
用法: python3 search.py <查询内容> [family|work] [--top-k 10]
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, '/Users/kk/.openclaw/skills/docsys')

from hub.storage import Hub
from services.api_layer import ServiceFactory


async def main():
    if len(sys.argv) < 2:
        print("用法: python3 search.py <查询内容> [family|work] [--top-k 10]")
        sys.exit(1)
    
    query = sys.argv[1]
    space = "family"
    top_k = 10
    
    # 解析参数
    args = sys.argv[2:]
    for i, arg in enumerate(args):
        if arg in ["family", "work"]:
            space = arg
        elif arg == "--top-k" and i + 1 < len(args):
            top_k = int(args[i + 1])
    
    print(f"=== 语义搜索 ===")
    print(f"查询: {query}")
    print(f"空间: {space}")
    print(f"Top-K: {top_k}")
    print()
    
    # 初始化服务
    config = ServiceFactory.get_config()
    print(f"Provider: {config.name}")
    print(f"Embedding: {config.embedding_model}")
    print()
    
    # 生成向量
    print("生成查询向量...")
    embedding = ServiceFactory.get_embedding()
    query_vector = await embedding.embed(query)
    print(f"向量维度: {len(query_vector)}")
    print()
    
    # 搜索
    hub = Hub(space)
    results = hub.search(
        filters={},
        query_vector=query_vector,
        top_k=top_k
    )
    
    print(f"找到 {len(results)} 条结果:")
    print()
    
    for i, r in enumerate(results, 1):
        print(f"{i}. {r.get('file_name', 'unknown')}")
        print(f"   ID: {r.get('record_id', '')[:8]}...")
        print(f"   类型: {r.get('doc_type')} | 分类: {r.get('category')}")
        print(f"   标签: {r.get('tags_list', [])}")
        summary = r.get('semantic_summary', '')
        if summary:
            print(f"   摘要: {summary[:80]}...")
        print()


if __name__ == "__main__":
    asyncio.run(main())
