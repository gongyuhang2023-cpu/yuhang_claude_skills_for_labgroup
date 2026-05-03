# -*- coding: utf-8 -*-
"""
配置管理器
管理用户配置的持久化存储（姓名、API Key、保存位置等）
配置文件存放在 %APPDATA%/WeeklySummary/config.json
"""

import json
import os
from pathlib import Path


class ConfigManager:
    CONFIG_DIR = Path(os.environ.get('APPDATA', '')) / "WeeklySummary"
    CONFIG_PATH = CONFIG_DIR / "config.json"

    DEFAULTS = {
        "user_name_cn": "",
        "user_name_en": "",
        "api_key": "",
        "model": "deepseek-v4-pro",
        "save_location": "",
        "max_tokens": 4000,
        "temperature": 1.0,
    }

    def load(self) -> dict:
        config = dict(self.DEFAULTS)
        if self.CONFIG_PATH.exists():
            try:
                with open(self.CONFIG_PATH, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                config.update(saved)
            except (json.JSONDecodeError, OSError):
                pass
        return config

    def save(self, config: dict):
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    def get_api_key(self) -> str:
        config = self.load()
        key = config.get("api_key", "")
        if key:
            return key
        return os.environ.get('DEEPSEEK_API_KEY', '')

    def get_save_location(self) -> Path:
        config = self.load()
        loc = config.get("save_location", "")
        if loc and Path(loc).is_dir():
            return Path(loc)
        return Path(os.path.expanduser("~/Desktop"))

    def get_model(self) -> str:
        return self.load().get("model", self.DEFAULTS["model"])

    def is_configured(self) -> bool:
        config = self.load()
        return bool(config.get("user_name_cn")) and bool(config.get("api_key"))
