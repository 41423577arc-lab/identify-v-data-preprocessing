import unittest
from prepare_linked_dataset import check_relations


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.root = dict(record_key='root', tweet_id='1', comment_level=1,
                         root_record_key='root', root_comment_key='root',
                         parent_comment_key=None, platform_comment_id=None)
        self.reply = dict(record_key='reply', tweet_id='1', comment_level=2,
                          root_record_key='root', root_comment_key='root',
                          parent_comment_key=None, platform_comment_id=None)

    def check(self, reply):
        return check_relations([{'tweet_id': '1'}], [self.root, reply], {'1'})[0][-1]

    def test_thread_membership_does_not_invent_direct_parent(self):
        link = self.check(self.reply)
        self.assertEqual(link['thread_relation_status'], 'verified_from_collection_metadata')
        self.assertEqual(link['direct_parent_status'], 'pending_verification')
        self.assertIsNone(link['direct_parent_record_key'])
        self.assertEqual(self.reply['parent_comment_key'], None)

    def test_unknown_root_is_flagged(self):
        link = self.check({**self.reply, 'root_record_key': 'missing', 'root_comment_key': 'missing'})
        self.assertIn('root_not_found', link['relationship_issues'])

    def test_wrong_post_root_is_flagged(self):
        link = self.check({**self.reply, 'tweet_id': '2'})
        self.assertIn('root_wrong_post_or_level', link['relationship_issues'])

    def test_conflicting_root_keys_are_flagged(self):
        link = self.check({**self.reply, 'root_comment_key': 'other'})
        self.assertIn('conflicting_root_keys', link['relationship_issues'])

    def test_duplicate_local_keys_fail(self):
        with self.assertRaises(ValueError):
            self.check({**self.reply, 'record_key': 'root'})

    def test_explicit_parent_is_separately_verified(self):
        link = self.check({**self.reply, 'parent_comment_key': 'root'})
        self.assertEqual(link['direct_parent_status'], 'verified_from_collection_metadata')


if __name__ == '__main__':
    unittest.main()
