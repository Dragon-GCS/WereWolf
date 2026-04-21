"""预设配置加载"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"


def _load_role_map() -> dict:
    """读取 roles.yml，返回 {role_name: raw_dict} 映射"""
    roles_path = CONFIG_DIR / "roles.yml"
    if not roles_path.exists():
        raise FileNotFoundError(roles_path)
    with roles_path.open(encoding="utf-8") as f:
        return {role["name"]: role for role in yaml.safe_load(f).get("roles", [])}


def load_preset(name: str) -> dict:
    """按名称（不含.yml）加载预设配置，自动从 roles.yml 注入角色定义"""
    path = CONFIG_DIR / f"{name}.yml"
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if "roles" not in config:
        role_map = _load_role_map()
        used = dict.fromkeys(config.get("roster", []))  # 保持顺序去重
        config["roles"] = [role_map[r] for r in used if r in role_map]

    return config


def list_presets() -> list:
    """扫描 config/ 目录，返回所有预设的名称和描述"""
    presets = []
    for p in sorted(CONFIG_DIR.glob("*.yml")):
        name = p.stem
        if name == "roles":
            continue
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
            presets.append(
                {
                    "name": name,
                    "description": cfg["description"],
                    "player_count": len(cfg["roster"]),
                }
            )
        except Exception:
            logger.warning("解析预设文件失败: %s", p)
    return presets
