"""
Bootstrap - 初始化目录结构

首次启动时自动创建所有必需的目录
"""

from pathlib import Path
from typing import List, Tuple
from hub import config


def ensure_directories() -> Tuple[bool, List[str]]:
    """
    确保所有必需的目录存在

    Returns:
        (created, messages): created=是否创建了新目录, messages=创建的目录列表
    """
    created_dirs = []

    # 基础路径
    paths_to_create = [
        config.get("paths.media", "/Users/kk/.openclaw/media/"),
        config.get("paths.inbound", "/Users/kk/.openclaw/media/inbound/"),
        config.get("paths.outbound", "/Users/kk/.openclaw/media/outbound/"),
        config.get("paths.workspace", "/Users/kk/.openclaw/workspace/"),
    ]

    # 空间目录
    spaces = config.get("spaces", {})
    for space_name, space_config in spaces.items():
        root = space_config.get("root", "")
        if root:
            paths_to_create.append(root)
            paths_to_create.append(f"{root}hub/")
            paths_to_create.append(f"{root}hub/meta/")
            paths_to_create.append(f"{root}hub/vectors/")

            # 子空间
            sub_spaces = space_config.get("sub_spaces", {})
            for sub_name, sub_path in sub_spaces.items():
                full_path = f"{root}{sub_path}"
                paths_to_create.append(full_path)

    # 创建目录
    for path_str in paths_to_create:
        path = Path(path_str)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(path))

    return len(created_dirs) > 0, created_dirs


def bootstrap() -> str:
    """
    执行初始化，返回用户友好的消息
    """
    created, dirs = ensure_directories()

    if not created:
        return ""  # 所有目录已存在，静默返回

    # 生成友好提示
    lines = ["📁 首次启动，已自动创建目录结构："]

    # 按空间分组显示
    home_dirs = [d for d in dirs if "/home/" in d]
    work_dirs = [d for d in dirs if "/work/" in d]
    other_dirs = [d for d in dirs if "/home/" not in d and "/work/" not in d]

    if home_dirs:
        lines.append(f"\n家庭空间（{len(home_dirs)} 个目录）")
        for d in home_dirs[:3]:
            lines.append(f"  • {Path(d).name}")
        if len(home_dirs) > 3:
            lines.append(f"  • ...还有 {len(home_dirs) - 3} 个")

    if work_dirs:
        lines.append(f"\n工作空间（{len(work_dirs)} 个目录）")
        for d in work_dirs[:3]:
            lines.append(f"  • {Path(d).name}")
        if len(work_dirs) > 3:
            lines.append(f"  • ...还有 {len(work_dirs) - 3} 个")

    if other_dirs:
        lines.append(f"\n其他（{len(other_dirs)} 个目录）")
        for d in other_dirs[:2]:
            lines.append(f"  • {Path(d).name}")

    lines.append("\n✅ 准备就绪，可以开始使用了")

    return "\n".join(lines)


if __name__ == "__main__":
    # 测试
    print(bootstrap())
