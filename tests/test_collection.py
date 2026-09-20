import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('reader_collection', Path(__file__).resolve().parents[1] / 'scripts' / 'collection.py')
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)


TEXT = '这是测试文章正文，完整保存可用于后续整理。' * 12


def config(accounts=None, **selection):
    return {'accounts': accounts or [{'name': '测试公众号', 'id': 'MP_WXS_123'}],
            'collector': {'directory_url': ''}, 'selection': {'max_articles_per_account': 200, **selection}}


def article(identifier='one', **values):
    return {'id': identifier, 'mp_id': 'MP_WXS_123', 'title': '正文标题',
            'url': 'https://mp.weixin.qq.com/s/' + identifier,
            'content': '<p>' + TEXT + '</p>', 'publish_time': 1780000000, **values}


def url_config(urls, **selection):
    return {'accounts': [], 'article_urls': urls, 'selection': {'max_articles_per_account': 20, **selection}}


def direct_body(url, name='链接公众号'):
    return {'title': '链接标题', 'account': name, 'account_id': 'MP_WXS_789', 'url': url,
            'original_wechat_url': url, 'content_text': TEXT, 'content_kind': 'fulltext',
            'content_hash': c.digest(TEXT), 'published_at': '2026-09-19T08:00:00+00:00',
            'date_source': 'original_page_timestamp', 'fetched_at': c._now(), 'provenance': '原文在线读取'}


class FakeClient:
    base = 'http://localhost/api/v1/wx'

    def __init__(self, rows=None, new=None, fail_collect=False):
        self.rows = rows or []
        self.new = new or []
        self.fail_collect = fail_collect
        self.paths = []

    def call(self, path, payload=None):
        self.paths.append(path)
        if path == '/weread/mp/test':
            return {'mp_name': '测试公众号'}
        if path == '/weread/collect':
            if self.fail_collect:
                raise c.CollectionError('wechat_login_required', '登录失效')
            return {'collected': len(self.new), 'articles': self.new}
        if path.startswith('/articles?'):
            query = c.urllib.parse.parse_qs(c.urllib.parse.urlsplit(path).query)
            offset = int(query['offset'][0])
            return {'total': len(self.rows), 'list': [{'id': row['id']} for row in self.rows[offset:offset + 100]]}
        if path.startswith('/articles/'):
            return next(row for row in self.rows if row['id'] == path.rsplit('/', 1)[-1])
        if path.startswith('/mps'):
            return {'total': 0, 'list': []}
        raise AssertionError(path)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_collect(self, settings, client):
        with patch.object(c, 'Client', return_value=client):
            return c.collect(settings, self.out, self.out)

    def test_database_paging_recovers_more_than_twenty_collect_results(self):
        rows = [article(str(index)) for index in range(101)]
        client = FakeClient(rows=rows, new=rows[:20])
        result = self.run_collect(config(), client)
        self.assertEqual(len(result['articles']), 101)
        self.assertTrue(any('offset=100' in path for path in client.paths))
        self.assertTrue((self.out / result['articles'][0]['original_file']).exists())
        self.assertEqual(result['articles'][0]['published_at'], '')
        self.assertEqual(result['coverage']['historical_coverage'], 'unknown')

    def test_empty_increment_retains_originals_only_for_same_config(self):
        first = self.run_collect(config(), FakeClient(new=[article()]))
        again = self.run_collect(config(), FakeClient())
        self.assertEqual(again['coverage']['reused_articles'], 1)
        self.assertEqual(first['articles'][0]['content_hash'], again['articles'][0]['content_hash'])
        changed = self.run_collect(config(include_keywords=['不会出现的词']), FakeClient())
        self.assertEqual(changed['articles'], [])
        self.assertFalse((self.out / first['articles'][0]['original_file']).exists())

    def test_upstream_failure_with_cached_text_is_partial_not_success(self):
        self.run_collect(config(), FakeClient(new=[article()]))
        again = self.run_collect(config(), FakeClient(fail_collect=True))
        self.assertEqual(again['coverage']['status'], 'partial')
        self.assertTrue(any(issue['code'] == 'wechat_login_required' for issue in again['issues']))

    def test_summary_never_exported_as_body(self):
        result = self.run_collect(config(), FakeClient(new=[article(content='', description=TEXT)]))
        self.assertEqual(result['articles'], [])
        self.assertEqual(result['coverage']['status'], 'failed')
        self.assertTrue(any(issue['code'] == 'body_missing' for issue in result['issues']))

    def test_database_page_limit_reported(self):
        settings = config()
        settings['collector']['max_pages'] = 1
        result = self.run_collect(settings, FakeClient(rows=[article(str(i)) for i in range(101)]))
        self.assertEqual(len(result['articles']), 100)
        self.assertTrue(result['coverage']['sources'][0]['list_truncated'])

    def test_ambiguous_name_never_picks_first_directory_entry(self):
        with patch.object(c, 'Client', return_value=FakeClient()), patch.object(c, '_directory', return_value=[
                {'name': '测试公众号', 'id': 'MP_WXS_123'}, {'name': '测试公众号', 'id': 'MP_WXS_456'}]):
            result = c.resolve(config(['测试公众号']), self.out)
        self.assertEqual(result['accounts'][0]['status'], 'ambiguous')
        self.assertNotIn('id', result['accounts'][0])

    def test_explicit_id_still_requires_current_name_match(self):
        client = FakeClient()
        client.call = lambda *args: {'mp_name': '别人的公众号'}
        with patch.object(c, 'Client', return_value=client):
            result = c.resolve(config(), self.out)
        self.assertEqual(result['accounts'][0]['status'], 'unresolved')
        self.assertTrue(any(issue['code'] == 'account_name_mismatch' for issue in result['issues']))

    def test_unavailable_backend_does_not_look_like_empty_publisher(self):
        client = FakeClient()
        def failed(*args):
            raise c.CollectionError('collector_unavailable', '采集服务未启动')
        client.call = failed
        with patch.object(c, 'Client', return_value=client):
            result = c.resolve(config(['测试公众号']), self.out)
        self.assertEqual(result['accounts'][0]['status'], 'unresolved')
        self.assertTrue(any(issue['code'] == 'collector_unavailable' for issue in result['issues']))

    def test_date_policy_never_uses_backend_capture_time(self):
        result = self.run_collect(config(unknown_date='exclude'), FakeClient(new=[article()]))
        self.assertEqual(result['articles'], [])
        self.assertEqual(result['coverage']['excluded']['unknown_date'], 1)

    def test_import_validates_hash_and_keeps_only_requested_publishers(self):
        rows = [dict(account='测试公众号', title='原标题', url='https://mp.weixin.qq.com/s/one',
                     content_text=TEXT, content_kind='fulltext', content_hash=c.digest(TEXT)),
                dict(account='其他公众号', url='https://mp.weixin.qq.com/s/two', content_text=TEXT, content_kind='fulltext'),
                dict(account='测试公众号', url='https://mp.weixin.qq.com/s/three', content_text=TEXT,
                     content_kind='fulltext', content_hash='modified')]
        result = c.import_articles(config(), rows, self.out)
        self.assertEqual(len(result['articles']), 1)
        self.assertTrue(any(issue['code'] == 'content_hash_mismatch' for issue in result['issues']))
        self.assertIn('未在线重新核验', result['articles'][0]['provenance'])

    def test_supplied_article_identity_mismatch_fails(self):
        raw = '<div id="js_name">其他公众号</div><div id="activity-name">标题</div><div id="js_content">' + TEXT + '</div>'
        with patch.object(c, 'request', return_value=(raw, 'https://mp.weixin.qq.com/s/one')):
            with self.assertRaisesRegex(c.CollectionError, '公众号名与输入不一致'):
                c._direct_article('https://mp.weixin.qq.com/s/one', '测试公众号')

    def test_article_link_with_verified_account_id_collects_account(self):
        settings = config([{'name': '测试公众号', 'article_url': 'https://mp.weixin.qq.com/s/seed'}])
        client = FakeClient(new=[article('another')])
        with patch.object(c, '_direct_article', return_value={'account_id': 'MP_WXS_123'}):
            result = self.run_collect(settings, client)
        self.assertIn('/weread/collect', client.paths)
        self.assertEqual(result['accounts'][0]['status'], 'resolved')
        self.assertEqual(result['articles'][0]['url'], 'https://mp.weixin.qq.com/s/another')

    def test_prefixed_backend_id_normalized_and_export_id_present(self):
        result = self.run_collect(config(), FakeClient(new=[article(mp_id='WEREAD_MP_WXS_123')]))
        self.assertEqual(len(result['articles']), 1)
        self.assertEqual(result['articles'][0]['id'], result['articles'][0]['article_id'])

    def test_new_body_not_lost_when_database_persistence_is_delayed(self):
        result = self.run_collect(config(), FakeClient(rows=[article(content='')], new=[article()]))
        self.assertEqual(len(result['articles']), 1)
        self.assertFalse(any(issue['code'] == 'body_missing' for issue in result['issues']))

    def test_only_managed_previous_files_are_removed(self):
        self.run_collect(config(), FakeClient(new=[article()]))
        other = self.out / 'originals' / 'my-note.md'
        other.write_text('user note', encoding='utf-8')
        self.run_collect(config(include_keywords=['不匹配']), FakeClient())
        self.assertTrue(other.exists())

    def test_pure_links_cross_publishers_never_construct_collector_or_expand_account(self):
        urls = ['https://mp.weixin.qq.com/s/one', 'https://mp.weixin.qq.com/s/two']
        settings = url_config(urls)
        with patch.object(c, 'Client', side_effect=AssertionError('collector must not be used')):
            resolved = c.resolve(settings, self.out)
            self.assertEqual(len(resolved['links']), 2)
            with patch.object(c, '_direct_article', side_effect=lambda url: direct_body(url, url.rsplit('/', 1)[-1])) as fetch:
                result = c.collect(settings, self.out, self.out)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual({a['account'] for a in result['articles']}, {'one', 'two'})
        self.assertEqual(result['coverage']['links']['succeeded'], 2)
        self.assertEqual(result['coverage']['historical_coverage'], 'not_requested')
        self.assertTrue(all(s['scope'] == 'one_supplied_article' for s in result['coverage']['sources']))

    def test_link_normalization_deduplicates_tracking_but_retains_idx(self):
        base = 'https://mp.weixin.qq.com/s?__biz=MTIz&mid=1&idx='
        settings = url_config([base + '1&sn=abc&scene=2', base + '1&sn=abc&from=share', base + '2&sn=abc'])
        with patch.object(c, '_direct_article', side_effect=direct_body) as fetch:
            result = c.collect(settings, self.out, self.out)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(result['articles']), 2)
        self.assertEqual(result['coverage']['links']['requested'], 3)
        self.assertEqual(result['coverage']['links']['duplicates'], 1)
        self.assertNotEqual(result['articles'][0]['id'], result['articles'][1]['id'])

    def test_partial_link_failure_does_not_stop_other_links(self):
        urls = ['https://mp.weixin.qq.com/s/bad', 'https://mp.weixin.qq.com/s/good']
        def fetch(url):
            if url.endswith('/bad'):
                raise c.CollectionError('captcha_required', '需要验证码')
            return direct_body(url)
        with patch.object(c, '_direct_article', side_effect=fetch):
            result = c.collect(url_config(urls), self.out, self.out)
        self.assertEqual(len(result['articles']), 1)
        self.assertEqual(result['coverage']['links']['failed'], 1)
        self.assertEqual(result['coverage']['links']['succeeded'], 1)
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(result['links'][0]['error_code'], 'captcha_required')

    def test_unknown_site_is_rejected_before_fetch_and_valid_link_continues(self):
        urls = ['https://example.com/s/no', 'https://mp.weixin.qq.com/s/yes']
        with patch.object(c, '_direct_article', side_effect=direct_body) as fetch:
            result = c.collect(url_config(urls), self.out, self.out)
        fetch.assert_called_once_with(urls[1])
        self.assertEqual(result['coverage']['links']['failed'], 1)
        with self.assertRaises(c.CollectionError):
            c.normalize_article_url('https://user:password@mp.weixin.qq.com/s/no')
        with self.assertRaises(c.CollectionError):
            c.normalize_article_url('https://mp.weixin.qq.com/cgi-bin/login')
        for invalid in ('https://mp.weixin.qq.com/s', 'https://mp.weixin.qq.com/s/one/two',
                        'https://mp.weixin.qq.com/s/bad token'):
            with self.assertRaises(c.CollectionError):
                c.normalize_article_url(invalid)

    def test_direct_page_extracts_publisher_without_supplied_name(self):
        raw = '<div id="js_name">不同公众号</div><div id="activity-name">真实标题</div><div id="js_content">' + TEXT + '</div><script>var ct = "1780000000"; var biz = "MTIz";</script>'
        with patch.object(c, 'request', return_value=(raw, 'https://mp.weixin.qq.com/s/one')):
            body = c._direct_article('https://mp.weixin.qq.com/s/one')
        self.assertEqual(body['account'], '不同公众号')
        self.assertEqual(body['account_id'], 'MP_WXS_123')
        self.assertTrue(body['published_at'])

    def test_captcha_and_offsite_redirect_are_not_read_as_article(self):
        with patch.object(c, 'request', return_value=('<html>captcha</html>', 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha')):
            with self.assertRaisesRegex(c.CollectionError, '验证码'):
                c._direct_article('https://mp.weixin.qq.com/s/one')
        with patch.object(c, 'request', return_value=('<html></html>', 'https://example.com/s/article')):
            with self.assertRaises(c.CollectionError):
                c._direct_article('https://mp.weixin.qq.com/s/one')

    def test_mixed_sources_keep_named_account_and_explicit_other_publisher(self):
        settings = config()
        settings['article_urls'] = ['https://mp.weixin.qq.com/s/other']
        with patch.object(c, '_direct_article', side_effect=direct_body):
            result = self.run_collect(settings, FakeClient(new=[article('named')]))
        self.assertEqual(len(result['articles']), 2)
        self.assertEqual({a['account'] for a in result['articles']}, {'测试公众号', '链接公众号'})

    def test_changed_url_config_does_not_reuse_another_publishers_text(self):
        first_url = 'https://mp.weixin.qq.com/s/first'
        with patch.object(c, '_direct_article', side_effect=direct_body):
            first = c.collect(url_config([first_url]), self.out, self.out)
        with patch.object(c, '_direct_article', side_effect=c.CollectionError('captcha_required', '验证码')):
            changed = c.collect(url_config(['https://mp.weixin.qq.com/s/second']), self.out, self.out)
        self.assertEqual(changed['articles'], [])
        self.assertFalse((self.out / first['articles'][0]['original_file']).exists())

    def test_failed_refetch_keeps_cache_explicitly_failed_and_reused(self):
        settings = url_config(['https://mp.weixin.qq.com/s/one'])
        with patch.object(c, '_direct_article', side_effect=direct_body):
            c.collect(settings, self.out, self.out)
        with patch.object(c, '_direct_article', side_effect=c.CollectionError('captcha_required', '验证码')):
            result = c.collect(settings, self.out, self.out)
        self.assertEqual(result['coverage']['links']['succeeded'], 0)
        self.assertEqual(result['coverage']['links']['failed'], 1)
        self.assertEqual(result['coverage']['links']['reused'], 1)
        self.assertTrue(result['articles'][0]['reused_from_previous_run'])
        self.assertEqual(result['articles'][0]['last_attempt_status'], 'failed')
        self.assertIn('本轮读取失败', (self.out / result['articles'][0]['original_file']).read_text(encoding='utf-8'))

    def test_link_import_allows_only_specified_url_even_same_publisher(self):
        requested = 'https://mp.weixin.qq.com/s/one'
        rows = [direct_body(requested + '?scene=1'), direct_body('https://mp.weixin.qq.com/s/two')]
        result = c.import_articles(url_config([requested]), rows, self.out)
        self.assertEqual(len(result['articles']), 1)
        self.assertEqual(result['coverage']['links']['imported'], 1)
        self.assertEqual(result['articles'][0]['url'], requested)

    def test_link_filtering_is_counted_separately_from_fetch_failure(self):
        settings = url_config(['https://mp.weixin.qq.com/s/one'], include_keywords=['没有匹配的词'])
        with patch.object(c, '_direct_article', side_effect=direct_body):
            result = c.collect(settings, self.out, self.out)
        self.assertEqual(result['coverage']['links']['succeeded'], 1)
        self.assertEqual(result['coverage']['links']['failed'], 0)
        self.assertEqual(result['coverage']['links']['filtered'], 1)
        self.assertEqual(result['links'][0]['selection_reason'], 'include_keywords')


if __name__ == '__main__':
    unittest.main()
