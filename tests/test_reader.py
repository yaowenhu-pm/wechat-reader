import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import reader

URL_ONE = 'https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_ONE'
URL_TWO = 'https://mp.weixin.qq.com/s/EXAMPLE_ARTICLE_TWO'


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.config = reader.read_json(reader.ROOT / 'examples' / 'config.json')

    def test_date_order_and_duplicate_sections(self):
        config = copy.deepcopy(self.config)
        config['selection'].update(since='2026-09-20', until='2026-09-01')
        with self.assertRaises(ValueError):
            reader.validate_config(config)
        config = copy.deepcopy(self.config)
        config['output']['sections'].append(config['output']['sections'][0])
        with self.assertRaises(ValueError):
            reader.validate_config(config)

    def test_typos_fail_and_custom_sections_work(self):
        self.config['output']['sections'] = [{'id': 'anything', 'title': '自选栏目', 'instructions': '自选规则'}]
        reader.validate_config(self.config)
        self.config['selection']['max_article'] = 5
        with self.assertRaises(ValueError):
            reader.validate_config(self.config)

    def test_prepare_excludes_secrets_and_rejects_modified_body(self):
        article = {'id': 'one', 'account': '动脉网', 'title': '测试', 'url': 'https://example.com/1',
                   'content_text': '测试正文', 'content_kind': 'fulltext', 'content_hash': reader.digest('测试正文')}
        self.config['collector']['credentials_file'] = 'SECRET_FILE'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'articles.json'
            reader.write_json(path, [article])
            reader.prepare(self.config, path, Path(temp) / 'out')
            payload = (Path(temp) / 'out' / 'reading-pack.json').read_text(encoding='utf-8')
            self.assertNotIn('SECRET_FILE', payload)
            article['content_text'] += '被修改'
            reader.write_json(path, [article])
            with self.assertRaises(ValueError):
                reader.prepare(self.config, path, Path(temp) / 'other')

    def test_login_status_never_returns_cookies(self):
        with patch('collection.Client') as factory:
            client = factory.return_value
            client.call.side_effect = [{'configured': True, 'cookie': 'SECRET'},
                                       {'login_status': False, 'uid': 'PRIVATE'}]
            result = reader.login_status(self.config, Path('.'))
            self.assertTrue(result['weread_configured'])
            self.assertFalse(result['current_qr_logged_in'])
            self.assertNotIn('SECRET', json.dumps(result))
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_qr_rejects_foreign_image_url(self):
        with patch('collection.Client') as factory:
            client = factory.return_value
            client.base = 'http://127.0.0.1:8006/api/v1/wx'
            client.call.return_value = {'code': 'https://example.com/login.png'}
            with self.assertRaises(ValueError):
                reader.login_status(self.config, Path('.'), qr=True)
            client.call.return_value = {'code': '/static/weread_qrcode.png'}
            result = reader.login_status(self.config, Path('.'), qr=True)
            self.assertEqual(result['image_url'], 'http://127.0.0.1:8006/static/weread_qrcode.png')

    def test_urls_only_config_allows_empty_or_omitted_accounts(self):
        self.config['accounts'] = []
        self.config['article_urls'] = [URL_ONE, URL_TWO]
        self.assertIs(reader.validate_config(self.config), self.config)
        self.config.pop('accounts')
        self.config.pop('collector')
        self.assertIs(reader.validate_config(self.config), self.config)
        self.assertEqual(reader.source_counts(self.config), {'accounts': 0, 'article_urls': 2, 'total': 2})

    def test_no_sources_or_bad_source_types_fail(self):
        self.config['accounts'] = []
        self.config['article_urls'] = []
        with self.assertRaises(ValueError):
            reader.validate_config(self.config)
        self.config.pop('accounts')
        self.config.pop('article_urls')
        with self.assertRaises(ValueError):
            reader.validate_config(self.config)
        for bad_urls in (None, URL_ONE, [None], [''], [123]):
            config = copy.deepcopy(self.config)
            config['article_urls'] = bad_urls
            with self.subTest(article_urls=bad_urls), self.assertRaises(ValueError):
                reader.validate_config(config)

    def test_article_urls_reject_foreign_domains_and_non_article_paths(self):
        self.config['accounts'] = []
        for bad_url in ('https://example.com/s/one', 'https://mp.weixin.qq.com.evil.example/s/one',
                        'file:///s/one', 'https://mp.weixin.qq.com/mp/profile_ext?action=home',
                        'https://name:password@mp.weixin.qq.com/s/one',
                        'https://mp.weixin.qq.com:8000/s/one',
                        'https://mp.weixin.qq.com/s', 'https://mp.weixin.qq.com/s/one/two',
                        'https://mp.weixin.qq.com/s/one two'):
            self.config['article_urls'] = [bad_url]
            with self.subTest(url=bad_url), self.assertRaises(ValueError):
                reader.validate_config(self.config)

    def test_init_accepts_batch_file_comments_bom_and_deduplicates_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sources.txt'
            path.write_text('# 我的文章\n\n  ' + URL_ONE + '?scene=1#wechat_redirect  \n' + URL_TWO + '\n', encoding='utf-8-sig')
            out = Path(temp) / 'config.json'
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(reader.main(['init', '--urls', URL_ONE, '--urls-file', str(path), '--out', str(out)]), 0)
            created = reader.read_json(out)
            self.assertEqual(created['accounts'], [])
            self.assertEqual(created['article_urls'], [URL_ONE, URL_TWO])
            self.assertEqual(json.loads(stdout.getvalue())['sources']['total'], 2)

    def test_init_accepts_mixed_sources_and_preserves_article_idx(self):
        first = 'https://mp.weixin.qq.com/s?__biz=MzA%3D&mid=100&idx=1&sn=abc'
        second = first.replace('idx=1', 'idx=2')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sources.txt'
            path.write_text(second + '\n' + first + '&scene=21\n', encoding='utf-8')
            out = Path(temp) / 'config.json'
            with redirect_stdout(io.StringIO()):
                reader.main(['init', '--accounts', '动脉网', '--urls', first, '--urls-file', str(path), '--out', str(out)])
            created = reader.read_json(out)
            self.assertEqual(created['accounts'], ['动脉网'])
            self.assertEqual(len(created['article_urls']), 2)
            self.assertIn('idx=1', created['article_urls'][0])
            self.assertIn('idx=2', created['article_urls'][1])
            self.assertEqual(reader.source_counts(created)['total'], 3)

    def test_init_empty_source_list_creates_no_config(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'config.json'
            with self.assertRaises(ValueError):
                reader.main(['init', '--accounts', '--urls', '--out', str(out)])
            self.assertFalse(out.exists())

    def test_init_does_not_overwrite_existing_config(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'config.json'
            out.write_text('keep this existing file', encoding='utf-8')
            with self.assertRaises(ValueError):
                reader.main(['init', '--urls', URL_ONE, '--out', str(out)])
            self.assertEqual(out.read_text(encoding='utf-8'), 'keep this existing file')

    def test_validate_outputs_source_counts_for_urls_only(self):
        self.config.pop('accounts')
        self.config['article_urls'] = [URL_ONE, URL_TWO]
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / 'config.json'
            reader.write_json(config_path, self.config)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(reader.main(['validate', '--config', str(config_path)]), 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['sources'], {'accounts': 0, 'article_urls': 2, 'total': 2})
            self.assertEqual(result['accounts'], 0)

    def test_urls_only_status_and_qr_never_create_collector_client(self):
        self.config['accounts'] = []
        self.config['article_urls'] = [URL_ONE]
        with patch('collection.Client') as factory:
            for qr in (False, True):
                result = reader.login_status(self.config, Path('.'), qr=qr)
                self.assertEqual(result['status'], 'direct_urls_no_login_required')
            factory.assert_not_called()

    def test_prepare_accepts_selected_url_without_account_and_rejects_other_links(self):
        self.config['accounts'] = []
        self.config['article_urls'] = [URL_ONE]
        article = {'id': 'one', 'account': '从原文读取的账号', 'title': '测试',
                   'url': URL_ONE + '?scene=21#wechat_redirect',
                   'content_text': '测试正文', 'content_kind': 'fulltext', 'content_hash': reader.digest('测试正文')}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'articles.json'
            reader.write_json(path, [article])
            result = reader.prepare(self.config, path, Path(temp) / 'out')
            self.assertEqual(result['articles'], 1)
            payload = reader.read_json(Path(temp) / 'out' / 'reading-pack.json')
            self.assertEqual(payload['articles'][0]['account'], '从原文读取的账号')
            article['url'] = URL_TWO
            reader.write_json(path, [article])
            with self.assertRaises(ValueError):
                reader.prepare(self.config, path, Path(temp) / 'other')
            article['url'] = 'https://foreign.example/s/one'
            reader.write_json(path, [article])
            with self.assertRaises(ValueError):
                reader.prepare(self.config, path, Path(temp) / 'foreign')

    def test_prepare_does_not_confuse_idx_of_same_mass_mailing(self):
        selected = 'https://mp.weixin.qq.com/s?__biz=MzA%3D&mid=100&idx=1&sn=abc'
        self.config['accounts'] = []
        self.config['article_urls'] = [selected]
        article = {'id': 'one', 'account': '账号', 'url': selected.replace('idx=1', 'idx=2'),
                   'content_text': '正文', 'content_kind': 'fulltext', 'content_hash': reader.digest('正文')}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'articles.json'
            reader.write_json(path, [article])
            with self.assertRaises(ValueError):
                reader.prepare(self.config, path, Path(temp) / 'out')


if __name__ == '__main__':
    unittest.main()
