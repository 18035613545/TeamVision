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
import re
import sys
import tempfile
import threading
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
#: dist-011（2026-09-20）：per-PID 之后"天数"不再是增长的上界（每次启动一个新 PID，
#: 单日可产生几十份，每份还能滚出 .1~.5 五个备份）。这两个常量给出**绝对**上界。
_KEEP_FILES = 120
_KEEP_TOTAL_BYTES = 64 * 1024 * 1024

#: C3：日志文件名里的日期前缀（app_YYYYMMDD[_pidN][.N].log）。
#: 用正则严格取日期，避免 app_debug.log 之类的非日期名被切片成假"日期键"而挤掉真日志。
_LOG_DATE_RE = re.compile(r"^app_(\d{8})")

#: 是否已完成 handler 初始化（幂等标志）
_initialized = False
#: cfg-003（2026-09-18）：初始化互斥锁（防双线程各挂一套 handler）
_init_lock = threading.Lock()


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
        """清理超出保留范围的日志（含大小滚动备份 .1/.2 与各 PID 文件）。

        dist-011（2026-09-20）：旧实现两处不足——
        ① `keep_keys = sorted(groups)[-5:]` 保留的是"目录里现存的后 5 个**日期组**"，
           而不是"距今 5 天"。本机长期只开一天机器再停几天时，最旧那组哪怕已经
           9 天前也照样整组保留（本机实测现存 20260911/17/18/19/20 五组，最旧一组
           是 9 天前，实删 0 个文件）；
        ② 组内不限量：per-PID 让文件数从"每天一份"变成"每次启动一份"，
           docstring 声称"避免磁盘无限增长"，实际增长维度换成了启动次数
           （本机 logs/ 已 191 个文件，2026-09-11 单日 69 个且 PID 互不相同）。
        现在：按"距今 N 天"筛日期组，保留组内再按**总文件数/总字节**二次裁剪，
        最旧的先删。

        C3（2026-09-11）：日期键必须是**真正的 8 位日期**。原实现用 `name[4:12]` 直接
        切片，目录里若存在 `app_debug.log` 这类非日期命名文件，其"日期"键 `"debug.lo"`
        在字典序上大于任何 `"2026xxxx"`，于是被保留在 keep_keys 里，**某个真实日期组
        反被挤出 5 日窗口删除**（日志丢失）。改用正则严格匹配，不匹配的文件不参与分组
        （也就不会被删）。
        """
        try:
            files = [f for f in os.listdir(self._log_dir)
                     if f.startswith("app_") and ".log" in f]
            groups = {}  # YYYYMMDD -> [文件名列表]
            for name in files:
                m = _LOG_DATE_RE.match(name)
                if not m:
                    continue  # app_debug.log 之类的非日期文件：不分组、不清理
                groups.setdefault(m.group(1), []).append(name)
            # ① 按"距今 _KEEP_DAYS 天"筛，而不是"现存的最后 5 组"
            today = time.strftime("%Y%m%d")
            try:
                cutoff = time.strftime(
                    "%Y%m%d",
                    time.localtime(time.time() - (_KEEP_DAYS - 1) * 86400))
            except Exception:
                cutoff = today
            for date in list(groups):
                if date < cutoff:
                    for name in groups.pop(date):
                        try:
                            os.remove(os.path.join(self._log_dir, name))
                        except OSError:
                            pass
            # ② 保留组内再按总量裁剪：最旧日期、文件名字序最小的先删
            survivors = []
            for date in sorted(groups):
                for name in sorted(groups[date]):
                    survivors.append((date, name))
            total_bytes = 0
            sized = []
            for date, name in survivors:
                try:
                    st = os.stat(os.path.join(self._log_dir, name))
                except OSError:
                    continue
                sized.append((date, name, st.st_size))
                total_bytes += st.st_size
            while (len(sized) > _KEEP_FILES or total_bytes > _KEEP_TOTAL_BYTES) \
                    and sized:
                date, name, size = sized.pop(0)
                try:
                    os.remove(os.path.join(self._log_dir, name))
                    total_bytes -= size
                except OSError:
                    sized.insert(0, (date, name, size))
                    break
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

    auth-013（2026-09-18）：短串按长度自适应——旧实现 9~16 位串（常见 API token
    长度）只遮 1~4 个字符，日志里近乎明文，还给人"已脱敏"的错觉。
    <=8 位全遮；<=24 位只留尾部 2 位；其余保持前后各 keep 位。
    """
    if s is None:
        return "****"
    s = str(s)
    if len(s) <= 8:
        return "****"
    if len(s) <= 24:
        return "****" + s[-2:]
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
            # cfg-002（2026-09-18）：探针名加线程号——只用 PID 时同进程两个线程
            # 首调互删探针，误判"不可写"使日志整体回退 %TEMP%
            probe = os.path.join(d, ".wprobe_%d_%d"
                                 % (os.getpid(), threading.get_ident()))
            with open(probe, "w", encoding="utf-8") as f:
                f.write("x")
            try:
                os.remove(probe)
            except OSError:
                pass   # 被他人清掉也算本次探测成功（目录确实可写）
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
    cfg = cfg or {}
    logs_cfg = cfg.get("logs") or {}
    level = _resolve_level(logs_cfg.get("level", "info"))

    logger = logging.getLogger(LOGGER_NAME)

    # cfg-003（2026-09-18）：初始化必须加锁——旧实现 `_initialized` 无锁，两个线程
    # 首次同时调用会各挂一套 file handler（同文件重复写 + 滚动改名 WinError 32）
    with _init_lock:
        return _setup_logger_locked(logger, level)


def _setup_logger_locked(logger, level):
    """setup_logger 的加锁实现体（cfg-003：初始化只做一次）。"""
    global _initialized
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
