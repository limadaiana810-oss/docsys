"""
ConfigLoader - 配置文件加载器
从 config.json 动态加载配置
"""

import json
import os
from pathlib import Path
from typing import Dict, Any, Optional

# 默认配置文件路径
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


class Config:
    """
    动态配置加载器
    所有硬编码的配置都从这里读取
    """
    
    _instance: Optional['Config'] = None
    _config: Dict[str, Any] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance
    
    def _load(self):
        """加载配置文件"""
        config_path = os.environ.get("DOCSYS_CONFIG", str(DEFAULT_CONFIG_PATH))
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
        except Exception as e:
            print(f"   ⚠️ 加载配置文件失败: {e}")
            self._config = self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "skill": {"name": "DocSys", "version": "2.0.0"},
            "spaces": {
                "home": {
                    "name": "家庭空间",
                    "storage": "/Users/kk/.openclaw/media/home/"
                },
                "work": {
                    "name": "办公空间", 
                    "storage": "/Users/kk/.openclaw/media/work/"
                }
            },
            "user_profile": {
                "sources": ["memory", "USER.md", "MEMORY.md"],
                "cache_ttl": 300,
                "defaults": {
                    "child_age": 7,
                    "style_preference": "卡通",
                    "lighting": "自然光",
                    "character_type": "小动物"
                }
            },
            "paths": {
                "workspace": "/Users/kk/.openclaw/workspace",
                "media": "/Users/kk/.openclaw/media/",
                "memory": "/Users/kk/.openclaw/workspace/memory/"
            }
        }
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项
        
        Args:
            key: 配置路径，如 "spaces.home.storage" 或 "user_profile.defaults.child_age"
            default: 默认值
        
        Returns:
            配置值
        """
        keys = key.split(".")
        value = self._config
        
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        
        return value
    
    def get_space(self, space: str) -> Dict[str, Any]:
        """获取空间配置"""
        return self.get(f"spaces.{space}", {})
    
    def get_user_defaults(self) -> Dict[str, Any]:
        """获取用户默认画像"""
        return self.get("user_profile.defaults", {
            "child_age": 7,
            "style_preference": "卡通",
            "lighting": "自然光",
            "character_type": "小动物"
        })
    
    def reload(self):
        """重新加载配置"""
        self._load()


# 全局配置实例
config = Config()
