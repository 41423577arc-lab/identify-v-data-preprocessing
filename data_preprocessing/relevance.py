"""可维护的主题规则；不读取微博 ID、检索词或人工参考标签。"""
import html
from html.parser import HTMLParser
import re
import unicodedata


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li"} and not self.hidden:
            self.parts.append("\n")
        if tag == "img" and not self.hidden:
            # 图片表情用 alt 保留；不删除攻击性词语和网络缩写。
            self.parts.append(dict(attrs).get("alt", ""))

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_text(text):
    parser = TextExtractor()
    parser.feed(text)
    parser.close()
    return html.unescape("".join(parser.parts)).strip()


def matching_text(text):
    # 规范化仅用于匹配，不改写研究用正文。
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\u200b-\u200f\ufeff]", "", text)


def term_hits(text, terms):
    hits = []
    for term in terms:
        value = matching_text(term)
        if value.isascii() and re.fullmatch(r"[a-z0-9]+", value):
            found = re.search(r"(?<![a-z0-9])" + re.escape(value) + r"(?![a-z0-9])", text)
        else:
            found = value in text
        if found:
            hits.append(term)
    return hits


class RuleClassifier:
    def __init__(self, config):
        self.config = config

    def classify(self, content):
        c, t = self.config, matching_text(content)
        hits = {name: term_hits(t, c[name]) for name in (
            "game_names", "contextual_aliases", "game_terms", "character_terms",
            "event_names", "team_candidates", "player_candidates", "ambiguous_terms",
            "event_context", "other_games", "other_game_specific", "other_sports",
            "other_sport_people", "other_sport_context",
            "promotion_terms", "contact_terms", "life_terms", "general_life_themes")}
        compact = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
        compact = compact.replace("蚣众呺", "公众号")
        novel_structure = term_hits(compact, c["novel_structure"])
        novel_funnel = term_hits(compact, c["novel_funnel"])
        th = c["thresholds"]
        explicit = bool(hits["game_names"])
        domain = hits["game_terms"]
        characters = hits["character_terms"]
        people = hits["player_candidates"]
        strong_domain = [term for term in domain if term not in c["ambiguous_game_terms"]]
        other_sport = bool(hits["other_sports"] or
                           hits["other_sport_people"] and len(hits["other_sport_context"]) >= 2)
        competing = other_sport or bool(hits["other_games"])
        # “第五次＋亚运”“约瑟夫＋游泳”不能成为游戏证据。
        contextual = bool(
            (hits["contextual_aliases"] or characters) and strong_domain or
            people and (strong_domain or hits["event_context"] and not competing) or
            (hits["team_candidates"] or hits["event_names"]) and (strong_domain or "电竞" in t))
        domain_pair = len(strong_domain) >= 2 or bool(characters and strong_domain)
        evidence = explicit or contextual or domain_pair
        # 口语日志常用逗号和省略号断句，不能把整段算作游戏讨论。
        sentences = [s for s in re.split(r"[。！？!?，,；;\n]+|…+|\.{3,}", t) if s.strip()]
        anchors = c["game_names"] + strong_domain
        relevant_chars = sum(len(s) for s in sentences if term_hits(s, anchors))
        share = relevant_chars / max(1, sum(map(len, sentences)))
        other_game = bool(hits["other_games"] or
                          len(hits["other_game_specific"]) >= th["other_game_specific_min_terms"])
        novel = len(novel_structure) >= th["novel_min_structure_terms"] and bool(novel_funnel)
        promo = bool(hits["promotion_terms"]) and bool(
            hits["contact_terms"] or re.search(r"\d+(?:\.\d+)?\s*(?:r|元|/周|/月)", t))
        spam = "novel_funnel" if novel else (
            "other_game_promotion" if other_game and promo else "none")
        repeated = sum(t.count(matching_text(s)) for s in c["novel_funnel"]) >= 3
        rules = []
        if novel:
            rules.append("SPAM_NOVEL_COMBINATION")
        if repeated and novel:
            rules.append("SPAM_REPEATED_TEMPLATE")
        if other_game and promo:
            rules.append("SPAM_OTHER_GAME_PROMOTION")

        def result(label, reason, rule, game, event):
            return {"relevance_label": label, "reason": reason,
                    "matched_rules": rules + [rule], "game_related": game,
                    "event_related": event, "spam_type": spam,
                    # 没有校准样本时不伪造置信概率。
                    "confidence": "", "signals": hits,
                    "related_sentence_share": round(share, 4)}

        event = "yes" if evidence and hits["event_context"] else "unknown"
        if not content.strip():
            return result("uncertain", "缺少正文，不能判断主题", "MISSING_CONTENT", "unknown", "unknown")
        if evidence:
            diary = (len(t) >= th["diary_min_chars"] and
                     len(hits["life_terms"]) >= th["diary_min_life_terms"] and
                     share < th["diary_max_related_share"])
            mixed = ((other_game or other_sport or novel) and
                     (not explicit or len(t) >= th["mixed_topic_min_chars"] and
                      share < th["related_sentence_share"] or not domain and t.count("第五人格") <= 1))
            if diary or mixed:
                return result("uncertain", "存在第五人格线索，但生活/其他主题占主要篇幅，需人工确定范围",
                              "MIXED_OR_DIARY_TOPIC", "mentioned", "mentioned" if hits["event_context"] else "unknown")
            if explicit or domain_pair:
                return result("clearly_related", "游戏名或多项游戏语境支持相关，未发现明显异主题主导",
                              "GAME_SUBSTANTIVE", "yes", event)
            return result("likely_related", "人物/简称与对局或赛事语境共同出现，保留待确认",
                          "CONTEXTUAL_GAME", "likely", "likely" if hits["event_context"] else "unknown")
        if novel:
            return result("clearly_unrelated", "小说结构与全文阅读引流同时出现，且缺少实质游戏讨论",
                          "UNRELATED_NOVEL", "no", "no")
        if other_game:
            return result("clearly_unrelated", "主要指向其他游戏，缺少第五人格实质讨论",
                          "UNRELATED_OTHER_GAME", "no", "no")
        if other_sport:
            return result("clearly_unrelated", "明确的其他体育项目语境，亚运/冠军不构成游戏相关证据",
                          "UNRELATED_OTHER_SPORT", "no", "no")
        if len(t) >= th["life_min_chars"] and len(hits["general_life_themes"]) >= th["life_min_themes"]:
            return result("clearly_unrelated", "多项泛生活/风水主题共同出现，没有实质游戏讨论",
                          "UNRELATED_GENERAL_LIFE", "no", "no")
        if domain or characters or people or hits["contextual_aliases"] or hits["event_names"]:
            return result("likely_related", "有游戏候选线索但语境不足，保留待确认",
                          "WEAK_GAME_CLUE", "likely", "unknown")
        return result("uncertain", "信息不足；检索词和亚运/冠军等歧义词不作为排除或放行依据",
                      "INSUFFICIENT_CONTEXT", "unknown", "unknown")
