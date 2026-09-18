# -*- coding: utf-8 -*-
"""B2（第 134 条）：两端共用的诊断包导出。

build_report_zip(dest_dir, *, config, logs_dir, extra_text) 把
「脱敏后的 config.json + 当日/上一日 app_*.log + report.txt」
打成一个 zip，供出问题时一次性发给支持者。

纯标准库实现（zipfile 为本仓唯一新依赖点，不动 requirements.txt）。
脱敏是硬约束：任何名为 token 的配置值一律替换为 "***"
（这是脱敏规则，不是 frp 功能开发；accounts 本就在配置文件外，无需处理）。
"""
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

from common import APP_NAME, APP_VERSION

_LOG_DATE_RE = re.compile(r"^app_(\d{8})")
#: 递归脱敏的键名（frp.token 及任何同名凭据）
_MASK_KEYS = {"token", "password"}


def redact(obj):
    """深拷贝并把 _MASK_KEYS 命中的字符串值替换为 '***'。"""
    if isinstance(obj, dict):
        return {k: ("***" if k in _MASK_KEYS else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def recent_log_files(logs_dir, days=2, now=None):
    """挑出最近 days 天（含今天）的 app_YYYYMMDD*.log；目录缺失返回空。"""
    out = []
    if not logs_dir or not os.path.isdir(logs_dir):
        return out
    base = now if now is not None else time.time()
    wanted = {time.strftime("%Y%m%d", time.localtime(base - i * 86400))
              for i in range(days)}
    try:
        names = sorted(os.listdir(logs_dir))
    except OSError:
        return out
    for name in names:
        m = _LOG_DATE_RE.match(name)
        if m and m.group(1) in wanted:
            p = os.path.join(logs_dir, name)
            if os.path.isfile(p):
                out.append(p)
    return out


def build_report_text(config, extra_text=""):
    """report.txt 内容：版本/Python/监听形态/准入与注册策略等静态项。

    在线观众数、最近自检结论属运行时信息，由调用方经 extra_text 追加；
    延迟 P95 自 E4（§11.4）起由观看端统计，随观众各自诊断包的频道行走。
    """
    host_cfg = (config or {}).get("host") if isinstance(config, dict) else None
    if not isinstance(host_cfg, dict):
        host_cfg = {}
    auth = host_cfg.get("auth") if isinstance(host_cfg.get("auth"), dict) else {}
    listen_host = host_cfg.get("listen_host")
    if not isinstance(listen_host, str) or not listen_host.strip():
        listen_form = "0.0.0.0（全网卡）"
    elif listen_host in ("0.0.0.0", "::"):
        listen_form = "%s（全网卡）" % listen_host
    else:
        listen_form = "%s（回环/指定地址）" % listen_host
    lines = [
        "%s 诊断报告（生成于 %s）" % (APP_NAME, time.strftime("%Y-%m-%d %H:%M:%S")),
        "版本: %s" % APP_VERSION,
        "Python: %s" % sys.version.replace("\n", " "),
        "监听形态: %s:%s" % (listen_form, host_cfg.get("port", "")),
        "账户准入: %s" % ("开启" if auth.get("enabled", False) else "关闭"),
        "注册策略: %s" % auth.get("allow_register", "open"),
        "公网地址: %s" % (host_cfg.get("frp", {}).get("public_addr", "")
                          if isinstance(host_cfg.get("frp"), dict) else ""),
        "延迟 P95: 由观看端统计（E4 已落地：见观众诊断包内「延迟 P50/P95」频道行）",
    ]
    if extra_text:
        lines.append("")
        lines.append(str(extra_text).rstrip())
    return "\n".join(lines) + "\n"


def build_report_zip(dest_dir, *, config, logs_dir=None, extra_text=""):
    """生成诊断包，返回 zip 路径。日志缺失/单个文件读失败均不抛异常。"""
    os.makedirs(dest_dir, exist_ok=True)
    dest = Path(dest_dir) / ("TeamVision-diag-%s.zip"
                             % time.strftime("%Y%m%d-%H%M%S"))
    tmp = Path(str(dest) + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("report.txt",
                        build_report_text(config, extra_text=extra_text))
            zf.writestr("config.json",
                        json.dumps(redact(config or {}), ensure_ascii=False,
                                   indent=2))
            for p in recent_log_files(logs_dir):
                try:
                    zf.write(p, "logs/" + os.path.basename(p))
                except OSError:
                    continue  # 正被轮转/占用：跳过单个文件不影响出包
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return dest
