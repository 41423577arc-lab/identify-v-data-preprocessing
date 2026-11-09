import unittest

from prepare_text import real_text_cleaning
from social_text_cleaning import (build_post_match_index, clean_social_text,
                                  match_repost_segments)


class SocialTextCleaningTests(unittest.TestCase):
    def test_badge_prefix_is_removed_and_body_kept(self):
        raw = '第五人格红蝶超话 这个设计需要调整，我不太喜欢现在的颜色搭配 \u200b'
        result = clean_social_text(raw)
        self.assertEqual(result['content_clean'], '这个设计需要调整，我不太喜欢现在的颜色搭配')
        self.assertEqual(result['removed_platform_fragments'][0]['category'], 'weibo_supertopic_badge')

    def test_mixed_topic_header_removes_only_confirmed_supertopics(self):
        raw = '#第五人格亚运会# 第五人格超话 #第五人格红蝶[超话]# 正文保留红蝶和亚运会'
        result = clean_social_text(raw)
        self.assertEqual(result['content_clean'], '第五人格亚运会 正文保留红蝶和亚运会')
        self.assertEqual([x['category'] for x in result['removed_platform_fragments']],
                         ['weibo_supertopic_badge', 'hashtag_supertopic'])

    def test_body_supertopic_mention_is_not_treated_as_a_prefix(self):
        result = clean_social_text('我看到正文里有人写#第五人格[超话]#，这句话属于用户')
        self.assertIn('第五人格[超话]', result['content_clean'])
        self.assertEqual(result['removed_platform_fragments'], [])

    def test_normal_topic_hashtag_remains_as_content(self):
        result = clean_social_text('#第五人格中国队摘金# 恭喜年锦拿下金牌')
        self.assertEqual(result['content_clean'], '第五人格中国队摘金 恭喜年锦拿下金牌')

    def test_column_reference_with_weibo_separator_is_removed_and_saved(self):
        raw = '今天完成了两局练习，结果还不错 \u200b 专栏 · 自制攻略标题 \u200b'
        result = clean_social_text(raw)
        self.assertEqual(result['content_clean'], '今天完成了两局练习，结果还不错')
        self.assertEqual(result['column_reference']['title'], '自制攻略标题')
        self.assertIn('trailing_column_share_title', result['column_reference']['reason'])

    def test_column_only_share_card_is_removed_as_metadata(self):
        result = clean_social_text('专栏 · 第五人格入门攻略')
        self.assertEqual(result['content_clean'], '')
        self.assertEqual(result['column_reference']['title'], '第五人格入门攻略')

    def test_ambiguous_column_sentence_is_retained_and_flagged(self):
        raw = '我最近在看专栏 · 第五人格新手攻略'
        result = clean_social_text(raw)
        self.assertEqual(result['content_clean'], raw)
        self.assertIn('possible_column_reference_needs_review', result['social_cleaning_flags'])

    def test_ordinary_column_mention_is_retained(self):
        result = clean_social_text('我喜欢专栏写的攻略，也喜欢第五人格')
        self.assertIn('专栏写的攻略', result['content_clean'])
        self.assertEqual(result['social_cleaning_flags'], [])

    def test_angle_bracket_emoticon_does_not_hide_column_text(self):
        raw = '接受全天候查岗>⩊<xyt专栏 · 咪˶>ᗜ<˶🛰️xmiquq'
        result = clean_social_text(raw)
        self.assertIn('xyt专栏 · 咪', result['content_clean'])
        self.assertIn('possible_column_reference_needs_review', result['social_cleaning_flags'])

    def test_actual_html_tags_are_still_cleaned(self):
        result = clean_social_text('<p>第五人格<img alt="[怒]" /></p><br>保留表情')
        self.assertEqual(result['content_clean'], '第五人格[怒] 保留表情')

    def test_repost_chain_is_split_without_attributing_quotes_to_current_author(self):
        raw = '我不喜欢这个设计//@反对者:红蝶真的太丑了我被气死//@第二个人:我支持反对者别买'
        result = clean_social_text(raw)
        self.assertEqual(result['content_clean'], '我不喜欢这个设计')
        self.assertEqual([s['author'] for s in result['repost_segments']], ['反对者', '第二个人'])
        self.assertEqual([s['content_clean'] for s in result['repost_segments']],
                         ['红蝶真的太丑了我被气死', '我支持反对者别买'])
        self.assertFalse(any(s['included_in_current_author_body'] for s in result['repost_segments']))

    def test_confirmed_full_unique_repost_source_is_matched(self):
        body = '我在第五人格里反复练习红蝶这张地图终于完成了今天的挑战'
        index = build_post_match_index([{'weibo_id': 'test-source-1', 'user_name': '原作者',
            'created_at': '2026-10-01 10:00:00', 'content': body, 'long_text_complete': 'true'}])
        segments = clean_social_text('转发说明//@原作者:' + body)['repost_segments']
        matched = match_repost_segments(segments, index, current_id='test-current-1',
            current_author='转发者', current_time='2026-10-02 10:00:00')
        self.assertEqual(matched[0]['match_status'], 'confirmed_in_database')
        self.assertTrue(matched[0]['already_independently_collected'])
        self.assertEqual(matched[0]['matched_post_id'], 'test-source-1')

    def test_comment_display_time_with_source_suffix_is_used(self):
        body = '这是一段足够长的第五人格正文，用于验证评论时间中附带来源说明时仍可核对转发先后'
        index = build_post_match_index([{'weibo_id': 'test-source-1', 'user_name': '原作者',
            'created_at': '2026-10-01 10:00:00', 'content': body}])
        segments = clean_social_text('//@原作者:' + body)['repost_segments']
        matched = match_repost_segments(segments, index, current_time='26-10-02 10:00 来自 福建')
        self.assertEqual(matched[0]['match_status'], 'confirmed_in_database')
        self.assertIn('source_time_valid', matched[0]['matching_evidence'])

    def test_short_repost_text_is_retained_and_sent_to_review(self):
        index = build_post_match_index([])
        segments = clean_social_text('//@小明:好看')['repost_segments']
        matched = match_repost_segments(segments, index, current_time='2026-10-02 10:00:00')
        self.assertEqual(matched[0]['match_status'], 'needs_manual_review')
        self.assertIn('short_repost_fragment', matched[0]['match_review_reasons'])
        self.assertEqual(matched[0]['content_clean'], '好看')

    def test_author_mismatch_never_auto_matches(self):
        body = '这是一段足够长的第五人格正文，用来验证转发匹配时作者必须一致'
        index = build_post_match_index([{'weibo_id': 'test-source-1', 'user_name': '另一个作者',
            'created_at': '2026-10-01 10:00:00', 'content': body}])
        segments = clean_social_text('//@原作者:' + body)['repost_segments']
        matched = match_repost_segments(segments, index, current_time='2026-10-02 10:00:00')
        self.assertEqual(matched[0]['match_status'], 'needs_manual_review')
        self.assertIn('author_mismatch_or_missing', matched[0]['match_review_reasons'])
        self.assertFalse(matched[0]['already_independently_collected'])

    def test_non_unique_text_match_is_sent_to_review(self):
        body = '这是一段被两条微博重复发布的第五人格正文用于验证匹配不唯一时不能删除'
        index = build_post_match_index([
            {'weibo_id': 'test-source-1', 'user_name': '原作者', 'created_at': '2026-10-01 10:00:00', 'content': body},
            {'weibo_id': 'test-source-2', 'user_name': '原作者', 'created_at': '2026-10-01 11:00:00', 'content': body},
        ])
        segments = clean_social_text('//@原作者:' + body)['repost_segments']
        matched = match_repost_segments(segments, index, current_time='2026-10-02 10:00:00')
        self.assertEqual(matched[0]['match_status'], 'needs_manual_review')
        self.assertIn('non_unique_database_match', matched[0]['match_review_reasons'])

    def test_truncated_prefix_candidate_is_not_auto_matched(self):
        body = '这是一段很长的第五人格正文，后面的内容还包括红蝶和地图的完整讨论'
        partial = body[:26]
        index = build_post_match_index([{'weibo_id': 'test-source-1', 'user_name': '原作者',
            'created_at': '2026-10-01 10:00:00', 'content': body}])
        segments = clean_social_text('//@原作者:' + partial)['repost_segments']
        matched = match_repost_segments(segments, index, current_time='2026-10-02 10:00:00')
        self.assertEqual(matched[0]['match_status'], 'needs_manual_review')
        self.assertIn('possible_truncated_source_text', matched[0]['match_review_reasons'])

    def test_repost_only_record_is_kept_and_flagged(self):
        result = clean_social_text('//@作者:这是转发的正文但当前作者没有添加原创文字内容')
        self.assertEqual(result['content_clean'], '')
        self.assertIn('repost_only_record', result['social_cleaning_flags'])
        self.assertEqual(result['repost_segments'][0]['content_clean'], '这是转发的正文但当前作者没有添加原创文字内容')

    def test_mentions_emojis_negation_and_attackive_words_are_retained(self):
        raw = '我不是不玩了@被指出来的人 [怒] 😡，别他妈乱说'
        self.assertEqual(real_text_cleaning(raw), raw)


if __name__ == '__main__':
    unittest.main()
