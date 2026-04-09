#!/usr/bin/env python3
"""
DocSys 文件详情查看脚本
用法: python3 inspect.py <record_id> [family|work]
"""

import sys

sys.path.insert(0, '/Users/kk/.openclaw/skills/docsys')

from hub.storage import Hub


def main():
    if len(sys.argv) < 2:
        print("用法: python3 inspect.py <record_id> [family|work]")
        sys.exit(1)
    
    record_id = sys.argv[1]
    space = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ["family", "work"] else "family"
    
    hub = Hub(space)
    record = hub.get(record_id)
    
    if not record:
        print(f"未找到记录: {record_id}")
        sys.exit(1)
    
    print(f"=== 文件详情 ===")
    print(f"记录ID: {record.record_id}")
    print(f"文件名: {record.file_name}")
    print(f"文件路径: {record.original_path}")
    print(f"文件类型: {record.file_type}")
    print(f"文件大小: {record.file_size} bytes")
    print()
    print(f"--- 元数据 ---")
    print(f"成员: {record.member}")
    print(f"文档类型: {record.doc_type}")
    print(f"分类: {record.category}")
    print(f"标签: {record.tags}")
    print(f"难度: {record.difficulty}")
    print(f"方向: {record.orientation}")
    print(f"有签名: {record.has_signature}")
    print()
    print(f"--- 语义 ---")
    print(f"摘要: {record.semantic_summary}")
    print(f"同义词: {record.synonyms}")
    print()
    print(f"--- 其他 ---")
    print(f"创建时间: {record.created_at}")
    print(f"归档时间: {record.archived_at}")
    print(f"向量ID: {record.vector_id}")


if __name__ == "__main__":
    main()
