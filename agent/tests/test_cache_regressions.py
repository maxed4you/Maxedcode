"""Regressions for prompt-cache affinity."""
import unittest

from maxedcode.cachekey import cache_key


class CacheKeyRegressionTests(unittest.TestCase):
    def test_timestamp_sessions_do_not_collide_after_truncation(self):
        one = cache_key("D:/project", "20261022-190601")
        two = cache_key("D:/project", "20261022-190902")
        self.assertNotEqual(one, two)

    def test_child_uses_parent_affinity_group(self):
        parent = cache_key("D:/project", "parent-session")
        child = cache_key("D:/project", "child-session", "parent-session")
        self.assertEqual(child, parent)


if __name__ == "__main__":
    unittest.main()
