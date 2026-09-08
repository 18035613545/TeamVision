# -*- coding: utf-8 -*-
"""全局日志模块（Task 14：完整日志系统）。

提供统一的日志初始化 setup_logger、惰性获取 get_logger 以及
敏感信息打码 mask_secret，供 host.py / viewer.py 共同使用。

日志写入可写数据目录（common.data_dir()，见第 102 条）/logs/app_YYYYMMDD_pidNNN.log
（5MB 滚动、保留 5 份），同时输出到控制台（stdout）。仅依赖标准库 logging / os / sys / time。
"""

import logging
import logging.handlers
import os
import sys
import tempfile
import time

from common import data_dir, exe_dir

#: 全局日志器名称
LOGGER_NAME = "sakura"

#: 配置字符串 -> logging 级别 的映射（大小写不敏感）
_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}

#: 文件日志单文件上限（5MB）
_MAX_BYTES = 5 * 1024 * 1024
#: 滚动归档保留的历史文件数（按大小滚动时，每个日期文件保留的份数）
_BACKUP_COUNT = 5
#: 按日期保留的历史日志文件数（app_YYYYMMDD.log，超出部分在跨天时清理）
_KEEP_DAYS = 5

#: 是否已完成 handler 初始化（幂等标志）
_initialized = False


class _DateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """按日期滚动的文件日志 handler：跨午夜自动切换到新日期的日志文件。

    在 RotatingFileHandler（5MB 大小滚动）基础上增加日期切换：
    - 每次 emit 检查当前日期，跨天时关闭旧文件并打开 app_新日期.log；
    - 切换时清理超出保留天数的历史日期日志，避免磁盘无限增长。
    """

    def __init__(self, log_dir, max_bytes=_MAX_BYTES, backup_count=_BACKUP_COUNT):
        self._log_dir = log_dir
        self._today = time.strftime("%Y%m%d")
        super().__init__(
            self._filename(self._today),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        self._prune_old()

    def _filename(self, date):
        # 第 14 条：文件名含 PID，host 与 viewer 各写各的，避免争用同一文件
        # 导致 Windows 下滚动改名失败（WinError 32）后 stream=None 永不重开。
        return os.path.join(self._log_dir, "app_%s_pid%d.log" % (date, os.getpid()))

    def _prune_old(self):
        """清理超出保留天数的历史日志文件（含大小滚动备份 .1/.2 与各 PID 文件）。

        按日期分组（app_YYYYMMDD_pid*.log 及其备份属于同一日期组），只保留最新的
        KEEP_DAYS 天，其余整组删除——避免旧日期文件长期累积。兼容旧的无 PID 命名。
        """
        try:
            files = [f for f in os.listdir(self._log_dir)
                     if f.startswith("app_") and ".log" in f]
            groups = {}  # YYYYMMDD -> [文件名列表]
            for name in files:
                date = name[4:12]  # app_ 之后的 8 位日期
                groups.setdefault(date, []).append(name)
            keep_keys = set(sorted(groups)[-_KEEP_DAYS:])
            for date, names in groups.items():
                if date in keep_keys:
                    continue
                for name in names:
                    try:
                        os.remove(os.path.join(self._log_dir, name))
                    except OSError:
                        pass
        except OSError:
            pass

    def emit(self, record):
        today = time.strftime("%Y%m%d")
        if today != self._today:
            # 跨天：切换到新日期的文件（避免长驻进程午夜后仍写昨日日志）
            self._today = today
            try:
                self.close()
            except Exception:
                pass
            self.baseFilename = self._filename(today)
            try:
                self.stream = self._open()
            except Exception:
                self.stream = None
            self._prune_old()
        super().emit(record)


class _SafeStreamHandler(logging.StreamHandler):
    """控制台 handler：windowed exe 的 stdout 句柄可能无效，emit/flush 失败时静默忽略。"""

    def handleError(self, record):  # noqa: D401 - 有意吞掉所有输出错误
        # StreamHandler.emit 失败会回调这里打印 "--- Logging error ---"，
        # 对无效句柄静默处理，避免启动噪音
        pass

    def flush(self):
        try:
            super().flush()
        except (OSError, ValueError):
            pass


def mask_secret(s, keep=4):
    """敏感信息打码：保留前 keep 位与后 keep 位，中间用 **** 替换。

    字符串长度不足（<= keep*2）或为 None 时整体替换为 ****。
    """
    if s is None:
        return "****"
    s = str(s)
    keep = max(1, int(keep))
    if len(s) <= keep * 2:
        return "****"
    return s[:keep] + "****" + s[-keep:]


def _resolve_level(level_name):
    """把配置中的级别字符串解析为 logging 级别常量；非法或缺失时回退为 INFO。"""
    if not isinstance(level_name, str):
        return logging.INFO
    return _LEVEL_MAP.get(level_name.strip().lower(), logging.INFO)


def _writable_log_dir():
    """返回一个可写的日志目录（第 13 条 / 第 102 条）。

    安装到 Program Files 后非管理员运行时 exe_dir()/logs 不可写，原代码在 import 期
    os.makedirs 直接抛 PermissionError，窗口都不出现。这里依次尝试：
    可写数据目录（common.data_dir()，与 config.json/accounts.json 同一目录）
    → exe_dir()/logs → 临时目录/TeamVision-logs，对每个候选实际写一个探测文件验证可写，
    返回第一个可用目录；全不可写返回 None。

    第 102 条：改为优先跟随 common.data_dir()，让配置、账户、日志落在同一处，
    避免"配置在用户目录、日志在安装目录"这种更难解释的状态。
    """
    candidates = []
    base = data_dir()
    if base:
        candidates.append(os.path.join(base, "logs"))
    candidates.append(os.path.join(exe_dir(), "logs"))
    candidates.append(os.path.join(tempfile.gettempdir(), "TeamVision-logs"))
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".wprobe_%d" % os.getpid())
            with open(probe, "w", encoding="utf-8") as f:
                f.write("x")
            os.remove(probe)
            return d
        except OSError:
            continue
    return None


def setup_logger(cfg=None):
    """幂等初始化全局日志并返回名为 "sakura" 的 Logger。

    - 级别：读取 cfg["logs"]["level"]（默认 "info"），大小写不敏感；
      每次调用都会用 cfg 重新设置级别（允许重配）。
    - 日志目录：os.path.join(exe_dir(), "logs")，不存在则创建。
    - 文件 handler：logs/app_YYYYMMDD.log（RotatingFileHandler，
      maxBytes=5MB、backupCount=5、encoding="utf-8"），
      格式 "%(asctime)s [%(levelname)s] %(message)s"。
    - 控制台 handler：StreamHandler(sys.stdout)，
      格式 "[sakura] %(levelname)s: %(message)s"。
    - logger.propagate = False；已初始化时仅更新级别并返回。
    """
    global _initialized

    cfg = cfg or {}
    logs_cfg = cfg.get("logs") or {}
    level = _resolve_level(logs_cfg.get("level", "info"))

    logger = logging.getLogger(LOGGER_NAME)

    if not _initialized:
        logger.setLevel(level)
        logger.propagate = False

        # 日志目录：探测可写位置（exe_dir/logs 不可写时回退用户本地/临时目录，第 13 条）
        log_dir = _writable_log_dir()
        if log_dir is not None:
            # 文件 handler：按日期+PID 命名 + 5MB 大小滚动（跨午夜自动切新日期文件）
            try:
                file_handler = _DateRotatingFileHandler(log_dir)
                file_handler.setFormatter(
                    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
                )
                logger.addHandler(file_handler)
            except Exception:
                # 文件日志不可用（权限/占用）：降级为仅控制台，绝不让 import 期崩溃
                pass

        # 控制台 handler：windowed 打包（sys.stdout 为 None）时跳过，仅保留文件日志；
        # 从带管道的终端启动 windowed exe 时 stdout 句柄可能无效（flush 抛 OSError），一并吞掉
        if sys.stdout is not None:
            console_handler = _SafeStreamHandler(sys.stdout)
            console_handler.setFormatter(
                logging.Formatter("[sakura] %(levelname)s: %(message)s")
            )
            logger.addHandler(console_handler)

        _initialized = True
    else:
        # 已初始化：仅按新配置更新级别（允许重配）
        logger.setLevel(level)

    return logger


def get_logger():
    """返回全局 Logger；未初始化时按默认配置（info 级别）初始化。"""
    global _initialized
    if not _initialized:
        setup_logger(None)
    return logging.getLogger(LOGGER_NAME)
