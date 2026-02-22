"""
统一日志模块

提供全局配置的日志记录器，替代 print() 输出。
"""

import logging
import sys
from pathlib import Path

# 日志格式
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 全局配置标志
_configured = False


def setup_logging(
    level: int = logging.INFO,
    log_file: str = None,
    format_string: str = LOG_FORMAT
) -> None:
    """
    配置全局日志

    Args:
        level: 日志级别 (默认 INFO)
        log_file: 日志文件路径 (可选)
        format_string: 日志格式
    """
    global _configured

    if _configured:
        return

    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding='utf-8'))

    logging.basicConfig(
        level=level,
        format=format_string,
        datefmt=DATE_FORMAT,
        handlers=handlers
    )

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的日志记录器

    Args:
        name: 模块名称 (建议使用 __name__)

    Returns:
        配置好的 Logger 实例
    """
    # 确保 logging 已配置
    if not _configured:
        setup_logging()

    return logging.getLogger(name)


# 模块级日志记录器（用于本模块）
_logger = None


def _get_module_logger() -> logging.Logger:
    """获取模块日志记录器"""
    global _logger
    if _logger is None:
        _logger = get_logger("utils.logger")
    return _logger
