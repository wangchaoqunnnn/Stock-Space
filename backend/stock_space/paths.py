"""运行期路径解析。

**本模块是"禁止硬编码绝对地址"这一约束的唯一落点。**
所有需要落盘的目录都由本文件的 ``__file__`` 逐级回溯推导, 因此整个项目
可以被 clone / 解压到任意目录(``/root``、``/opt``、``D:\\...``)后直接运行。

可用环境变量覆盖(便于容器把数据卷挂到别处):
  * ``SS_HOME``       —— 项目根目录 (默认: 本文件上溯 4 级)
  * ``SS_DATA_DIR``   —— 运行数据目录 (默认: ``<root>/data``)
  * ``SS_LOG_DIR``    —— 日志目录 (默认: ``<root>/logs``)
  * ``SS_CONFIG_DIR`` —— 配置文件目录 (默认: ``<root>/config``)
  * ``SS_CACHE_DIR``  —— 磁盘缓存目录 (默认: ``<data>/cache``)
"""

from __future__ import annotations

import os
from pathlib import Path

#: backend/stock_space/paths.py -> backend/stock_space -> backend -> <项目根>
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return default


ROOT_DIR: Path = _env_path("SS_HOME", _DEFAULT_ROOT)
BACKEND_DIR: Path = ROOT_DIR / "backend"
PACKAGE_DIR: Path = Path(__file__).resolve().parent

CONFIG_DIR: Path = _env_path("SS_CONFIG_DIR", ROOT_DIR / "config")
DATA_DIR: Path = _env_path("SS_DATA_DIR", ROOT_DIR / "data")
LOG_DIR: Path = _env_path("SS_LOG_DIR", ROOT_DIR / "logs")
CACHE_DIR: Path = _env_path("SS_CACHE_DIR", DATA_DIR / "cache")

#: 前端静态资源目录(零构建, 直接由后端托管)
WEB_DIR: Path = PACKAGE_DIR / "web"

#: SQLite 数据库文件
DB_PATH: Path = DATA_DIR / "stock_space.sqlite3"

#: 用户设置(API Key / Webhook / 手工输入源地址等)落盘位置 —— 不入版本库
RUNTIME_SETTINGS_PATH: Path = DATA_DIR / "runtime_settings.json"

#: 用户自定义的上游地址覆盖表
SOURCES_OVERRIDE_PATH: Path = CONFIG_DIR / "sources.local.toml"

#: 默认的源目录
SOURCES_DEFAULT_PATH: Path = CONFIG_DIR / "sources.toml"

#: 默认的规则/参数目录
RULES_DEFAULT_PATH: Path = CONFIG_DIR / "rules.toml"


def ensure_dirs() -> None:
    """启动时创建所有必需目录(幂等)。"""
    for path in (DATA_DIR, LOG_DIR, CACHE_DIR, CONFIG_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:  # 只读文件系统等极端情况: 不阻断启动
            pass


def relative_to_root(path: Path) -> str:
    """把绝对路径转成相对项目根的 POSIX 字符串 —— 用于对外展示, 避免泄露服务器目录。"""
    try:
        return path.resolve().relative_to(ROOT_DIR).as_posix()
    except (ValueError, OSError):
        return path.name


__all__ = [
    "ROOT_DIR",
    "BACKEND_DIR",
    "PACKAGE_DIR",
    "CONFIG_DIR",
    "DATA_DIR",
    "LOG_DIR",
    "CACHE_DIR",
    "WEB_DIR",
    "DB_PATH",
    "RUNTIME_SETTINGS_PATH",
    "SOURCES_OVERRIDE_PATH",
    "SOURCES_DEFAULT_PATH",
    "RULES_DEFAULT_PATH",
    "ensure_dirs",
    "relative_to_root",
]
