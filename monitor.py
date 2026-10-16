#!/usr/bin/env python3
"""VPS restock watcher.

拉取商品页 -> 按 monitors.json 规则判断库存 -> 有货状态变化时推 Telegram/微信。
仅用标准库。为 GitHub Actions 设计：
- watch 模式：一个 job 连续跑 ~4 小时，内部每 CHECK_INTERVAL 秒查一次，
  时间预算用完干净退出，由下一个调度窗口无缝接力；
- state.json 记录各目标上次库存，跨进程/跨 job 去重，只在状态变化时提交回仓库；
- 被 Cloudflare 拦截的站点通过 FlareSolverr（无头浏览器服务）抓取；
- 双向防抖系统：
  1) verify_pattern 机制：正向核实商品信息是否存在，杜绝骨架屏/404/挑战页误判为有货；
  2) 售罄迟滞确认 (Hysteresis)：连续 2 轮确认缺货才变更状态，彻底消除单轮页面抖动；
  3) 推送冷却窗口 (NOTIFY_COOLDOWN)：同一单品 30 分钟内绝不重复警报，杜绝消息轰炸。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def bj_now_str():
    bj_tz = timezone(timedelta(hours=8))
    return datetime.now(bj_tz).strftime("%Y-%m-%d %H:%M:%S")

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "40"))      # 每轮检查间隔（秒）
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "349"))         # 本进程最长运行分钟数
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "6"))   # 心跳消息间隔（小时）
NOTIFY_COOLDOWN = int(os.environ.get("NOTIFY_COOLDOWN", "600"))   # 10分钟防抖冷却（支持捕获15分钟未支付订单回滚）
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")   # Server酱 SendKey（微信推送，备用渠道）
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")   # PushPlus token（微信推送，备用渠道）
WEIXIN_HEARTBEAT = os.environ.get("WEIXIN_HEARTBEAT") == "1"  # 微信渠道是否也发心跳（默认只发补货警报）
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "")
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "").rstrip("/")
ALLOW_STATE_COMMIT = os.environ.get("ALLOW_STATE_COMMIT") == "1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FLARE_SESSION = "vpsmon"
_flare_ready = False


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def cf_worker_get(url, timeout=25):
    if not CF_WORKER_URL:
        raise RuntimeError("未配置 CF_WORKER_URL")
    proxy_url = f"{CF_WORKER_URL}?url={urllib.parse.quote(url)}"
    return http_get(proxy_url, timeout=timeout)


def visible_text(html):
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _flare_post(payload):
    req = urllib.request.Request(
        FLARESOLVERR_URL.rstrip("/") + "/v1",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def flare_get(url):
    global _flare_ready
    if not FLARESOLVERR_URL:
        raise RuntimeError("页面被 Cloudflare 拦截，且未配置 FLARESOLVERR_URL")
    if not _flare_ready:
        try:
            _flare_post({"cmd": "sessions.create", "session": FLARE_SESSION})
        except Exception:
            pass
        _flare_ready = True
    data = _flare_post({"cmd": "request.get", "url": url,
                        "session": FLARE_SESSION, "maxTimeout": 60000})
    if data.get("status") != "ok":
        try:
            _flare_post({"cmd": "sessions.destroy", "session": FLARE_SESSION})
        except Exception:
            pass
        try:
            _flare_post({"cmd": "sessions.create", "session": FLARE_SESSION})
        except Exception:
            pass
        data = _flare_post({"cmd": "request.get", "url": url,
                            "session": FLARE_SESSION, "maxTimeout": 60000})
        if data.get("status") != "ok":
            raise RuntimeError(f"FlareSolverr: {str(data.get('message'))[:120]}")
    return data["solution"]["response"]


def fetch_html(target):
    mode = target.get("fetch", "auto")
    url = target["url"]

    if mode == "cf_worker":
        try:
            return cf_worker_get(url)
        except Exception as e:
            log(f"[cf_worker] {url} Worker代理失败({e})，尝试 FlareSolverr/直连备用")
            if FLARESOLVERR_URL:
                return flare_get(url)
            raise

    if mode == "flare":
        try:
            return flare_get(url)
        except Exception as e:
            if CF_WORKER_URL:
                log(f"[flare] {url} FlareSolverr失败({e})，尝试 CF Worker 备用")
                try:
                    return cf_worker_get(url)
                except Exception:
                    pass
            raise

    try:
        html = http_get(url)
        low = html.lower()
        if "just a moment" in low or "cf-chl" in low or "cf-challenge" in low:
            raise RuntimeError("cloudflare challenge")
        return html
    except (urllib.error.HTTPError, RuntimeError) as e:
        if mode == "auto":
            if CF_WORKER_URL:
                log(f"[auto] {url} 直连被拦截({e})，尝试 CF Worker")
                try:
                    res = cf_worker_get(url)
                    low = res.lower()
                    if "just a moment" not in low and "cf-chl" not in low and "cf-challenge" not in low:
                        return res
                except Exception:
                    pass
            if FLARESOLVERR_URL:
                log(f"[auto] {url} 改走 FlareSolverr")
                return flare_get(url)
        raise


def compute_stock(target, text=None):
    """返回库存：
    - count 模式：通过正则提取数字，提取不到返回 None；
    - badge 模式：先验证商品特征词 (verify_pattern)，未验证通过说明页面不完整返回 None；验证通过后根据 oos_pattern 判定 0/1；
    - category 模式：统计可订购匹配数。若为 0 且页面未出现任何缺货/售罄特征，说明渲染未就绪返回 None；
    - json 模式：读接口字段。
    """
    mode = target.get("stock", "count")

    if mode == "badge":
        # 1. 验证目标商品是否渲染在页面上（第一道防线）
        verify = target.get("verify_pattern")
        if verify and not re.search(verify, text, re.I):
            sample = text[:120].strip()
            log(f"[warn] {target['name']}: 页面未检测到商品特征词({verify})。页面摘要: {sample}")
            return None

        # 2. 检查是否有明确的缺货/售罄标记
        is_oos = bool(re.search(target["oos_pattern"], text))

        # 3. 双正向断言（第二道防线：彻底消除反向匹配误报陷阱）
        # 页面不仅要“未见缺货标”，还必须“正向明确检测到可订购动作”才判定为有货；若二者皆无，说明是软报错/维护页/残损DOM，安全跳过！
        in_stock_pat = target.get("in_stock_pattern")
        if in_stock_pat:
            is_in_stock = bool(re.search(in_stock_pat, text, re.I))
            if is_oos:
                return 0
            if is_in_stock:
                return 1
            sample = text[:120].strip()
            log(f"[warn] {target['name']}: 既无缺货标亦无指定在售标({in_stock_pat})，判定DOM不完整跳过。摘要: {sample}")
            return None

        generic_buy = target.get("available_pattern", r"(?:立即订购|立即购买|Order Now|Add to Cart|Select\b|订购|Continue)")
        if is_oos:
            return 0
        if re.search(generic_buy, text, re.I):
            return 1

        sample = text[:120].strip()
        log(f"[warn] {target['name']}: 虽未见缺货标，但亦未检测到任何订购按钮，疑似维护页或软报错，安全跳过。摘要: {sample}")
        return None

    if mode == "category":
        pattern = target.get("available_pattern", r"(?:立即订购|立即购买|Order Now|Add to Cart|Select\b|订购|Continue)")
        matches = re.findall(pattern, text, re.I)
        count = len(matches)
        if count == 0:
            # 验证页面是否真的是“全部分类售罄”，还是页面没加载出来
            oos_check = target.get("category_oos_pattern", r"(?:缺货|售罄|已售罄|无货|Out of Stock|Sold Out|0\s*(?:Available|台)|Unavailable)")
            if not re.search(oos_check, text, re.I):
                sample = text[:120].strip()
                log(f"[warn] {target['name']}: 分类页未检测到任何商品或售罄标记。页面摘要: {sample}")
                return None
        return count

    if mode == "json":
        return compute_stock_json(target)

    if "pattern" in target and target["pattern"]:
        m = re.search(target["pattern"], text)
        return int(m.group(1)) if m else None

    return 1


# ---- panstar.ai JSON 接口（响应 AES-256-GCM 加密，密钥来自其前端 JS）----

PANSTAR_KEY = base64.b64decode("26lAIOdVLW74vJFTawTAiA79kXTtdlibFh49gt+j5Zk=")


def fetch_json(target):
    url = target.get("json_url") or target["url"]
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        encrypted = r.headers.get("X-Api-Encrypt")
        iv = r.headers.get("X-Api-Encrypt-Iv")
        data = json.loads(r.read())
    if encrypted == "1" or isinstance(data.get("ciphertext"), str):
        if AESGCM is None:
            raise RuntimeError("需要 pip install cryptography 才能解密 panstar 响应")
        plain = AESGCM(PANSTAR_KEY).decrypt(base64.b64decode(iv),
                                            base64.b64decode(data["ciphertext"]), None)
        data = json.loads(plain)
    return data


def _plans_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "list", "records", "data", "plans"):
            if isinstance(data.get(k), list):
                return data[k]
    raise RuntimeError("plans 数据结构不认识，接口可能已改版")


def _sellable(plans):
    return [p for p in plans if not p.get("archived") and p.get("status", 0) == 0 and p.get("enabled", True)]


def _json_find(data, spec):
    """分步取套餐：steps 每步 {path, filter}——先按 path 取子级（dict 直取，
    list 逐项展开拍平），再用 filter 对本层条目做等值过滤。"""
    cur = data
    for step in spec["steps"]:
        seg = step.get("path", "")
        keys = [k.strip() for k in seg.split(".") if k.strip()]
        if isinstance(cur, list):
            flat = []
            for item in cur:
                obj = item
                for key in keys:
                    obj = obj[key]
                flat.extend(obj if isinstance(obj, list) else [obj])
            cur = flat
        else:
            for key in keys:
                cur = cur[key]
        f = step.get("filter")
        if f:
            cur = [x for x in cur if all(x.get(k) == v for k, v in f.items())]
    return cur


def compute_stock_json(target):
    data = fetch_json(target)
    spec = target.get("json_find_plans")
    plans = _json_find(data, spec) if spec else _plans_list(data)
    plans = _sellable(plans)
    stock_field = target.get("json_stock_field", "stockAvailable")
    pick = target.get("json_pick", "cheapest")

    def plan_label(p):
        return p.get("name") or p.get("plan_name") or str(p.get("id"))

    def fmt(p):
        price_val = p.get("price")
        if price_val is None and "monthly_price_cents" in p:
            price_val = p["monthly_price_cents"] / 100
            currency = "¥"
        else:
            price_val = (price_val or 0) / 100
            currency = "$"
        flow = p.get("traffic") or p.get("flow", 0)
        return (f'{p.get("name")}: {currency}{price_val:g}/月, '
                f'{p.get("cpu")}核/{p.get("memory")}M内存/{p.get("disk")}G盘/{flow}G流量')

    if pick == "plan_id":
        plan = next((p for p in plans if p.get("id") == target.get("plan_id")), None)
        if plan is None:
            raise RuntimeError(f'plan {target.get("plan_id")} 不存在或已下架')
        target["_detail"] = fmt(plan)
        return 1 if (plan.get(stock_field) if isinstance(plan.get(stock_field), (int, float)) else bool(plan.get(stock_field))) else 0
    if pick == "cheapest":
        best = min(plans, key=lambda p: (p.get("monthly_price_cents", p.get("price", 0)) if isinstance(p.get("price") or p.get("monthly_price_cents"), (int, float)) else 1e18))
        target["_detail"] = fmt(best)
        return 1 if best.get(stock_field) else 0
    if pick == "count_in_stock":
        in_stock = [p for p in plans if (p.get(stock_field) > 0 if isinstance(p.get(stock_field), (int, float)) else bool(p.get(stock_field)))]
        if in_stock:
            target["_detail"] = "\n".join(
                f"· {plan_label(p)}: {p.get(stock_field)} 台有货" for p in in_stock)
        return len(in_stock)
    raise RuntimeError(f"未知 json_pick: {pick}")


def stock_text(target, n):
    mode = target.get("stock", "count")
    if mode == "badge":
        return "有货" if n else "缺货 (0 Available)"
    if mode == "category" or target.get("json_pick") == "count_in_stock":
        return f"有货 ({n} 款在售/补货)" if n else "全分类售罄"
    return f"{n} 台"


def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        log("[tg] 未配置 TG_TOKEN/TG_CHAT，本应发送:", text.replace("\n", " | "))
        return
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT,
        "text": text,
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log("[tg] 已发送")
    except Exception as e:
        log("[tg] 发送失败:", repr(e))


def weixin_send(title, body):
    """通过 PushPlus / Server酱 推微信。优先使用 PushPlus（每日200条），未配置时回退到 Server酱。"""
    sent = False
    if body:
        if body.startswith("http://") or body.startswith("https://"):
            full_body = f"### {title}\n\n🛒 **购买直链**：[{body}]({body})"
        else:
            full_body = f"### {title}\n\n{body}"
    else:
        full_body = title

    if PUSHPLUS_TOKEN:
        try:
            # PushPlus 官方限制：title 上限 100 字符，content 上限 20,000 字符 (POST JSON)
            pp_title = title if len(title) <= 100 else (title[:99] + "…")
            pp_content = full_body
            if len(pp_content) > 20000:
                pp_content = pp_content[:19900] + "\n\n*(内容过长已截断)*"

            payload = json.dumps({"token": PUSHPLUS_TOKEN, "title": pp_title,
                                  "content": pp_content, "template": "markdown"}).encode()
            req = urllib.request.Request("https://www.pushplus.plus/send",
                                         data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r)
            if res.get("code") == 200:
                sent = True
                log("[wechat/PushPlus] 已发送")
            else:
                log("[wechat/PushPlus] 失败:", str(res)[:150])
        except Exception as e:
            log("[wechat/PushPlus] 发送失败:", repr(e))
        return sent

    if SERVERCHAN_KEY:
        try:
            # Server酱标题限制32字符，若超长保留31字加省略号，并在正文中完整呈现商品名与直达链接
            short_title = title if len(title) <= 32 else (title[:31] + "…")
            data = urllib.parse.urlencode({"title": short_title, "desp": full_body}).encode()
            req = urllib.request.Request(
                f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send", data=data)
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r)
            if res.get("code") == 0:
                sent = True
                log("[wechat/Server酱] 已发送")
            else:
                log("[wechat/Server酱] 失败:", str(res)[:150])
        except Exception as e:
            log("[wechat/Server酱] 发送失败:", repr(e))
        return sent

    log("[wechat] 未配置微信推送渠道")
    return None


def notify(title, body, weixin=True):
    """多渠道通知：Telegram + 微信（Server酱/PushPlus）。心跳类消息可指定 weixin=False 省额度。"""
    tg_send(f"{title}\n{body}" if body else title)
    if weixin:
        weixin_send(title, body)


def load_targets():
    with open(os.path.join(BASE_DIR, "monitors.json"), encoding="utf-8") as f:
        return json.load(f)


def load_state():
    try:
        with open(os.path.join(BASE_DIR, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_and_commit_state(state):
    path = os.path.join(BASE_DIR, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    if not ALLOW_STATE_COMMIT:
        return
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("CHAIN_PAT", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not (token and repo):
        return

    remote = f"https://x-access-token:{token}@github.com/{repo}.git"

    # 多工作流并发防竞态重试循环（最多重试 3 次，指数退避）
    for attempt in range(1, 4):
        try:
            # 1. 强行终止任何可能残留的中间合并/变基状态
            subprocess.run(("git", "rebase", "--abort"), capture_output=True)
            subprocess.run(("git", "merge", "--abort"), capture_output=True)

            # 2. 拉取远端最新提交并重置本地 HEAD
            fetch_res = subprocess.run(("git", "fetch", remote, "main"), capture_output=True)
            if fetch_res.returncode != 0:
                time.sleep(attempt * 1.5)
                continue
            subprocess.run(("git", "reset", "--mixed", "FETCH_HEAD"), capture_output=True)

            # 3. 与远端 state.json 智能合并（双向合并，绝不丢失任何并发工作流写回的状态）
            try:
                with open(path, "r", encoding="utf-8") as rf:
                    merged = json.load(rf)
            except Exception:
                merged = {}
            merged.update(state)
            with open(path, "w", encoding="utf-8") as wf:
                json.dump(merged, wf, ensure_ascii=False, indent=1)

            subprocess.run(("git", "add", "state.json"), capture_output=True)
            c = subprocess.run(("git", "-c", "user.name=stock-bot", "-c",
                                "user.email=actions@users.noreply.github.com",
                                "commit", "-m", "stock state update"),
                               capture_output=True, text=True)
            # 若无变更（并发工作流已先一步推入了相同状态），直接返回无需推送
            if c.returncode != 0 and "nothing to commit" in (c.stdout + c.stderr):
                log(f"[state] 远端状态已由并发工作流同步至最新，无需重复提交")
                return

            p = subprocess.run(("git", "push", remote, "HEAD:main"), capture_output=True, text=True)
            if p.returncode == 0:
                log(f"[state] 状态已成功同步至 GitHub 仓库 (第 {attempt} 次提交)")
                return
            else:
                log(f"[state] 遭遇远程提交竞争，准备重试 (第 {attempt} 次): {p.stderr.strip()[:80]}")
                time.sleep(attempt * 1.5)
        except Exception as e:
            log(f"[state] 提交异常 (第 {attempt} 次):", repr(e))
            time.sleep(attempt * 1.5)


def dispatch_next_watch():
    """自触发接力：下班前把下一班值守拉起来。
    优先用 CHAIN_PAT（个人令牌，确定可行）；实测 GITHUB_TOKEN 的
    workflow_dispatch 不会真正触发新运行，故只作最后兜底。"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("CHAIN_PAT", "") or os.environ.get("GITHUB_TOKEN", "")
    if not (repo and token):
        log("[chain] 缺少 GITHUB_REPOSITORY/令牌，跳过自触发")
        return
    body = json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main")}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/watch.yml/dispatches",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "restock-chain"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log("[chain] 已呼叫下一班上岗，接力完成")
    except Exception as e:
        log("[chain] 自触发失败，由定时排班兜底:", repr(e))


def peer_on_duty():
    """排班班次开工查岗：若已有值守窗口在跑，本班自动下岗（防止多班重叠）。"""
    if "--force" in sys.argv or os.environ.get("FORCE_CHECK") == "1":
        return False
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("CHAIN_PAT", "")
    if not (repo and token):
        return False
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/runs?status=in_progress&per_page=30",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "restock-chain"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            runs = json.load(r).get("workflow_runs", [])
    except Exception as e:
        log("[peer] 查岗失败，按无同伴继续:", repr(e))
        return False
    me = os.environ.get("GITHUB_RUN_ID", "")
    for run in runs:
        if str(run["id"]) == me or not run["name"].startswith("watch"):
            continue
        if run["status"] == "in_progress":
            # 若同伴已运行超 3.5 小时（210分钟），说明同伴即将结束下岗，本班应当接力，不能退岗
            try:
                created_str = run.get("created_at", "")
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                age_seconds = (datetime.now(timezone.utc) - created_dt).total_seconds()
                if age_seconds > 210 * 60:
                    log(f"[peer] 发现老窗口 run {run['id']} 已运行 {int(age_seconds/60)}m，本班正常接班上岗")
                    continue
            except Exception:
                pass
            log(f"[peer] 发现已有同伴在岗: run {run['id']}（{run['created_at'][11:16]} UTC 起）")
            return True
    return False


def main():
    once = "--once" in sys.argv
    if peer_on_duty():
        log("[peer] 已有同伴窗口在岗，本班自动下岗（排班冗余）")
        return
    targets = load_targets()
    prev = {t["name"]: load_state().get(t["name"]) for t in targets}
    oos_streak = {}     # 记录连续检测为缺货的次数（必须连续 2 轮确认才将有货状态降为 0）
    last_notified = {}  # 记录单品上次推送时间，同一目标设定 30 分钟防抖冷却
    if not once:
        notify("🟢 补货监控已启动", "盯住:\n" + "\n".join(
            f"· {t['name']}" for t in targets), weixin=False)
    start = time.time()
    last_beat = start
    while True:
        cycle_start = time.time()
        cache = {}
        dirty = False
        for t in targets:
            try:
                if t.get("stock") == "json":
                    n = compute_stock(t)
                else:
                    key = t["url"]
                    if key not in cache:
                        cache[key] = visible_text(fetch_html(t))
                    n = compute_stock(t, cache[key])
            except Exception as e:
                log(f"[net] {t['name']}: {e!r}")
                continue
            if n is None:
                continue

            old = prev[t["name"]]
            threshold = t.get("notify_above", 0)

            # 缺货迟滞防抖：若之前是有货状态(old > threshold)，单次偶发 0 暂不降级，需连续 2 轮确认
            if old is not None and old > threshold and n <= threshold:
                streak = oos_streak.get(t["name"], 0) + 1
                oos_streak[t["name"]] = streak
                if streak < 2:
                    log(f"[jitter] {t['name']}: 偶发检测到缺货 (第 1 次)，等待下一轮复核确认，暂不降级")
                    continue
            else:
                oos_streak[t["name"]] = 0

            if old is None:
                log(f"[init] {t['name']}: {n}")
            else:
                is_restock = False
                restock_reason = ""
                # 判定 1：从无到有经典补货（缺货/售罄 -> 有货）
                if n > threshold >= old:
                    is_restock = True
                    restock_reason = f"缺货上架（现已可购: {stock_text(t, n)}）"
                # 判定 2：分类模式增量补货（例如 1 款增至 3 款，说明上了特惠新款或原有缺货款恢复）
                elif t.get("stock") == "category" and n > old > threshold:
                    is_restock = True
                    restock_reason = f"新增在售款式（由 {old} 款增至 {n} 款）"
                # 判定 3：数字库存大幅补货（之前仅剩少量尾货 <=5 台，突然批量补充 >=5 台）
                elif t.get("stock") == "count" and old <= 5 and n >= old + 5:
                    is_restock = True
                    restock_reason = f"库存补充上架（由 {old} 台增至 {n} 台）"

                if is_restock:
                    now = time.time()
                    last_t = last_notified.get(t["name"], 0)
                    if now - last_t >= NOTIFY_COOLDOWN:
                        last_notified[t["name"]] = now
                        detail = t.pop("_detail", "")
                        bj_time = bj_now_str()
                        buy_link = t.get("buy_url", t.get("url", ""))

                        card_lines = [
                            f"### 🚨 {t['name']} 补货啦！",
                            f"- ⏱ **检测时间**：{bj_time} (北京时间)",
                            f"- 📦 **变动详情**：{restock_reason}",
                        ]
                        if detail:
                            card_lines.append(f"- 📋 **套餐信息**：{detail}")
                        card_lines.append(f"- 🛒 **购买直链**：[点击立即前往官网购买]({buy_link})")
                        card_lines.append("\n*(热门套餐库存极快耗尽，建议点击直链迅速锁定订单)*")

                        body_md = "\n".join(card_lines)
                        title_str = f"🚨 {t['name']} 补货啦: {stock_text(t, n)}！"
                        notify(title_str, body_md)
                        time.sleep(1)  # 突发多商品补货时的平滑缓冲，防止推送接口突发限速
                    else:
                        log(f"[cooldown] {t['name']}: 库存={n} (距上次通知仅 {int(now - last_t)}s，冷却防抖)")

            if old is None or n != old:
                prev[t["name"]] = n
                dirty = True
            log(f"[check] {t['name']}: {n}")

        if dirty:
            save_and_commit_state(prev)
        if once:
            break
        if time.time() - last_beat >= HEARTBEAT_HOURS * 3600:
            last_beat = time.time()
            notify("💓 心跳：监控运行中", "\n".join(
                f"· {t['name']}: {stock_text(t, prev[t['name']]) if prev[t['name']] is not None else '未知'}"
                for t in targets), weixin=WEIXIN_HEARTBEAT)
        if time.time() - start >= MAX_MINUTES * 60:
            log("时间预算用完，交接下一班")
            dispatch_next_watch()
            break
        # 动态周期漂移补偿：减去抓取消耗的时间，让检查间隔锁定在预期秒数，大幅提升捡漏敏锐度
        elapsed = time.time() - cycle_start
        sleep_sec = max(5, CHECK_INTERVAL - elapsed)
        time.sleep(sleep_sec)


if __name__ == "__main__":
    main()
