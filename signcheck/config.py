import json
import os
from typing import Dict, Any, Optional

DEFAULT_CONFIG: Dict[str, Any] = {
    "sort_by": "id",
    "sort_order": "asc",
    "export_path": "reconcile_result.csv",
}

VALID_CONFIG_KEYS = set(DEFAULT_CONFIG.keys())

SORT_BY_CHOICES = {"id", "status"}
SORT_ORDER_CHOICES = {"asc", "desc"}


def _resolve_config_dir() -> str:
    env_dir = os.environ.get("SIGNCHECK_DB_DIR")
    if env_dir:
        return os.path.join(env_dir, ".signcheck")
    return os.path.join(os.getcwd(), ".signcheck")


def _get_config_path() -> str:
    return os.path.join(_resolve_config_dir(), "config.json")


def load_config() -> Dict[str, Any]:
    config_path = _get_config_path()
    if not os.path.exists(config_path):
        return dict(DEFAULT_CONFIG)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        for key, value in user_config.items():
            if key in DEFAULT_CONFIG:
                merged[key] = value
        return merged
    except (json.JSONDecodeError, IOError):
        return dict(DEFAULT_CONFIG)


def save_config(config: Dict[str, Any]) -> None:
    config_dir = _resolve_config_dir()
    os.makedirs(config_dir, exist_ok=True)
    config_path = _get_config_path()
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def set_config(key: str, value: Any) -> Dict[str, Any]:
    if key not in VALID_CONFIG_KEYS:
        raise ValueError(f"无效配置项「{key}」，可选值：{', '.join(sorted(VALID_CONFIG_KEYS))}")
    if key == "sort_by" and value not in SORT_BY_CHOICES:
        raise ValueError(f"sort_by 可选值：{', '.join(sorted(SORT_BY_CHOICES))}")
    if key == "sort_order" and value not in SORT_ORDER_CHOICES:
        raise ValueError(f"sort_order 可选值：{', '.join(sorted(SORT_ORDER_CHOICES))}")
    if key == "export_path" and not value:
        raise ValueError("export_path 不能为空")
    config = load_config()
    config[key] = value
    save_config(config)
    return config


def reset_config() -> Dict[str, Any]:
    config_path = _get_config_path()
    if os.path.exists(config_path):
        os.remove(config_path)
    return dict(DEFAULT_CONFIG)


def get_config(key: str) -> Optional[Any]:
    config = load_config()
    return config.get(key)
