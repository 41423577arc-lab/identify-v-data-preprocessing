import unittest
from run_reviewed_preprocessing import conservative_clean, emoji_features
from alias_normalization import AliasNormalizer


class ReviewedPreprocessingTests(unittest.TestCase):
    def test_semantic_signals_and_joined_emoji_survive(self):
        clean=conservative_clean('<p>@选手 不好！[怒] 🐴 👩‍💻 1️⃣ #红蝶# https://t.cn/abc </p>')
        for term in ['不好','[怒]','🐴','👩‍💻','红蝶']:
            self.assertIn(term,clean)
        self.assertNotIn('http',clean)
        self.assertEqual(emoji_features(clean),['[怒]','🐴','👩‍💻','1️⃣'])

    def test_repeated_emotion_is_not_collapsed(self):
        self.assertEqual(conservative_clean('不不不！好好好[怒][怒]'),'不不不！好好好[怒][怒]')

    def test_record_scoped_label_has_priority_and_does_not_leak(self):
        entry=dict(surface='回忆',canonical='WBG_Huiyi',term_type='neutral_alias',weight=10000,
                   category='player',sensitive=0,toxicity=None,review_status='context_required',context_any=['比赛'])
        rule=dict(weibo_id='c1',surface='回忆',canonical='小女孩',term_type='neutral_alias',category='role')
        n=AliasNormalizer([entry],[rule])
        self.assertEqual(n.normalize('回忆蓝皮','c1')[0],'小女孩蓝皮')
        self.assertEqual(n.normalize('回忆蓝皮','c2')[0],'回忆蓝皮')


if __name__=='__main__':
    unittest.main()
