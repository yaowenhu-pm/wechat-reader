"""Offline tests using generated text and a synthetic one-pixel image; no article fixtures."""
import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'export_originals.py'
SPEC = importlib.util.spec_from_file_location('export_originals_under_test', SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)
HAS_HTML = importlib.util.find_spec('bs4') is not None and importlib.util.find_spec('markdownify') is not None
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z4GcAAAAASUVORK5CYII=')
IMAGE_URL = 'https://images.example.test/photo.jpg'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.out = Path(self.temporary.name) / 'export'
        self.first = chr(65) * 30
        self.second = chr(66) * 30

    def tearDown(self):
        self.temporary.cleanup()

    def article(self, body=None, **overrides):
        body = self.first + '\n\n' + self.second if body is None else body
        record = {'id': 'synthetic-1', 'title': 'Synthetic', 'account': 'Synthetic publisher',
                  'url': 'https://mp.weixin.qq.com/s/SYNTHETIC', 'published_at': '',
                  'content_text': body, 'content_kind': 'fulltext', 'content_hash': exporter._digest_text(body)}
        record.update(overrides)
        return record

    def markdown(self, result, index=0):
        return (self.out / result['articles'][index]['markdown']).read_text(encoding='utf-8')


class PlainAndSafetyTests(Fixture):
    def test_plain_text_is_explicitly_partial_without_claiming_images(self):
        download = Mock()
        result = exporter.export_originals([self.article()], self.out, download=download)
        download.assert_not_called()
        self.assertEqual(result['status'], 'partial')
        row = result['articles'][0]
        self.assertEqual(row['images']['status'], 'unavailable')
        self.assertIsNone(row['images']['references'])
        self.assertIsNone(row['published_at'])
        markdown = self.markdown(result)
        self.assertIn(self.first + '\n\n' + self.second, markdown)
        self.assertIn('发布时间：未知', markdown)
        self.assertIn('\n\n---\n\n', markdown)
        self.assertEqual(json.loads((self.out / 'export-status.json').read_text(encoding='utf-8')), result)

    def test_plain_text_keeps_literal_markdown_and_raw_tags_inert(self):
        body = '* A _ B [C](https://example.test)\nD\n\n<script>E</script>'
        result = exporter.export_originals([self.article(body)], self.out)
        markdown = self.markdown(result)
        self.assertIn(r'\* A \_ B \[C\]\(https://example\.test\)', markdown)
        self.assertIn('  \nD\n\n', markdown)
        self.assertIn('&lt;script&gt;E&lt;/script&gt;', markdown)
        self.assertNotIn('<script>', markdown)

    def test_required_kind_hash_and_body_are_checked(self):
        cases = [self.article(content_kind=None), self.article(content_kind='summary'),
                 self.article(content_hash=''), self.article(content_hash='wrong'),
                 self.article('', content_html='<p>unverified</p>')]
        for index, article in enumerate(cases):
            with self.subTest(case=index):
                result = exporter.export_originals([article], self.out)
                self.assertEqual(result['status'], 'failed')
                self.assertFalse((self.out / 'originals' / article['id'] / 'article.md').exists())
        self.assertEqual(result['articles'][0]['issues'][0]['code'], 'content_text_missing')

    def test_captcha_text_is_never_a_successful_article(self):
        result = exporter.export_originals([self.article('环境异常，请完成安全验证')], self.out)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['articles'][0]['issues'][0]['code'], 'captcha_page')

    def test_missing_date_is_not_replaced_with_today(self):
        result = exporter.export_originals([self.article()], self.out)
        self.assertIn('发布时间：未知', self.markdown(result))
        self.assertIsNone(result['articles'][0]['published_at'])
        self.assertNotIn('generated_at', result)

    def test_unsafe_id_and_duplicate_id_cannot_escape_or_overwrite(self):
        for identifier in ('../escape', '/absolute', 'C:\\absolute', 'CON', 'name/other'):
            result = exporter.export_originals([self.article(id=identifier)], self.out)
            self.assertEqual(result['articles'][0]['issues'][0]['code'], 'invalid_article_id')
        result = exporter.export_originals([self.article(), self.article(self.second)], self.out)
        self.assertEqual(result['articles'][1]['issues'][0]['code'], 'duplicate_article_id')
        self.assertIn(self.first, self.markdown(result))

    def test_invalid_image_urls_are_rejected_without_network(self):
        urls = ['file:///etc/passwd', 'data:image/png;base64,abc', 'javascript:alert(1)',
                'http://localhost/a', 'http://127.0.0.1/a', 'https://10.1.2.3/a',
                'https://169.254.169.254/latest', 'https://[::1]/a',
                'https://host.local/a', 'https://user:password@images.example.test/a',
                'https://images.example.test:8443/a', 'https://images.example.test/a b']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(exporter.ExportError):
                exporter.normalize_image_url(url)
        self.assertEqual(exporter.normalize_image_url('//mmbiz.qpic.cn/a'), 'https://mmbiz.qpic.cn/a')

    def test_dns_private_addresses_are_rejected(self):
        for address in ('127.0.0.1', '10.0.0.8', '169.254.169.254', '192.168.1.1'):
            entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
            with patch.object(exporter.socket, 'getaddrinfo', return_value=entries):
                with self.subTest(address=address), self.assertRaises(exporter.ExportError):
                    exporter._resolve_public_addresses('public.example.test', 443, https=True)

    def test_tun_fake_ip_exception_requires_exact_wechat_host_and_https(self):
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.2.3', 443))]
        with patch.object(exporter.socket, 'getaddrinfo', return_value=entries):
            self.assertEqual(exporter._resolve_public_addresses('mmbiz.qpic.cn', 443, https=True), ['198.18.2.3'])
            for host, port, https in [('other.example.test', 443, True), ('mmbiz.qpic.cn', 80, False),
                                      ('evil.mmbiz.qpic.cn', 443, True), ('mmbiz.qpic.cn.evil.test', 443, True),
                                      ('mmbiz.qpic.cn', 443, False)]:
                with self.subTest(host=host, port=port, https=https), self.assertRaises(exporter.ExportError):
                    exporter._resolve_public_addresses(host, port, https=https)
        with self.assertRaises(exporter.ExportError):
            exporter.normalize_image_url('https://198.18.2.3/a')

    def test_pinned_connection_retains_tls_hostname_verification(self):
        with patch.object(exporter, '_resolve_public_addresses', return_value=['198.18.2.3']), \
                patch.object(exporter.socket, 'create_connection') as connect, \
                patch.object(exporter.ssl, 'create_default_context') as context, \
                patch.object(exporter.http.client, 'HTTPConnection') as connection:
            exporter._open_pinned('https://mmbiz.qpic.cn/a?x=1', 10)
        connect.assert_called_once_with(('198.18.2.3', 443), timeout=10)
        context.return_value.wrap_socket.assert_called_once_with(connect.return_value, server_hostname='mmbiz.qpic.cn')
        headers = connection.return_value.request.call_args[1]['headers']
        self.assertEqual(headers['Host'], 'mmbiz.qpic.cn')
        self.assertNotIn('Cookie', headers)
        self.assertNotIn('Authorization', headers)

    def test_image_type_uses_bytes_instead_of_url_or_untrusted_mime(self):
        self.assertEqual(exporter.image_type(PNG, 'image/jpeg'), ('png', 'image/png'))
        self.assertEqual(exporter.image_type(b'GIF89a' + bytes(10), 'application/octet-stream'), ('gif', 'image/gif'))
        with self.assertRaises(exporter.ExportError):
            exporter.image_type(b'<html>Access denied</html>', 'image/png')
        with self.assertRaises(exporter.ExportError):
            exporter.image_type(b'<svg onload="bad()"></svg>', 'image/svg+xml')

    def test_download_rejects_private_redirect_and_large_body(self):
        connection = Mock()
        response = Mock(status=302)
        response.getheader.side_effect = lambda key: 'https://127.0.0.1/image' if key == 'Location' else None
        with patch.object(exporter, '_open_pinned', return_value=(connection, response)) as opened:
            with self.assertRaises(exporter.ExportError):
                exporter.download_image(IMAGE_URL)
            self.assertEqual(opened.call_count, 1)
        connection.close.assert_called()
        response = Mock(status=200)
        response.getheader.side_effect = lambda key: {'Content-Length': '1000'}.get(key)
        with patch.object(exporter, '_open_pinned', return_value=(connection, response)):
            with self.assertRaises(exporter.ExportError) as caught:
                exporter.download_image(IMAGE_URL, max_bytes=10)
        self.assertEqual(caught.exception.code, 'image_too_large')

    def test_unmanaged_output_is_not_deleted_or_overwritten(self):
        existing = self.out / 'originals' / 'unmanaged' / 'keep.txt'
        existing.parent.mkdir(parents=True)
        existing.write_text('keep', encoding='utf-8')
        with self.assertRaises(exporter.ExportError) as caught:
            exporter.export_originals([self.article()], self.out)
        self.assertEqual(caught.exception.code, 'output_exists')
        self.assertEqual(existing.read_text(), 'keep')

    def test_rerun_never_deletes_previous_good_delivery_even_for_bad_input(self):
        initial = exporter.export_originals([self.article()], self.out)
        previous = self.out / initial['articles'][0]['markdown']
        saved = previous.read_bytes()
        previous_status = (self.out / 'export-status.json').read_bytes()
        unrelated = self.out / 'other.txt'
        unrelated.write_text('keep', encoding='utf-8')
        with self.assertRaises(exporter.ExportError) as caught:
            exporter.export_originals([self.article('环境异常，请完成安全验证')], self.out)
        self.assertEqual(caught.exception.code, 'output_exists')
        self.assertEqual(previous.read_bytes(), saved)
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertEqual((self.out / 'export-status.json').read_bytes(), previous_status)

    def test_cli_is_offline_for_plain_text_and_returns_partial(self):
        path = Path(self.temporary.name) / 'articles.json'
        path.write_text(json.dumps([self.article()]), encoding='utf-8')
        with patch.object(exporter, 'download_image') as download, redirect_stdout(io.StringIO()):
            code = exporter.main(['--articles', str(path), '--out', str(self.out), '--offline'])
        self.assertEqual(code, 2)
        download.assert_not_called()


@unittest.skipUnless(HAS_HTML, 'HTML tests require beautifulsoup4 and markdownify; all downloads are mocked')
class HtmlTests(Fixture):
    def body_html(self, image=''):
        return '<div id="js_content"><p>' + self.first + '</p>' + image + '<p>' + self.second + '</p></div>'

    def test_paragraphs_lazy_images_deduplication_and_type_correction(self):
        source = '<img data-src="' + IMAGE_URL + '" src="data:image/gif;base64,placeholder" alt="one">'
        raw = self.body_html(source + '<p>' + self.first + '</p>' + source.replace('alt="one"', 'alt="two"'))
        article = self.article(self.first + self.first + self.second, content_html=raw)
        download = Mock(return_value=(PNG, 'image/jpeg'))
        result = exporter.export_originals([article], self.out, download=download)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(download.call_count, 1)
        images = result['articles'][0]['images']
        self.assertEqual((images['references'], images['unique'], images['downloaded']), (2, 1, 1))
        image = images['items'][0]
        self.assertTrue(image['path'].endswith('.png'))
        self.assertEqual(image['detected_type'], 'image/png')
        markdown = self.markdown(result)
        self.assertEqual(markdown.count('](' + image['path'] + ')'), 2)
        self.assertLess(markdown.index(self.first), markdown.index('![one]'))
        self.assertLess(markdown.index('![one]'), markdown.index('![two]'))
        self.assertLess(markdown.index('![two]'), markdown.index(self.second))
        self.assertEqual((self.out / 'originals/synthetic-1' / image['path']).read_bytes(), PNG)

    def test_failed_image_keeps_source_position_and_explicit_partial_status(self):
        raw = self.body_html('<img src="' + IMAGE_URL + '" alt="missing">')
        result = exporter.export_originals([self.article(content_html=raw)], self.out,
                                           download=Mock(side_effect=TimeoutError('PRIVATE_RESPONSE')))
        self.assertEqual(result['status'], 'partial')
        markdown = self.markdown(result)
        self.assertIn(IMAGE_URL, markdown)
        self.assertLess(markdown.index(self.first), markdown.index(IMAGE_URL))
        self.assertLess(markdown.index(IMAGE_URL), markdown.index(self.second))
        self.assertEqual(result['articles'][0]['images']['failed'], 1)
        self.assertNotIn('PRIVATE_RESPONSE', json.dumps(result))

    def test_offline_mode_keeps_links_without_any_download(self):
        download = Mock()
        raw = self.body_html('<img src="' + IMAGE_URL + '">')
        result = exporter.export_originals([self.article(content_html=raw)], self.out,
                                           download=download, offline=True)
        download.assert_not_called()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['articles'][0]['images']['items'][0]['error_code'], 'offline_image_unavailable')

    def test_scripts_iframes_and_executable_image_sources_are_not_emitted(self):
        raw = self.body_html('<script>DO_NOT_EXECUTE()</script><iframe src="https://example.test"></iframe>'
                             '<img src="javascript:DO_NOT_EXECUTE()">')
        download = Mock()
        result = exporter.export_originals([self.article(content_html=raw)], self.out, download=download)
        download.assert_not_called()
        markdown = self.markdown(result)
        self.assertNotIn('DO_NOT_EXECUTE', markdown)
        self.assertNotIn('<iframe', markdown)
        self.assertEqual(result['status'], 'partial')

    def test_missing_or_extra_html_text_is_partial(self):
        for index, raw in enumerate(('<p>' + self.first + '</p>', self.body_html('<p>' + chr(67) * 20 + '</p>'))):
            result = exporter.export_originals([self.article(content_html=raw)], self.out / str(index))
            self.assertEqual(result['status'], 'partial')
            self.assertIn('html_text_mismatch', [issue['code'] for issue in result['articles'][0]['issues']])

    def test_background_and_srcset_images_are_explicitly_incomplete(self):
        for index, extra in enumerate(('<span style="background-image:url(' + IMAGE_URL + ')"></span>',
                                      '<picture><source srcset="' + IMAGE_URL + '"></picture>')):
            result = exporter.export_originals([self.article(content_html=self.body_html(extra))], self.out / str(index))
            self.assertEqual(result['status'], 'partial')
            self.assertIn('additional_image_sources_unavailable', [issue['code'] for issue in result['articles'][0]['issues']])

    def test_body_container_background_and_wechat_media_are_reported(self):
        raw = self.body_html('<mpvoice></mpvoice>').replace('id="js_content"', 'id="js_content" style="background:url(' + IMAGE_URL + ')"')
        result = exporter.export_originals([self.article(content_html=raw)], self.out)
        codes = [issue['code'] for issue in result['articles'][0]['issues']]
        self.assertEqual(result['status'], 'partial')
        self.assertIn('additional_image_sources_unavailable', codes)
        self.assertIn('embedded_media_omitted', codes)

    def test_captcha_page_empty_body_and_wrong_full_page_are_rejected(self):
        bodies = ['<html><body>环境异常，请完成安全验证</body></html>',
                  '<div id="js_content"><script>x()</script></div>',
                  '<html><body><p>' + self.first + '</p></body></html>']
        for raw in bodies:
            result = exporter.export_originals([self.article(content_html=raw)], self.out)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['articles_exported'], 0)
            self.assertFalse((self.out / 'originals/synthetic-1/article.md').exists())

    def test_rerun_requires_new_directory_and_preserves_existing_images(self):
        raw = self.body_html('<img src="' + IMAGE_URL + '">')
        first = exporter.export_originals([self.article(content_html=raw)], self.out,
                                          download=Mock(return_value=(PNG, 'image/png')))
        previous_image = self.out / 'originals/synthetic-1' / first['articles'][0]['images']['items'][0]['path']
        previous_markdown = (self.out / 'originals/synthetic-1/article.md').read_bytes()
        with self.assertRaises(exporter.ExportError) as caught:
            exporter.export_originals([self.article(content_html=self.body_html())], self.out)
        self.assertEqual(caught.exception.code, 'output_exists')
        self.assertEqual(previous_image.read_bytes(), PNG)
        self.assertEqual((self.out / 'originals/synthetic-1/article.md').read_bytes(), previous_markdown)


if __name__ == '__main__':
    unittest.main()
