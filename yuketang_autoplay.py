# -*- coding: utf-8 -*-
r"""
雨课堂视频自动刷课脚本 —— 最终版（专属隔离浏览器 + 节点级完成度闭环版）
==================================================================
配套环境: 桌面快捷方式"刷课专属Edge"
          启动参数: --user-data-dir="D:\Edge_Auto_Profile" --remote-debugging-port=9222
          (与日常浏览器完全隔离, 不影响默认配置的插件/主页)

第一部分 连接与启动:
  1. 优先 connect_over_cdp 连接已运行的专属浏览器(9222端口)
  2. 连接失败 → subprocess.Popen 自动启动 Edge(独立配置目录+调试端口), 等3秒后重连
  3. 未登录/未进入学习页时, 控制台循环提示"请在浏览器中登录雨课堂并进入视频页面"
  4. 目标列表自检: 每轮用 HTTP 直查 /json, 学习页已开但连接感知不到时自动重建连接

第二部分 核心刷课逻辑:
  1. 精准过滤: 只点击左侧目录树中类型标签含"视频"的节点(.leaf-item-tag),
     绝不点击图文/讨论/作业/考试
  2. 强制设置: muted=True + playbackRate=1.0 原速(2倍速平台不计进度!),
     挂 volumechange/canplay/playing/timeupdate 监听锁死(播放器会偷偷解除静音)
  3. 完成度判定(节点级, 2026-11-21 实测定稿): 每20秒只看左侧目录"当前激活节点"自己——
     已完成的标志 = .leaf-item-status 里出现圆圈勾图标(icon-yuanquangou-mianzhuang)
     或文字"已完成"/100%。绝不读页面全局"完成度XX%"或目录里其他视频的数字
     (全局数字是全课程/其他视频的进度, 误用会导致同一个视频反复重播)
     实测DOM: 未完成视频节点状态栏是空的(<!---->), 完成后才出现圆圈勾图标;
     作业/测试节点显示 status-pie 进度环(与本脚本无关)
  4. 播完后处理: 先等一个周期给平台打勾时间, 节点仍无完成标记才归零重播
     (currentTime=0), 最多重播3轮防死循环, 仍无标记则带警告跳下一节
  5. 自动跳转: 当前节点一旦显示"已完成"→ 立即点击下一个"视频"节点, 循环执行
  6. 防挂机: 播放进度连续30秒未动 → 强制 video.play() + 真实鼠标点击兜底
"""

import os
import subprocess
import sys
import time
import datetime

from playwright.sync_api import sync_playwright

# ==================== 可调参数 ====================
CDP_URL         = "http://localhost:9222"          # 调试端口
AUTO_PROFILE    = r"D:\Edge_Auto_Profile"          # 专属隔离配置目录
START_URL       = "https://www.yuketang.cn/"       # 自动启动时先打开的页面
CHECK_INTERVAL  = 20        # 轮询间隔(秒), 同时也是完成度抓取周期
TARGET_RATE     = 1.0       # 必须原速: 2倍速平台不计学习进度
END_MARGIN      = 2.0       # currentTime >= duration - 2 视为播完
NEXT_TIMEOUT    = 60        # 点击下一节后等待新视频加载的最长时间(秒)
MAX_REPLAYS     = 3         # 播完后最多重播3轮, 当前节点始终无完成标记则带警告跳下一节
STUCK_SECONDS   = 30        # 播放进度连续30秒未动 → 防挂机强制播放
VIDEO_TAG       = "视频"    # 只点击类型标签含此字样的目录节点
# =================================================

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def log(msg: str):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ==================== 注入 JS ====================

# 视频播放状态
STATUS_JS = """
() => {
    const v = document.querySelector('video');
    if (!v) return null;
    return {
        t:      v.currentTime,
        d:      v.duration || 0,          // duration 未加载时是 NaN, 统一转 0
        paused: v.paused,
        ended:  v.ended,
        muted:  v.muted,
        rate:   v.playbackRate,
        src:    (v.currentSrc || v.src || '').slice(-70)
    };
}
"""

# 强制: 静音 + 原速(精确等于) + 播放; 并挂全套监听器锁死
APPLY_JS = """
(rate) => {
    const v = document.querySelector('video');
    if (!v) return null;
    v.muted = true;
    v.volume = 0;
    try { if (Math.abs(v.playbackRate - rate) > 0.01) v.playbackRate = rate; } catch (e) {}

    if (!v.__autoHook) {
        v.__autoHook = true;
        const keep = () => {
            try {
                v.muted = true;
                if (Math.abs(v.playbackRate - rate) > 0.01) v.playbackRate = rate;
            } catch (e) {}
        };
        v.addEventListener('ratechange', keep);      // 改倍速 → 改回
        v.addEventListener('volumechange', keep);    // 解除静音 → 静音回去
        v.addEventListener('play', keep);
        v.addEventListener('playing', keep);
        v.addEventListener('canplay', keep);
        v.addEventListener('loadedmetadata', keep);  // 换源后重设
        v.addEventListener('timeupdate', keep);      // 播放中每250ms兜底
    }

    let rejected = false;
    if (v.paused && !v.ended) {
        try {
            const pr = v.play();
            if (pr && pr.catch) pr.catch(() => { rejected = true; });
        } catch (e) { rejected = true; }
    }
    return rejected;
}
"""

# 进度归零并重播(循环重播用)
PLAY_FROM_START_JS = """
() => {
    const v = document.querySelector('video');
    if (!v) return false;
    try { v.currentTime = 0; } catch (e) {}
    try { const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
    return true;
}
"""

# 完成度抓取(节点级): 只看左侧目录"当前激活节点"自己的状态
# 实测: 未完成视频节点的状态栏是空的, 完成后出现 icon-yuanquangou-mianzhuang 圆圈勾图标
COMPLETION_JS = """
() => {
    const act = document.querySelector('.leaf-item.is-active');
    if (!act) return {nodePercent: null, statusDone: false, activeTitle: '', hasActive: false};
    const activeTitle = ((act.querySelector('.leaf-item-title') || {}).innerText || '').trim();
    const actText = act.innerText || '';
    const m = actText.match(/(\\d{1,3})\\s*%/);              // 当前节点自带的百分比(如有)
    const nodePercent = m ? parseFloat(m[1]) : null;
    const stEl = act.querySelector('.leaf-item-status');
    const icon = stEl ? stEl.querySelector('i.iconfont, i.icon') : null;
    const iconCls = icon ? (icon.className || '') : '';
    const st = ((stEl || {}).innerText || '').trim();
    const cls = act.className || '';
    // "已完成"判定: 圆圈勾图标 / 状态文字 / 完成样式类名 / aria标签
    let statusDone =
        /yuanquangou|gou|check|finish|complete/i.test(iconCls) ||
        st.indexOf('已完成') >= 0 ||
        actText.indexOf('已完成') >= 0 ||
        st.indexOf('100%') >= 0 ||
        /is-finish|is-complete|finished|completed/i.test(cls) ||
        (act.getAttribute('aria-label') || '').indexOf('已完成') >= 0;
    return {nodePercent, statusDone, activeTitle, hasActive: true};
}
"""

# 连播: 在目录树中定位"当前节点的下一个视频节点"并点击
NEXT_VIDEO_JS = """
() => {
    const items = [...document.querySelectorAll('.leaf-item')];
    if (!items.length) return null;
    const isVideo = (el) => {
        const tag = el.querySelector('.leaf-item-tag');
        return tag && (tag.innerText || '').trim().indexOf('视频') >= 0;
    };
    const vids = items.filter(isVideo);                  // 绝不含图文/讨论/作业/考试
    if (!vids.length) return null;
    const name = (el) => ((el.querySelector('.leaf-item-title') || {}).innerText || '').trim();

    const idx = vids.findIndex(el => el.className.indexOf('is-active') >= 0);
    if (idx >= 0) {
        if (idx + 1 >= vids.length) return {kind: 'LAST', title: name(vids[idx])};
        const nxt = vids[idx + 1];
        nxt.click();                                     // 实测 JS click 可触发路由跳转
        return {kind: 'clicked', title: name(nxt), pos: (idx + 2) + '/' + vids.length};
    }
    // 当前节点不是视频(如停在图文/作业上): 点击它后面第一个视频节点
    const aIdx = items.findIndex(el => el.className.indexOf('is-active') >= 0);
    if (aIdx >= 0) {
        const after = vids.find(el => items.indexOf(el) > aIdx);
        if (after) { after.click(); return {kind: 'fallback', title: name(after)}; }
    }
    return null;
}
"""

# 自动关闭确认弹窗(只点正向按钮)
DISMISS_JS = """
() => {
    const words = ['确定', '继续学习', '继续观看', '继续', '知道了', '我知道了'];
    const els = [...document.querySelectorAll('button, a, .btn, div, span')].filter(el => {
        if (!el.offsetParent) return false;
        const t = (el.innerText || '').trim();
        return t && t.length <= 8 && words.indexOf(t) >= 0;
    });
    if (els.length) { els[0].click(); return els[0].innerText.trim(); }
    return null;
}
"""

# JS 兜底点击播放
CLICK_PLAY_JS = """
() => {
    const sels = ['.xt_video_player_commons_icon_play', '.playIcon',
                  '.vjs-big-play-button', '.xgplayer-play'];
    for (const s of sels) {
        const el = document.querySelector(s);
        if (el) { try { el.click(); return s; } catch (e) {} }
    }
    const v = document.querySelector('video');
    if (v) { try { v.click(); return 'video'; } catch (e) {} }
    return null;
}
"""

# ==================== 浏览器连接(第一部分) ====================

def launch_edge():
    """用 subprocess.Popen 启动专属 Edge: 独立配置目录 + 调试端口 9222"""
    edge = next((c for c in EDGE_CANDIDATES if os.path.exists(c)), 'msedge.exe')
    cmd = [edge, f'--user-data-dir={AUTO_PROFILE}',
           '--remote-debugging-port=9222', '--no-first-run', START_URL]
    log(f"自动启动专属 Edge: {os.path.basename(edge)}  (配置目录 {AUTO_PROFILE})")
    try:
        subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS)
    except Exception as e:
        log(f"启动 Edge 失败: {e}")


def ensure_browser(p, browser):
    """保证有一条可用的 CDP 连接; 断线/未启动时自动重连或拉起 Edge"""
    if browser is not None:
        try:
            if browser.is_connected():
                return browser
        except Exception:
            pass
        log("与 Edge 的连接已断开, 尝试重连...")
    try:
        b = p.chromium.connect_over_cdp(CDP_URL, timeout=8000)
        log("已连接专属 Edge (CDP 9222)")
        return b
    except Exception:
        pass
    # 连接失败 → 自动启动专属浏览器, 等 3 秒后再连(带重试)
    launch_edge()
    for _ in range(5):
        time.sleep(3)
        try:
            b = p.chromium.connect_over_cdp(CDP_URL, timeout=8000)
            log("已连接自动启动的专属 Edge")
            return b
        except Exception:
            continue
    return None


def pick_page(browser):
    """只接管 lms-graph 学习页; 未进入学习页时返回 None(触发登录等待提示)"""
    pages = []
    for ctx in browser.contexts:
        pages.extend(ctx.pages)
    graph = [p for p in pages if 'lms-graph' in p.url]
    return graph[-1] if graph else None


def cdp_has_graph_page():
    """绕开 Playwright 连接, 直接 HTTP 查 DevTools 目标列表, 判断学习页是否已打开。

    用途: 脚本先连接、用户后开标签页时, 旧 CDP 连接的目标列表可能"过期"
    (感知不到新开的标签页)。此函数返回 True 而 pick_page 返回 None,
    即说明需要重建连接刷新目标列表。"""
    try:
        import urllib.request
        import json as _json
        with urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=3) as r:
            data = _json.loads(r.read().decode('utf-8', 'ignore'))
        return any(t.get('type') == 'page' and 'lms-graph' in (t.get('url') or '')
                   for t in data)
    except Exception:
        return False


def find_video_frame(page):
    """找到包含 video 标签的 frame(该页面在主 frame, 保留多 frame 兼容)"""
    for frame in page.frames:
        try:
            if frame.evaluate("() => !!document.querySelector('video')"):
                return frame
        except Exception:
            continue
    return None


def force_play(frame):
    """防挂机恢复: JS play + 真实鼠标点击视频区域兜底"""
    try:
        frame.evaluate("() => { const v=document.querySelector('video'); if (v && v.play) v.play().catch(()=>{}); }")
    except Exception:
        pass
    try:
        frame.locator("video").first.click(timeout=3000)
        log("已真实点击视频区域恢复播放")
    except Exception:
        try:
            ok = frame.evaluate(CLICK_PLAY_JS)
            if ok:
                log(f"已点击播放控件: {ok}")
        except Exception:
            pass


def resolve_completion(ci):
    """完成度只取自"当前激活节点"自己(节点级):
    ① 节点状态栏出现圆圈勾图标/文字"已完成" → 100, 当前视频判定完成
    ② 节点自带百分比(如有) → 按该值判定
    ③ 都没有 → None(未标记, 播完后走保险策略)
    绝不读页面全局"完成度XX%"或目录里其他视频的数字!"""
    if ci.get("statusDone"):
        return 100.0, "节点已完成标记"
    if ci.get("nodePercent") is not None:
        return float(ci["nodePercent"]), "节点自带百分比"
    return None, "节点无完成标记"


# ==================== 主流程 ====================

def main():
    with sync_playwright() as p:
        log("=" * 60)
        log("雨课堂自动刷课脚本启动 (专属隔离浏览器版)")
        log("=" * 60)

        browser = ensure_browser(p, None)
        if browser is None:
            log("无法连接或启动 Edge, 请检查后重新运行脚本")
            sys.exit(1)

        last_src = None        # 上个视频源(识别新视频)
        last_url = None        # 上个页面URL(识别节点切换)
        last_t = None          # 上次进度秒数
        last_progress_ts = time.time()   # 上次进度变化的时间
        replays = 0            # 当前视频的重播轮数
        end_grace_used = False # 播完后是否已等待过平台打勾
        done_count = 0         # 已完成切换的视频数

        while True:
            try:
                browser = ensure_browser(p, browser)
                if browser is None:
                    log("无法连接 Edge, 10秒后重试...")
                    time.sleep(10)
                    continue

                page = pick_page(browser)
                # 学习页已打开但当前连接感知不到(目标列表过期) → 重建连接刷新
                if (page is None or 'lms-graph' not in page.url) and cdp_has_graph_page():
                    log("检测到学习页已打开但当前连接未感知到, 重建 CDP 连接...")
                    try:
                        browser.close()   # connect_over_cdp 的 close 只断开连接, 不会关 Edge
                    except Exception:
                        pass
                    browser = ensure_browser(p, None)
                    if browser is not None:
                        page = pick_page(browser)
                if page is None or 'lms-graph' not in page.url:
                    # 未登录 / 还在目录外 / 空白页 → 循环等待用户操作
                    log("请在浏览器中登录雨课堂并进入视频页面 (每20秒自动检查一次)")
                    time.sleep(CHECK_INTERVAL)
                    continue

                try:
                    page.bring_to_front()
                except Exception:
                    pass
                main_frame = page.main_frame

                # 自动关闭确认弹窗
                try:
                    dismissed = main_frame.evaluate(DISMISS_JS)
                    if dismissed:
                        log(f"已自动点击弹窗按钮: {dismissed}")
                except Exception:
                    pass

                # 完成度抓取(每20秒一次, 只读当前激活节点)
                try:
                    ci = main_frame.evaluate(COMPLETION_JS)
                except Exception:
                    ci = {}
                comp, comp_src = resolve_completion(ci)

                frame = find_video_frame(page)
                if frame is None:
                    log(f"已进入学习页但当前节点没有视频(当前: {ci.get('activeTitle', '?')}), "
                        f"尝试跳到下一个视频节点...")
                    # 若停在了图文/作业等节点, NEXT_VIDEO_JS 会自动点后面的视频节点
                    res = None
                    try:
                        res = main_frame.evaluate(NEXT_VIDEO_JS)
                    except Exception:
                        res = None
                    if res and res.get('kind') in ('clicked', 'fallback'):
                        log(f"已点击视频节点: {res.get('title')} {res.get('pos', '')}")
                        last_src = last_url = None
                        replays = 0
                        end_grace_used = False
                        time.sleep(CHECK_INTERVAL)
                    continue

                st = frame.evaluate(STATUS_JS)
                if not st:
                    time.sleep(CHECK_INTERVAL)
                    continue

                # ---- 新视频/新节点检测 ----
                if (st["src"] and st["src"] != last_src) or page.url != last_url:
                    mark = "✓已完成" if ci.get("statusDone") else "未标记完成"
                    log(f"当前节点: {ci.get('activeTitle', '?')}  "
                        f"视频 {st['t']:.0f}/{st['d']:.0f}s  完成标记: {mark}")
                    last_src, last_url = st["src"], page.url
                    last_t, replays = None, 0
                    end_grace_used = False
                    last_progress_ts = time.time()

                # ---- 强制原速 + 静音 + 播放 ----
                frame.evaluate(APPLY_JS, TARGET_RATE)
                if abs(st["rate"] - TARGET_RATE) > 0.01:
                    log(f"倍速被页面改为 {st['rate']}x, 已强制恢复 {TARGET_RATE}x (2倍速不计进度)")

                # ---- 当前节点已标"已完成" → 立即跳下一节(不管视频是否播完) ----
                if comp is not None and comp >= 100:
                    done_count += 1
                    log(f"「{ci.get('activeTitle', '?')}」当前节点显示已完成 "
                        f"(来源:{comp_src}), 切换下一节... [累计{done_count}]")
                    old_src, old_url = st["src"], page.url
                    advance(main_frame, browser, old_src, old_url)
                    last_src, last_url = None, None
                    replays = 0
                    end_grace_used = False
                    continue

                # ---- 播完判定 ----
                finished = st["ended"] or (st["d"] > 0 and st["t"] >= st["d"] - END_MARGIN)
                if finished:
                    if comp is None:
                        # 当前节点还没有完成标记: 先等一个周期给平台打勾时间
                        if not end_grace_used:
                            end_grace_used = True
                            log("视频已播完, 当前节点还没有完成标记, 等待平台记录(下轮复查)...")
                            time.sleep(CHECK_INTERVAL)
                            continue
                        if replays < 1:
                            replays += 1
                            log("等待后节点仍无完成标记, 归零重播一遍以确保进度记录...")
                            frame.evaluate(PLAY_FROM_START_JS)
                            last_progress_ts = time.time()
                            end_grace_used = False
                            continue
                        log("【警告】重播后当前节点始终未出现完成标记, 按已播完处理, 切换下一节")
                        old_src, old_url = st["src"], page.url
                        advance(main_frame, browser, old_src, old_url)
                        last_src, last_url = None, None
                        replays = 0
                        end_grace_used = False
                        continue
                    if comp < 100:
                        # 播完了但当前节点完成度没到100% → 归零重播
                        if replays >= MAX_REPLAYS:
                            log(f"【警告】重播{replays}轮后当前节点仍只有 {comp}% "
                                f"(来源:{comp_src}), 平台始终未标记已完成, 跳下一节")
                            old_src, old_url = st["src"], page.url
                            advance(main_frame, browser, old_src, old_url)
                            last_src, last_url = None, None
                            replays = 0
                            end_grace_used = False
                            continue
                        replays += 1
                        log(f"视频已播完但当前节点未标已完成({comp:g}% < 100%, 来源:{comp_src}), "
                            f"第 {replays}/{MAX_REPLAYS} 轮归零重播...")
                        frame.evaluate(PLAY_FROM_START_JS)
                        last_progress_ts = time.time()
                        end_grace_used = False
                        continue
                    # comp >= 100 的情况在上面已处理

                # ---- 防挂机: 进度连续30秒未动 → 强制播放 ----
                if last_t is not None and abs(st["t"] - last_t) > 0.3:
                    last_progress_ts = time.time()
                last_t = st["t"]
                if time.time() - last_progress_ts >= STUCK_SECONDS:
                    log(f"【防挂机】进度连续{STUCK_SECONDS}秒未动(t={st['t']:.0f}s, "
                        f"paused={st['paused']}), 强制恢复播放")
                    force_play(frame)
                    last_progress_ts = time.time()
                else:
                    comp_show = f"{comp:g}%" if comp is not None else "未标记"
                    log(f"播放中  {st['t']:.0f}/{st['d']:.0f}s  {st['rate']}x  "
                        f"muted={st['muted']}  当前节点={comp_show}  重播轮数={replays}")

                time.sleep(CHECK_INTERVAL)

            except KeyboardInterrupt:
                log("已手动停止(Ctrl+C), 专属 Edge 浏览器保持打开")
                break
            except Exception as e:
                log(f"本轮出错(忽略并继续): {type(e).__name__}: {e}")
                time.sleep(CHECK_INTERVAL)


def advance(main_frame, browser, old_src, old_url):
    """点击下一个视频节点并等待新视频加载"""
    res = None
    try:
        res = main_frame.evaluate(NEXT_VIDEO_JS)
    except Exception:
        res = None
    if not res:
        log("未找到下一个视频节点(可能目录未展开), 20秒后重试")
        return False
    if res.get('kind') == 'LAST':
        log(f"「{res.get('title')}」已是目录中最后一个视频, 待机中(每20秒复查)...")
        return False
    log(f"已点击下一节: {res.get('title')}  {res.get('pos', '')}")
    waited = 0
    while waited < NEXT_TIMEOUT:
        time.sleep(3)
        waited += 3
        page = pick_page(browser)
        if page is None:
            continue
        f2 = find_video_frame(page)
        if f2 is None:
            continue              # 路由跳转中
        try:
            s2 = f2.evaluate(STATUS_JS)
        except Exception:
            continue
        if s2 and ((s2["src"] and s2["src"] != old_src) or page.url != old_url):
            log("新视频已加载, 开始播放")
            return True
    log("等待新视频超时, 下轮自动重试")
    return False


if __name__ == "__main__":
    main()
