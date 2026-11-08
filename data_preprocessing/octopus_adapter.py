"""八爪鱼网页字段适配；仅转换输入表示，不改变预处理判定规则。"""
from datetime import datetime
import re
from urllib.parse import urlsplit


def weibo_id_from_url(url):
    parts = urlsplit(url.strip())
    if parts.hostname not in {"weibo.com", "www.weibo.com", "m.weibo.cn"}:
        return ""
    path = parts.path.strip("/").split("/")
    if len(path) != 2 or not (path[0].isdigit() or path[0] in {"detail", "status"}):
        return ""
    token = path[1]
    if re.fullmatch(r"[0-9]{15,}", token):
        return token
    if not re.fullmatch(r"[a-zA-Z0-9]{5,12}", token):
        return ""
    # 与现有爬虫 spiders/common.py 的 url_to_mid 相同：右起四位解码并补七位。
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    chunks = [token[max(0, end - 4):end] for end in range(len(token), 0, -4)][::-1]
    decoded = []
    for index, chunk in enumerate(chunks):
        value = 0
        for char in chunk:
            value = value * 62 + alphabet.index(char)
        decoded.append(str(value).zfill(7) if index else str(value))
    return str(int("".join(decoded)))


def normalize_count(value, placeholder):
    value = value.strip().replace(",", "")
    if value == placeholder:
        # 按钮标签没有给出可观测数值，留空而不编造零。
        return ""
    if re.fullmatch(r"[0-9]+", value):
        return value
    # 万/亿为网页舍入展示值，保留原值交给质量检查送审。
    return value


def adapt_octopus(raw, year=None):
    result = dict(raw)
    for field in ("created_at", "comments_count", "likes_count"):
        result["octopus_original_" + field] = result.get(field, "")
    if not result.get("weibo_id", "").strip():
        result["weibo_id"] = weibo_id_from_url(result.get("from_链接", ""))
    date = result.get("created_at", "").strip()
    if year and re.fullmatch(r"\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}", date):
        try:
            date = datetime.strptime(f"{year}年{date}", "%Y年%m月%d日 %H:%M").isoformat(sep=" ")
        except ValueError:
            pass
    result["created_at"] = date
    result["comments_count"] = normalize_count(result.get("comments_count", ""), "评论")
    result["likes_count"] = normalize_count(result.get("likes_count", ""), "赞")
    return result
