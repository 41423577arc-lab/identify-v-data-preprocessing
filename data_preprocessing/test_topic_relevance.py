import unittest

from topic_relevance import EXCLUDED_SUPERTOPICS, classify_source_topic


class SourceTopicRelevanceTests(unittest.TestCase):
    def test_all_requested_source_topics_match_with_and_without_suffix(self):
        for topic in EXCLUDED_SUPERTOPICS:
            with self.subTest(topic=topic):
                self.assertTrue(classify_source_topic({'来源超话': topic})['excluded'])
                self.assertTrue(classify_source_topic({'来源超话': topic + '超话'})['excluded'])

    def test_normalizes_metadata_whitespace_and_compatibility_characters(self):
        self.assertTrue(classify_source_topic({'来源超话': ' ＡＬＣｕ 表白墙 超话 '})['excluded'])

    def test_does_not_exclude_related_topics_by_substring(self):
        for topic in ('第五人格娱乐超话', '超自然行动组联动第五人格超话', 'sky光遇黑市攻略第五人格'):
            with self.subTest(topic=topic):
                result = classify_source_topic({'来源超话': topic})
                self.assertFalse(result['excluded'])
                self.assertEqual(result['status'], 'not_matched')

    def test_missing_topic_is_not_excluded(self):
        self.assertFalse(classify_source_topic({'weibo_id': 'example'})['excluded'])


if __name__ == '__main__':
    unittest.main()
