# -*- coding: utf-8 -*-
"""B4 更新提示（实现方案 §6）：GitHub Releases 最新 tag 探测 + 语义化比对 + 24h 缓存。

**仍不自动下载/安装**（09-17 §5 裁决有效）：替用户静默换掉正在跑的 exe 会踩杀软与
反作弊的雷，且 onedir 形态的替换需要先停两个进程，做成"一键静默升级"收益远小于风险。
本轮（第 9 轮"部署与更新"）只把提示升级成**可执行入口**：文案里带 release 页面地址，
两端各给一个按钮调用 `open_url` 打开浏览器——用户从"看到有新版"到"拿到安装包"
之间不再需要自己去搜仓库。网络层只用标准库 urllib，任何失败静默（不弹窗、不写 ERROR）；
结果缓存 24 小时到本地数据目录，避免每次启动都打 GitHub API。

**第 10 轮：判定口径从"标签比本机新"改成"本机角色有比本机新的安装包"**（见
`check_release`）。单包时代 `releases/latest` 的 tag 就是唯一版本号；一旦共享端/观看端
拆包分别发版，最新那次发布只属于其中一端，另一端的 `latest` 会长期停在更旧的 tag 上，
按标签判定的话那端用户**从此收不到任何更新提示**。

**第 12 轮（2026-10-23）：这条口径已接线。** `check_update` 现在就是 `check_release`
的文案出口（传 role），两者共用同一份实现与同一份缓存：

- 缓存里存的是**原始发布列表**而不是判定结果——判定要按角色来，同一台机器上的
  共享端与观看端看到的是同一份列表、结论不同，把判定结果存进缓存会让两端互相
  把对方的缓存顶掉（每次都判定为"缓存过期"而重新打网络）。
- 取数从 `/releases/latest` 改成 `/releases?per_page=10`：拆包后要在一页里找出
  "本机角色最高的那次发布"，只取 latest 一条是找不出来的。
- 单包时代的发布（资产名里认不出 Host/Viewer）退回"最高标签"口径，与旧行为一致。
"""

import json
import os
import re
import threading
import time
import urllib.request
import webbrowser

import logger as logger_mod
from common import APP_VERSION, data_dir

log = logger_mod.get_logger()

REPO = "18035613545/TeamVision"
RELEASES_LIST_URL = "https://api.github.com/repos/%s/releases?per_page=10" % REPO
RELEASES_URL = "https://github.com/%s/releases" % REPO
#: 隧道服务商控制台（共享端「公网地址」旁的链接）。穿透本身是外部工具，
#: 这条只是把用户接下来要做的事（创建隧道 / 核对远端端口）变成一次点击。
PANEL_URL = "https://console.natfrp.com/"
CACHE_NAME = "update_check.json"
CACHE_TTL_S = 24 * 3600.0
HTTP_TIMEOUT_S = 5.0

#: 从提示文案里取回页面地址：两端按钮都靠它分流（有地址→打开浏览器；没有→旧行为）。
_URL_RE = re.compile(r"https?://[^\s，。）)]+")
#: 只允许交给浏览器打开 http(s)：文案来自远端 API，不允许 file:/javascript: 之类
#: （open_url 里再兜一次，extract_url 这一层只做提取）。
_SAFE_SCHEMES = ("http://", "https://")


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


def release_url():
    """固定的 Releases 页面：API 没给 html_url、或缓存是旧格式时的回退。"""
    return RELEASES_URL


def hint_headline(text):
    """提示文案的"人话部分"（不含页面地址），给单行状态栏用。

    文案形状固定为 "<一句话> - <URL>"（见 check_update）；按最后一个 " - " 切开，
    而不是 split("-")——tag 或句子里的连字符不该把地址提前切开。
    """
    if not isinstance(text, str):
        return ""
    head, sep, tail = text.rpartition(" - ")
    if not sep:
        return text.strip()
    if tail.lower().startswith(_SAFE_SCHEMES):
        return head.strip()
    # 切成两半但右半不是地址：整段原样留给状态栏，不替用户猜哪半边是人话
    return text.strip()


def extract_url(text):
    """从提示文案里取回第一个 http(s) 地址；取不到返回 None（调用方回落旧行为）。"""
    if not isinstance(text, str):
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def open_url(url):
    """用系统默认浏览器打开 http(s) 地址；成功返回 True。

    拒绝非 http(s) 协议（文案里的地址来自远端 API，不能被诱导成 file:/javascript:），
    浏览器拉不起来时返回 False 而不是抛——调用方在 Tk 回调里，异常只会被静默吞掉。
    """
    if not isinstance(url, str) or not url.lower().startswith(_SAFE_SCHEMES):
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception as e:
        log.info("打开更新页面失败（不影响使用）：%s", e)
        return False


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


def _fetch_releases(opener):
    """取最近若干次发布（列表）；失败向上抛，由调用方按"降级用旧缓存"处理。

    返回值统一是 **list**：`/releases` 正常返回数组；万一拿到单条对象
    （旧接口形态）也包成单元素列表，调用方不必再分叉。
    """
    with opener(RELEASES_LIST_URL, timeout=HTTP_TIMEOUT_S) as resp:
        raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return data if isinstance(data, list) else []


def _release_list(opener, cache_path, now):
    """取发布列表（带 24h 缓存）；返回 (releases, fetched)。

    缓存里存的是**原始列表**，不是"判定结果"——判定要按角色来（拆包后两端
    看到的是同一份列表、结论不同），把判定结果存进缓存会让共用一台机器的
    共享端与观看端互相覆盖缓存（每次都判定为"缓存过期"而重新打网络）。

    网络失败时用**旧缓存**继续顶着（哪怕已过期）：断网时"仍然告诉用户有新版"
    比"装作没事"有用。反之若连旧缓存都没有，返回空列表，由调用方保持旧文案。
    """
    cached = _read_cache(cache_path)
    checked_at = cached.get("checked_at") if cached else None
    age = (now - checked_at) if isinstance(checked_at, int | float) else None
    # dist-006（2026-09-18）：年龄必须为**非负**才算新鲜——时钟回拨/缓存被写成
    # 未来时刻时 (now - checked_at) 恒负，旧判据永远"新鲜"→ 该机器永久不再检查更新
    fresh = isinstance(age, int | float) and 0 <= age < CACHE_TTL_S
    if fresh:
        releases = cached.get("releases")
        if isinstance(releases, list):
            return releases, False
        # 兼容旧格式缓存（{"tag":..., "page":..., "checked_at":...}）：它只记了一个
        # tag，够还原成"一条没有资产的发布"——角色判定会退回标签口径，与旧行为一致。
        # 不做这层兼容的话，升级后的第一次检查会白白打一次网络。
        if isinstance(cached.get("tag"), str):
            old = {"tag_name": cached["tag"], "html_url": cached.get("page")}
            return [old], False
    try:
        releases = _fetch_releases(opener)
    except Exception as e:  # 断网/DNS/限流一律降级用旧缓存
        # dist-007（2026-09-18）：失败必须留痕（默认 INFO 级别可见）——
        # 旧实现只 log.debug，断网/代理/限流在日志里一个字都看不到
        log.info("更新检查失败（不影响使用）：%s", e)
        old = cached.get("releases") if cached else None
        if isinstance(old, list):
            return old, False
        # 旧格式缓存（只有 tag/page）同样能顶：断网时"仍然告诉用户有新版"
        # 比"装作没事"有用。
        if isinstance(cached.get("tag") if cached else None, str):
            return [{"tag_name": cached["tag"], "html_url": cached.get("page")}], False
        return [], False
    if not releases:
        return [], False
    try:
        # dist-005：临时名带 PID+线程号（host/viewer 同机运行时不再互踩）；
        # replace 失败重试一次（Windows 上撞读即 PermissionError）
        tmp = "%s.%d.%d.tmp" % (cache_path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"checked_at": now, "releases": releases}, f)
            try:
                os.replace(tmp, cache_path)
            except PermissionError:
                time.sleep(0.05)
                os.replace(tmp, cache_path)
        except OSError as e:
            # cfg-006（2026-09-20）：临时名唯一（PID+线程），写失败后旧实现
            # 只 info、不清理 → 磁盘永久留下一个不会再被覆盖回收的残渣
            # （同族的 _persist_upgrade 落的是含明文 frp token 的整份配置，
            # 三处清理口径要一致）。replace 成功后 tmp 已不存在，remove 静默。
            log.info("更新检查缓存写入失败（忽略）：%s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
    except Exception as e:
        log.info("更新检查缓存写入异常（忽略）：%s", e)
    return releases, True


def check_update(cfg=None, now=None, opener=None, cache_path=None, role="host"):
    """返回提示文案（有新版）或 None（无更新/失败静默）。opener 供测试注入。

    第 12 轮（2026-10-23）：判定口径改为"**本机这一端**有没有更新的安装包"
    （`check_release`），而不是"最新标签是否比本机新"——拆包发版后
    `releases/latest` 只会是其中一端的新版本，另一端的 latest 停在更旧的 tag 上，
    按标签判定的话那端用户**从此收不到更新提示**。单包时代两者等价。

    role 决定"本机这一端"是谁：共享端传 "host"、观看端传 "viewer"。
    """
    if not enabled(cfg):
        return None
    info = check_release(role, current=APP_VERSION, now=now, opener=opener,
                         cache_path=cache_path)
    if not info.get("update"):
        return None
    # 页面地址随文案一起给 UI：缺 html_url 时由 _release_info 兜到 Releases 页，
    # 用户至少能落到"最近的正式发布"那一屏。tag 来自远端（GitHub 允许 tag 含空格），
    # 用 ' - ' 隔开，hint_headline 按最后一个 " - " 切开，句子里的连字符不受影响。
    return "有新版 %s 可下载（GitHub Releases） - %s" % (info["tag"], info["url"])


# ---------------------------------------------------------------------------
# 第 10 轮：按角色拆包的发布判定（见模块 docstring）
# ---------------------------------------------------------------------------

#: 安装包文件名里的角色标记：与 installer.iss 的产物名一致（Host→共享端，Viewer→观看端）。
#: 按**独立词**匹配而不是子串：`frpc-windows-amd64.zip` 里也含 "rc"，子串匹配会把
#: 无关资产错认成某一端的安装包。
_ROLE_TOKENS = {"host": re.compile(r"(?<![a-z])host(?![a-z])", re.I),
                "viewer": re.compile(r"(?<![a-z])viewer(?![a-z])", re.I)}


def role_of_asset_name(name):
    """从安装包文件名推断属于哪一端；认不出返回 None（不替资产猜归属）。"""
    if not isinstance(name, str):
        return None
    for role, pattern in _ROLE_TOKENS.items():
        if pattern.search(name):
            return role
    return None


def _assets(release):
    items = release.get("assets") if isinstance(release, dict) else None
    return [a for a in items if isinstance(a, dict)] if isinstance(items, list) else []


def _release_info(role, current, best_tag, release, asset):
    """把一条发布整理成 UI 需要的最小结构（tag / 版本 / 页面 / 直链）。"""
    version = best_tag.lstrip("vV")
    page = release.get("html_url")
    if not (isinstance(page, str) and page.lower().startswith(_SAFE_SCHEMES)):
        # 回退到固定 Releases 页（不是按 tag 拼出来的 tag 页）：tag 页在 tag 含
        # 斜杠等怪字符时拼不出合法地址，而固定页永远打得开——用户至少能落到
        # "最近的正式发布"那一屏（第 9 轮的既定口径）。
        page = RELEASES_URL
    download = None
    match_asset = None
    if asset is not None:
        # 文件名带版本号时按名字精确找；找不到就是没有（不拿对端的包充数——
        # 给用户一个装错的安装包，比只给页面链接更糟）
        for a in _assets(release):
            if a.get("name") == asset:
                match_asset = a
                break
    else:
        # 没有已知文件名（单包时代的发布）：只取认不出角色的资产（旧版安装包名里
        # 没有 Host/Viewer）。对端角色的资产绝不拿来充数——给用户一个装错的安装包，
        # 比只给页面链接更糟
        for a in _assets(release):
            if role_of_asset_name(a.get("name")) is None:
                match_asset = a
                break
    if match_asset is not None:
        url = match_asset.get("browser_download_url")
        if isinstance(url, str) and url.lower().startswith(_SAFE_SCHEMES):
            download = url
    return {"update": True, "version": version, "tag": best_tag, "url": page,
            "download_url": download, "current": current, "source": "releases"}


def _select_release(role, releases, current):
    """从发布列表里挑出"本机这一端"的最新版本；纯函数，不碰网络与磁盘。

    目标版本的选择：
      1. 在发布里按**角色**找：只认文件名里带 Host/Viewer 且与本机角色一致的
         安装包，取其中最高的版本；
      2. 若一条都认不出（单包时代的发布，或产物名改版），退回"最高标签版本"——
         与旧口径一致；
      3. 预发布标签（解析失败）一律跳过，不会被当成可更新版本。
    """
    # (版本元组, tag, release, 资产文件名)；单跑一条坏条目不能拖垮整次比对
    candidates = []
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        tag = rel.get("tag_name")
        parsed = parse_version(tag)
        if parsed is None:
            continue
        candidates.append((parsed, tag, rel))
    if not candidates:
        return {"update": False, "source": "releases"}
    by_role = []
    for parsed, tag, rel in candidates:
        for a in _assets(rel):
            if role_of_asset_name(a.get("name")) == role:
                by_role.append((parsed, tag, rel, a.get("name")))
                break
    if by_role:
        pool = by_role
    elif any(role_of_asset_name(a.get("name")) for rel in candidates for a in _assets(rel[2])):
        # 发布里认得出角色、却没有一个是本机的：对端的新版本不能当本机的更新
        # （装错包比"暂无更新"糟得多）；本机这一端确实还没发新版
        return {"update": False, "current": current, "source": "releases"}
    else:
        # 一条角色都认不出（单包时代的发布）→ 退回"最高标签版本"，与旧口径一致
        pool = [(p, t, r, None) for p, t, r in candidates]
    best = max(pool, key=lambda c: c[0])
    if not is_newer(best[1], current):
        return {"update": False, "current": current, "source": "releases",
                "version": best[1].lstrip("vV")}
    return _release_info(role, current, best[1], best[2], best[3])


def check_release(role, current=APP_VERSION, now=None, opener=None, cache_path=None):
    """判定"本机这一端有没有更新的安装包"；返回结构见 _release_info，失败返回
    {"update": False, "source": "none"/"releases"}，**绝不抛**。

    第 12 轮（2026-10-23）：本函数不再自己管缓存——发布列表统一由
    `_release_list` 取（缓存里存的是**原始列表**，判定按角色现算）。这样
    共用一台机器的共享端与观看端共享同一份缓存，不会互相把对方的缓存顶掉。
    """
    if now is None:
        now = time.time()
    if opener is None:
        opener = urllib.request.urlopen
    if cache_path is None:
        base = data_dir()
        if not base:
            return {"update": False, "source": "none"}
        cache_path = os.path.join(base, CACHE_NAME)
    releases, _fetched = _release_list(opener, cache_path, now)
    if not releases:
        return {"update": False, "source": "none"}
    return _select_release(role, releases, current)


def start_background_check(cfg, post_msg, role="host"):
    """启动后台检查线程；发现新版时调用 post_msg(text)（调用方负责回 UI 主线程）。

    返回 Thread 或 None（开关关闭时不启动）。线程内任何异常都不外抛。
    role 决定判定的是哪一端（共享端 "host" / 观看端 "viewer"）。
    """
    if not enabled(cfg):
        return None

    def work():
        try:
            msg = check_update(cfg, role=role)
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
