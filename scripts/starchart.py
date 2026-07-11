#!/usr/bin/env python3
"""
starchart.py - 生成 GitHub 仓库 Star 历史曲线图 (SVG)

用法：
    export GH_TOKEN=ghp_xxx
    export GH_REPOS="cmliu/edgetunnel"     # 明文，支持逗号分隔多个仓库（也可用旧名 REPOS）
    python scripts/starchart.py
  也可直接传参：python scripts/starchart.py owner/repo1 owner/repo2
"""
import os
import sys
import time

import requests
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")  # 无界面（headless）后端，适合在服务器/CI 运行
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

PER_PAGE = 100          # GitHub 每页最大 100 条 stargazers
MAX_PAGES_FULL = 10     # 仓库页数 <= 此值时全量抓取（<=1000 stars）
MAX_SAMPLES = 20        # 大仓库最多抽样的页数
STAR_PAGE_CAP = 400     # GitHub stargazers 翻页硬上限（前 400 页 / 40000 stars）
REQUEST_TIMEOUT = 30


def _parse_iso(s):
    """解析 GitHub 的 ISO 时间字符串为带 UTC 时区的 datetime；失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def _check_rate_limit(resp, repo):
    """若命中 GitHub API 限流，抛出带重置时间的明确错误。"""
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None and int(remaining) <= 0:
        reset = resp.headers.get("X-RateLimit-Reset")
        msg = f"{repo}: GitHub API 速率限制已耗尽"
        if reset:
            reset_dt = datetime.fromtimestamp(int(reset), tz=timezone.utc)
            msg += f"（预计 {reset_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} 重置）"
        raise RuntimeError(msg)


def _fetch_json(url, headers, repo, tries=2):
    """带重试的 GET JSON；返回 (data, resp)，网络/状态码异常时返回 (None, None)。"""
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last_err = f"网络错误: {e}"
            if attempt < tries:
                time.sleep(1.5 * attempt)
                continue
            print(f"  ! {repo}: {last_err}")
            return None, None

        if resp.status_code == 200:
            return resp.json(), resp
        _check_rate_limit(resp, repo)
        last_err = f"HTTP {resp.status_code}"
        if attempt < tries and resp.status_code in (403, 429, 500, 502, 503):
            time.sleep(2 * attempt)
            continue
        print(f"  ! {repo}: 拉取失败 ({last_err}) {resp.text[:120]}")
        return None, None
    return None, None


def collect_star_points(repo, token):
    """返回排序后的 [(datetime, count), ...] 星标累计点。"""
    headers = {
        "Accept": "application/vnd.github.v3.star+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "star-chart-script",
    }
    if token:
        headers["Authorization"] = f"token {token}"

    # 1) 元数据：拿到真实 star 总数与仓库创建时间
    meta, _ = _fetch_json(f"https://api.github.com/repos/{repo}", headers, repo)
    if meta is None:
        return []
    total_stars = int(meta.get("stargazers_count", 0))
    if total_stars == 0:
        print(f"  {repo}: 0 stars，无数据可绘制。")
        return []

    total_pages = (total_stars + PER_PAGE - 1) // PER_PAGE
    # GitHub 对 stargazers 翻页有硬上限：超过 STAR_PAGE_CAP 页一律返回 422。
    # 可拉取的最末页钳制在 400，避免必然失败的请求。
    fetch_last = min(total_pages, STAR_PAGE_CAP)
    if total_pages > STAR_PAGE_CAP:
        print(f"  {repo}: 注意 GitHub 仅允许翻前 {STAR_PAGE_CAP * PER_PAGE} 个 star，"
              f"超出部分的精确时间无法获取（曲线尾部将拉平到今天）。")

    # 2) 决定要抓哪些页
    if total_pages <= MAX_PAGES_FULL:
        page_indices = list(range(1, total_pages + 1))
        strategy = "全量"
    else:
        step = (fetch_last - 1) / (MAX_SAMPLES - 1)
        page_indices = []
        for i in range(MAX_SAMPLES):
            p = round(1 + i * step)
            p = max(1, min(p, STAR_PAGE_CAP))   # 钳制在翻页上限内
            if p not in page_indices:
                page_indices.append(p)
        page_indices.sort()
        if page_indices[-1] != fetch_last:
            page_indices[-1] = fetch_last       # 强制包含可拉取的最末页
        strategy = f"抽样 {len(page_indices)}/{total_pages} 页"

    print(f"  {repo}: 共 {total_stars} stars，{strategy}。")

    # 3) 抓取抽样页，取每页首/尾 star 的时间戳 + 已知序号
    points = []
    for page in page_indices:
        url = (f"https://api.github.com/repos/{repo}/stargazers"
               f"?per_page={PER_PAGE}&page={page}")
        data, _ = _fetch_json(url, headers, repo)
        if not data:
            continue

        # 本页第一条 star 对应全局序号 (page-1)*PER_PAGE + 1
        first_idx = (page - 1) * PER_PAGE + 1
        first_dt = _parse_iso(data[0].get("starred_at"))
        if first_dt:
            points.append((first_dt, first_idx))

        # 可拉取的最末页额外记录最后一条 star => 该页末位的累计序号
        # （被翻页上限截断时，这个数 < total_stars，是能拿到的最末尾真实时间点）
        if page == fetch_last and len(data) > 1:
            last_idx = (page - 1) * PER_PAGE + len(data)
            last_idx = min(last_idx, total_stars)
            last_dt = _parse_iso(data[-1].get("starred_at"))
            if last_dt:
                points.append((last_dt, last_idx))

    # 4) 按时间排序，对 count 去重（保留最早出现的点）
    points.sort(key=lambda t: t[0])
    seen, unique = set(), []
    for dt, cnt in points:
        if cnt not in seen:
            seen.add(cnt)
            unique.append((dt, cnt))

    now = datetime.now(timezone.utc)

    # 5) 兜底：若全部页抓取失败，用「创建时间=0 → 现在=total」画一条最小曲线
    if not unique:
        created = _parse_iso(meta.get("created_at"))
        unique = ([(created, 0)] if created else []) + [(now, total_stars)]
        print(f"  {repo}: 未取到抽样点，使用兜底曲线。")
        return unique

    # 6) 把曲线尾部拉平到「今天 = total_stars」
    last_dt, last_cnt = unique[-1]
    if last_cnt == total_stars:
        if last_dt < now:
            unique.append((now, total_stars))
    else:
        # 抽样缺口：末页没拿到 total_stars，直接从此刻补齐
        unique.append((now, total_stars))
        if total_pages > STAR_PAGE_CAP:
            print(f"  {repo}: 受 GitHub 翻页上限（前 {STAR_PAGE_CAP} 页）限制，"
                  f"曲线尾部已拉平至 {total_stars}。")
        else:
            print(f"  {repo}: 末页未命中，已补足至 {total_stars}。")

    return unique


def plot_chart(repo, points, total_stars):
    # 配置现代无衬线字体，兼容中英文
    # 注意：把 matplotlib 自带的 DejaVu Sans 放进去——
    # Segoe UI 不含 ★ (U+2605) 字形，靠它在回退链里补齐，避免“Glyph missing”警告。
    plt.rcParams['font.sans-serif'] = ['Segoe UI', 'DejaVu Sans', 'Microsoft YaHei', 'Arial', 'sans-serif']
    plt.rcParams['font.family'] = 'sans-serif'
    
    dates = [p[0] for p in points]
    counts = [p[1] for p in points]

    # 创建高分辨率画布
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=200)
    
    # 颜色配置：采用适应性强的颜色，在 Light/Dark 模式透明背景下均有极佳效果
    line_color = "#8250df"    # GitHub 高级紫
    glow_color = "#bf87ff"    # 发光层紫
    text_color = "#7d8590"    # 中性灰（双端清晰）
    grid_color = "#7d8590" 
    
    # 设置全透明背景
    fig.patch.set_alpha(0.0)
    ax.patch.set_alpha(0.0)

    # 1. 绘制发光特效 (Glow Effect)
    for n in range(1, 8):
        ax.plot(dates, counts, color=glow_color, linewidth=2 + (n * 1.2), alpha=0.04, zorder=1)

    # 2. 绘制主曲线
    ax.plot(dates, counts, color=line_color, linewidth=2.5, zorder=2)
    
    # 3. 绘制曲线下方的半透明填充
    ax.fill_between(dates, counts, 0, color=line_color, alpha=0.1, zorder=1)

    # 4. 终点高亮标记（纯装饰：星形矢量 marker，不显示具体数字，
    #    因为每周生成一次，终点的精确数字意义不大，走势线本身才是重点）
    if counts:
        # 终点：星形标记（用矢量 marker 绘制 ★，不依赖字体，避免“Glyph missing”警告）
        ax.scatter(dates[-1], counts[-1], marker='*', color=line_color, s=300,
                   edgecolors='white', linewidths=1, zorder=3)
        # 终点光晕
        ax.scatter(dates[-1], counts[-1], color=glow_color, s=420, alpha=0.25, zorder=2)

    # 5. 美化标题与坐标轴标签
    ax.set_title(f"{repo}", fontsize=24, fontweight="bold", color=text_color, pad=25, loc='center')
    ax.set_xlabel("Time", fontsize=12, color=text_color, labelpad=12)
    ax.set_ylabel("Stargazers", fontsize=12, color=text_color, labelpad=12)
    
    # 6. 设置网格线（非常微弱，提升质感）
    ax.grid(True, linestyle="--", color=grid_color, alpha=0.15, zorder=0)
    
    # 7. 优化边框（移除顶部和右侧，弱化左侧和底部）
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    for spine in ["left", "bottom"]:
        ax.spines[spine].set_color(grid_color)
        ax.spines[spine].set_alpha(0.3)
        ax.spines[spine].set_linewidth(1.5)
        
    ax.tick_params(colors=text_color, width=1.5, length=5, direction='out', labelsize=11)

    # 8. Y轴留白，X轴自适应日期
    if counts:
        ax.set_ylim(0, max(counts) * 1.15)
    ax.margins(x=0.02)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y / %m"))
    # 不传 maxticks，让 AutoDateLocator 自行选择间隔，避免“Defaulting to 6”警告
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    
    fig.tight_layout()

    # 9. 输出并保存
    os.makedirs("star", exist_ok=True)
    out = f"star/{repo.split('/')[-1]}.svg"
    fig.savefig(out, format="svg", transparent=True, bbox_inches="tight")
    plt.close(fig)
    return out


def generate_star_chart(repo, token):
    print(f"Fetching stargazers for {repo} ...")
    points = collect_star_points(repo, token)
    if not points:
        print(f"  {repo}: 没有可用数据，跳过。")
        return
    total = points[-1][1]
    out = plot_chart(repo, points, total)
    print(f"  -> 图表已生成: {out}（{total} stars，{len(points)} 个数据点）")


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("警告: 未设置 GH_TOKEN / GITHUB_TOKEN，将受匿名限流（60 次/小时）影响。")

    # 优先用命令行参数，否则回退到 GH_REPOS / REPOS 环境变量
    args = [a for a in sys.argv[1:] if a.strip()]
    if args:
        repos = args
    else:
        repos_env = os.environ.get("GH_REPOS") or os.environ.get("REPOS", "cmliu/edgetunnel")
        repos = [r.strip() for r in repos_env.split(",") if r.strip()]

    if not repos:
        print("未指定任何仓库（可用 REPOS 环境变量或命令行参数）。")
        return

    for repo in repos:
        # 兼容直接粘贴 github.com/owner/repo 链接
        repo = repo.replace("https://github.com/", "").replace("http://github.com/", "").strip("/")
        if repo.count("/") != 1:
            print(f"跳过无效仓库名: {repo}（应为 owner/repo）")
            continue
        try:
            generate_star_chart(repo, token)
        except RuntimeError as e:
            print(f"  {repo}: {e}")
        except Exception as e:  # 单个仓库失败不影响其余
            print(f"  {repo}: 意外错误 {e}")


if __name__ == "__main__":
    main()
