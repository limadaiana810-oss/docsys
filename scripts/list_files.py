#!/usr/bin/env python3
"""
DocSys 文件列表脚本
用法: python3 list_files.py [family|work] [--limit 50]
"""

import sys

sys.path.insert(0, '/Users/kk/.openclaw/skills/docsys')

from hub.storage import Hub


def main():
    space = "family"
    limit = 50
    
    # 解析参数
    for arg in sys.argv[1:]:
        if arg in ["family", "work"]:
            space = arg
        elif arg.startswith("--limit="):
            limit = int(arg.split("=")[1])
        elif arg.isdigit():
            limit = int(arg)
    
    hub = Hub(space)
    records = hub.list(limit=limit)
    total = hub.storage.count()
    
    print(f"=== {'家庭' if space == 'family' else '办公'}空间 Hub ===")
    print(f"总记录数: {total}")
    print(f"显示: {len(records)} 条")
    print()
    
    for r in records:
        print(f"- {r.file_name}")
        print(f"  ID: {r.record_id[:8]}...")
        print(f"  类型: {r.doc_type or '(无)'} | 分类: {r.category or '(无)'}")
        print(f"  标签: {r.tags}")
        print(f"  路径: {r.original_path}")
        print()


if __name__ == "__main__":
    main()
