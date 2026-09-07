import io
from datetime import datetime

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def generate_analytics_infographic(
    date_str: str,
    top_channels: list[dict],
    hourly_distribution: list[dict],
    audience_stats: dict,
    summary: dict,
) -> bytes | None:
    """
    Renders a high-resolution 1200x675 dark-mode infographic card
    displaying Audience Reach, Top 5 Channels, and 24-Hour Traffic Heatmap.
    Returns PNG bytes, or None if PIL is not installed.
    """
    if not HAS_PIL:
        return None

    width, height = 1200, 675
    img = Image.new("RGBA", (width, height), "#0F172A")
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("arial.ttf", 26)
        font_subtitle = ImageFont.truetype("arial.ttf", 15)
        font_kpi_val = ImageFont.truetype("arial.ttf", 28)
        font_kpi_lbl = ImageFont.truetype("arial.ttf", 12)
        font_section = ImageFont.truetype("arial.ttf", 18)
        font_body = ImageFont.truetype("arial.ttf", 13)
        font_small = ImageFont.truetype("arial.ttf", 11)
    except Exception:
        font_title = ImageFont.load_default()
        font_subtitle = font_title
        font_kpi_val = font_title
        font_kpi_lbl = font_title
        font_section = font_title
        font_body = font_title
        font_small = font_title

    # 1. Header Banner
    draw.rectangle([(30, 25), (1170, 95)], fill="#1E293B", outline="#334155", width=1)
    draw.text((50, 36), "📊 AUDIENCE & CHANNEL INTELLIGENCE", fill="#38BDF8", font=font_title)
    draw.text((50, 68), f"Forwarder Analytics Digest • {date_str} • Active Channels: {summary.get('active_channels', 0)}", fill="#94A3B8", font=font_subtitle)

    badge_text = f"Total Posts: {summary.get('total_posts', 0)}"
    draw.rectangle([(980, 42), (1150, 78)], fill="#0369A1", outline="#38BDF8", width=1)
    draw.text((1000, 52), badge_text, fill="#FFFFFF", font=font_body)

    # 2. KPI Tiles (4 Tiles)
    kpi_defs = [
        ("TOTAL AUDIENCE REACH", f"{audience_stats.get('totalAudience', 0):,}", "#10B981", "Subscribers & Members"),
        ("NEWSLETTER SUBSCRIBERS", f"{audience_stats.get('newsletterSubscribers', 0):,}", "#818CF8", f"{audience_stats.get('newslettersCount', 0)} Channels"),
        ("GROUP PARTICIPANTS", f"{audience_stats.get('groupMembers', 0):,}", "#38BDF8", f"{audience_stats.get('groupsCount', 0)} Groups"),
        ("PEAK TRAFFIC HOUR", f"{summary.get('peak_hour_label', 'N/A')}", "#F59E0B", f"{summary.get('peak_hour_volume', 0)} Peak Posts"),
    ]

    tile_w = 265
    tile_h = 95
    tile_y = 115
    for i, (label, val, color, desc) in enumerate(kpi_defs):
        tx = 30 + i * (tile_w + 35)
        draw.rectangle([(tx, tile_y), (tx + tile_w, tile_y + tile_h)], fill="#1E293B", outline="#334155", width=1)
        draw.rectangle([(tx, tile_y), (tx + 5, tile_y + tile_h)], fill=color)
        draw.text((tx + 18, tile_y + 12), label, fill="#94A3B8", font=font_kpi_lbl)
        draw.text((tx + 18, tile_y + 32), val, fill=color, font=font_kpi_val)
        draw.text((tx + 18, tile_y + 70), desc, fill="#64748B", font=font_small)

    # 3. Top 5 Most Active Channels (Left Panel: x: 30 to 580)
    left_x = 30
    left_w = 550
    panel_y = 230
    panel_h = 415
    draw.rectangle([(left_x, panel_y), (left_x + left_w, panel_y + panel_h)], fill="#1E293B", outline="#334155", width=1)
    draw.text((left_x + 20, panel_y + 18), "📢 Top 5 Active Channels", fill="#F8FAFC", font=font_section)
    draw.text((left_x + 20, panel_y + 42), "Ranked by post forwarding volume today", fill="#94A3B8", font=font_small)

    bar_max_w = 270
    top_row_y = panel_y + 75
    if top_channels:
        max_posts = max(c.get("post_count", 1) for c in top_channels)
        for idx, c in enumerate(top_channels[:5]):
            cy = top_row_y + idx * 64
            cid = str(c.get("channel_id", "Unknown"))
            cnt = c.get("post_count", 0)
            pct = c.get("percent", 0.0)
            groups = c.get("groups_count", 0)

            draw.text((left_x + 20, cy), f"#{idx+1}", fill="#38BDF8", font=font_body)
            draw.text((left_x + 48, cy), f"{cid}", fill="#F8FAFC", font=font_body)
            draw.text((left_x + 48, cy + 18), f"{cnt} posts ({pct}%) • {groups} grps", fill="#94A3B8", font=font_small)

            bar_len = int((cnt / max(max_posts, 1)) * bar_max_w)
            bx = left_x + 250
            draw.rectangle([(bx, cy + 5), (bx + bar_max_w, cy + 19)], fill="#334155")
            if bar_len > 0:
                draw.rectangle([(bx, cy + 5), (bx + bar_len, cy + 19)], fill="#38BDF8")
    else:
        draw.text((left_x + 20, panel_y + 100), "No channel posts recorded yet today.", fill="#64748B", font=font_body)

    # 4. Peak Volume Hours: 24h Traffic Heatmap (Right Panel: x: 620 to 1170)
    right_x = 620
    right_w = 550
    draw.rectangle([(right_x, panel_y), (right_x + right_w, panel_y + panel_h)], fill="#1E293B", outline="#334155", width=1)
    draw.text((right_x + 20, panel_y + 18), "🔥 Peak Volume Hours (24h Traffic Heatmap)", fill="#F8FAFC", font=font_section)
    draw.text((right_x + 20, panel_y + 42), "Hourly distribution of forwarded posts across all channels", fill="#94A3B8", font=font_small)

    chart_x = right_x + 35
    chart_y = panel_y + 80
    chart_w = 480
    chart_h = 240
    slot_w = chart_w / 24.0

    max_hourly = max((h.get("post_count", 0) for h in hourly_distribution), default=1)
    if max_hourly == 0:
        max_hourly = 1

    draw.line([(chart_x - 5, chart_y + chart_h), (chart_x + chart_w + 5, chart_y + chart_h)], fill="#334155", width=1)

    for h_data in hourly_distribution:
        h = h_data.get("hour", 0)
        cnt = h_data.get("post_count", 0)
        is_peak = h_data.get("is_peak", False)

        bar_h = int((cnt / max_hourly) * (chart_h - 20))
        bx1 = chart_x + h * slot_w + 2
        bx2 = bx1 + slot_w - 4
        by2 = chart_y + chart_h
        by1 = by2 - max(bar_h, 3 if cnt > 0 else 1)

        intensity = cnt / max_hourly
        if is_peak:
            bar_color = "#EF4444"
        elif intensity > 0.6:
            bar_color = "#F59E0B"
        elif intensity > 0.3:
            bar_color = "#38BDF8"
        elif cnt > 0:
            bar_color = "#0284C7"
        else:
            bar_color = "#1E3A5F"

        draw.rectangle([(bx1, by1), (bx2, by2)], fill=bar_color)

        if h % 4 == 0:
            draw.text((bx1 - 4, by2 + 8), f"{h:02d}h", fill="#94A3B8", font=font_small)

    legend_y = panel_y + 360
    draw.rectangle([(right_x + 40, legend_y), (right_x + 55, legend_y + 12)], fill="#0284C7")
    draw.text((right_x + 62, legend_y), "Quiet", fill="#94A3B8", font=font_small)

    draw.rectangle([(right_x + 140, legend_y), (right_x + 155, legend_y + 12)], fill="#38BDF8")
    draw.text((right_x + 162, legend_y), "Moderate", fill="#94A3B8", font=font_small)

    draw.rectangle([(right_x + 260, legend_y), (right_x + 275, legend_y + 12)], fill="#F59E0B")
    draw.text((right_x + 282, legend_y), "High", fill="#94A3B8", font=font_small)

    draw.rectangle([(right_x + 360, legend_y), (right_x + 375, legend_y + 12)], fill="#EF4444")
    draw.text((right_x + 382, legend_y), "Peak Hour", fill="#94A3B8", font=font_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def generate_analytics_text_report(
    top_channels: list[dict],
    hourly_distribution: list[dict],
    audience_stats: dict,
    summary: dict,
) -> str:
    """
    Generates an executive textual representation of the analytics dashboard
    using Unicode bar meters and formatted KPI metrics.
    """
    date_str = summary.get("date_key", datetime.now().strftime("%Y-%m-%d"))
    total_posts = summary.get("total_posts", 0)
    active_channels = summary.get("active_channels", 0)
    peak_lbl = summary.get("peak_hour_label", "N/A")
    peak_vol = summary.get("peak_hour_volume", 0)

    tot_aud = audience_stats.get("totalAudience", 0)
    nl_subs = audience_stats.get("newsletterSubscribers", 0)
    nl_cnt = audience_stats.get("newslettersCount", 0)
    grp_mems = audience_stats.get("groupMembers", 0)
    grp_cnt = audience_stats.get("groupsCount", 0)

    lines = [
        "📊 <b>Audience & Channel Intelligence</b>",
        f"<i>Daily Activity Digest • {date_str}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"👥 <b>Total Audience Reach:</b> <b>{tot_aud:,}</b>",
        f"  • 📢 Newsletters: <b>{nl_subs:,}</b> subscribers ({nl_cnt} channels)",
        f"  • 💬 Groups: <b>{grp_mems:,}</b> members ({grp_cnt} groups)",
        "",
        "📈 <b>Traffic Overview:</b>",
        f"  • Forwarded Posts: <b>{total_posts}</b> across <b>{active_channels}</b> active channels",
        f"  • Peak Traffic Window: <b>{peak_lbl}</b> ({peak_vol} posts)",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🏆 <b>Top 5 Active Channels:</b>",
    ]

    if top_channels:
        for c in top_channels[:5]:
            rank = c.get("rank", 1)
            cid = c.get("channel_id", "Unknown")
            cnt = c.get("post_count", 0)
            pct = c.get("percent", 0.0)

            filled = min(10, max(1, int(round(pct / 10.0)))) if pct > 0 else 0
            bar = "█" * filled + "░" * (10 - filled)
            lines.append(f"<code>{rank}.</code> <code>{cid}</code> — <b>{cnt}</b> posts (<code>{pct}%</code>)")
            lines.append(f"   [{bar}]")
    else:
        lines.append("  <i>No post activity recorded yet today.</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("⏱️ <b>Hourly Traffic Heatmap (4h Windows):</b>")

    buckets = [
        ("00:00 - 04:00", range(0, 4)),
        ("04:00 - 08:00", range(4, 8)),
        ("08:00 - 12:00", range(8, 12)),
        ("12:00 - 16:00", range(12, 16)),
        ("16:00 - 20:00", range(16, 20)),
        ("20:00 - 24:00", range(20, 24)),
    ]

    hourly_map = {h["hour"]: h["post_count"] for h in hourly_distribution}
    max_bucket_vol = max((sum(hourly_map.get(hr, 0) for hr in hours) for _, hours in buckets), default=1)
    if max_bucket_vol == 0:
        max_bucket_vol = 1

    for label, hours in buckets:
        b_vol = sum(hourly_map.get(hr, 0) for hr in hours)
        ratio = b_vol / max_bucket_vol
        bar_len = min(8, int(round(ratio * 8))) if b_vol > 0 else 0
        spark = "█" * bar_len + "░" * (8 - bar_len)
        flame = " 🔥" if ratio >= 0.9 and b_vol > 0 else ""
        lines.append(f"• <code>{label}</code>: [{spark}] {b_vol} posts{flame}")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>Tap 'Top Channels Details' below for full source metrics.</i>")
    return "\n".join(lines)
