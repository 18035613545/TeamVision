# -*- coding: utf-8 -*-
"""B4 更新提示（实现方案 §6）：GitHub Releases 最新 tag 探测 + 语义化比对 + 24h 缓存。

纯提示器，不做自动更新（09-17 §5 裁决仍有效）。网络层只用标准库 urllib，
任何失败静默（不弹窗、不写 ERROR）；结果缓存 24 小时到本地数据目录，
避免每次启动都打 GitHub API。
"""

import json
import os
import threading
import time
import urllib.request

import logger as logger_mod
from common import APP_VERSION, data_dir

log = logger_mod.get_logger()

REPO = "18035613545/TeamVision"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
CACHE_NAME = "update_check.json"
CACHE_TTL_S = 24 * 3600.0
HTTP_TIMEOUT_S = 5.0


def enabled(cfg):
    """配置开关：update_check.enabled，**默认 False**（缺键/缺段/坏值一律视为关闭）。

    dist-004（2026-09-18）：更新检查默认开启时，每 24 小时都会向 api.github.com
    暴露使用者 IP 与"正在使用本程序"这一事实——与第 131 条默认安全口径相悖，
    且没有任何 UI 开关。改为 opt-in：需要更新提示的用户在共享端控制台勾选，
    或手改 config.json 的 update_check.enabled。
    """
    sec = cfg.get("update_check") if isinstance(cfg, dict) else None
    if not isinstance(sec, dict):
        return False
    return bool(sec.get("enabled", False))


def parse_version(text):
    """'v1.5.0'/'1.5' -> (1, 5, 0)/(1, 5)；任一段非纯数字返回 None。"""
    if not isinstance(text, str):
        return None
    s = text.strip().lstrip("vV")
    if not s:
        return None
    parts = s.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    out = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    return tuple(out)


def is_newer(latest, current=APP_VERSION):
    """语义化比较（补零对齐）；任何一侧解析失败按"无更新"处理，绝不误报。"""
    a = parse_version(latest)
    b = parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def _read_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fetch_tag(opener):
    with opener(API_URL, timeout=HTTP_TIMEOUT_S) as resp:
        raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    data = json.loads(raw)
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) else None


def check_update(cfg=None, now=None, opener=None, cache_path=None):
    """返回提示文案（有新版）或 None（无更新/失败静默）。opener 供测试注入。"""
    if not enabled(cfg):
        return None
    if opener is None:
        opener = urllib.request.urlopen
    if now is None:
        now = time.time()
    if cache_path is None:
        base = data_dir()
        if not base:
            return None          # dist-005：无可写数据目录时不检查（旧实现拼接 None）
        cache_path = os.path.join(base, CACHE_NAME)
    cached = _read_cache(cache_path)
    tag = cached.get("tag") if cached else None
    checked_at = cached.get("checked_at") if cached else None
    # dist-006（2026-09-18）：年龄必须为**非负**才算新鲜——时钟回拨/缓存被写成未来
    # 时刻时 (now - checked_at) 恒负，旧判据永远"新鲜"→ 该机器永久不再检查更新
    age = (now - checked_at) if isinstance(checked_at, (int, float)) else None
    fresh = (isinstance(tag, str) and age is not None and 0 <= age < CACHE_TTL_S)
    if not fresh:
        try:
            fetched = _fetch_tag(opener)
        except Exception as e:  # 断网/DNS/限流一律降级用旧缓存
            # dist-007（2026-09-18）：失败必须留痕（默认 INFO 级别可见）——
            # 旧实现只 log.debug，断网/代理/限流在日志里一个字都看不到
            log.info("更新检查失败（不影响使用）：%s", e)
            fetched = None
        if fetched is not None:
            tag = fetched
            checked_at = now
            try:
                # dist-005：临时名带 PID+线程号（host/viewer 同机运行时不再互踩）；
                # replace 失败重试一次（Windows 上撞读即 PermissionError）
                tmp = "%s.%d.%d.tmp" % (cache_path, os.getpid(),
                                        threading.get_ident())
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"checked_at": checked_at, "tag": tag}, f)
                try:
                    os.replace(tmp, cache_path)
                except PermissionError:
                    time.sleep(0.05)
                    os.replace(tmp, cache_path)
            except OSError as e:
                log.info("更新检查缓存写入失败（忽略）：%s", e)
        elif not (isinstance(tag, str) and is_newer(tag)):
            return None
    return ("有新版 %s 可下载（GitHub Releases）" % tag) if is_newer(tag) else None


def start_background_check(cfg, post_msg):
    """启动后台检查线程；发现新版时调用 post_msg(text)（调用方负责回 UI 主线程）。

    返回 Thread 或 None（开关关闭时不启动）。线程内任何异常都不外抛。
    """
    if not enabled(cfg):
        return None

    def work():
        try:
            msg = check_update(cfg)
        except Exception as e:
            log.info("更新检查线程异常（不影响使用）：%s", e)
            return
        if msg:
            try:
                post_msg(msg)
            except Exception:
                pass

    t = threading.Thread(target=work, name="update-check", daemon=True)
    t.start()
    return t
