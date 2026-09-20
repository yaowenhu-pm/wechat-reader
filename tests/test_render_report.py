import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'render_report.py'
spec = importlib.util.spec_from_file_location('render_report', SCRIPT)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def sample():
    content = '这是一篇用于测试的虚构公众号原文。\n团队发布了新的阅读工具，允许读者自行配置栏目。\n仍需核对原始材料。'
    article = {'id': 'sample-1', 'title': '阅读工具介绍', 'account': '虚构测试号', 'url': 'https://example.com/article?x=1&y=2', 'content_text': content, 'content_kind': 'fulltext', 'content_hash': renderer.digest(content)}
    config = {'output': {'title': '我的专题报告', 'sections': [{'id': 'reading', 'title': '值得阅读'}, {'id': 'followup', 'title': '继续关注'}], 'formats': ['html', 'markdown']}}
    report = {'title': '自定义阅读报告', 'summary': '只整理需要阅读的材料。', 'sections': [{'id': 'reading', 'items': [{'title': '支持自定义栏目', 'body': '作者介绍了可配置的阅读工具。\n\n是否适用还需要实际试用。', 'sources': [{'article_id': article['id'], 'content_hash': article['content_hash'], 'quote': '允许读者自行配置栏目'}]}]}]}
    return config, [article], report


class RendererTests(unittest.TestCase):
    def test_config_order_and_missing_sections(self):
        config, articles, report = sample()
        report['sections'].insert(0, {'id': 'followup', 'items': []})
        result = renderer.validate_report(config, {'articles': articles}, report)
        self.assertEqual([s['id'] for s in result['sections']], ['reading', 'followup'])
        page = renderer.render_html(result)
        self.assertIn('暂无内容', page)
        self.assertIn('https://example.com/article?x=1&amp;y=2', page)
        self.assertLess(page.index('值得阅读'), page.index('继续关注'))
        for output in (page, renderer.render_markdown(result)):
            self.assertIn('阅读工具介绍', output)
            self.assertIn('允许读者自行配置栏目', output)
            self.assertNotIn(articles[0]['content_hash'], output)
            self.assertNotIn('原文 ID:', output)
            self.assertNotIn('引文校验仅确认出处', output)

    def test_missing_or_wrong_source_rejected(self):
        for mutation in ('unknown', 'hash', 'quote', 'empty_quote', 'no_source', 'summary', 'tampered'):
            with self.subTest(mutation=mutation):
                config, articles, report = sample()
                source = report['sections'][0]['items'][0]['sources'][0]
                if mutation == 'unknown': source['article_id'] = 'missing'
                elif mutation == 'hash': source['content_hash'] = '0' * 64
                elif mutation == 'quote': source['quote'] = '允许读者……配置栏目'
                elif mutation == 'empty_quote': source['quote'] = ' '
                elif mutation == 'no_source': report['sections'][0]['items'][0]['sources'] = []
                elif mutation == 'summary': articles[0]['content_kind'] = 'summary'
                elif mutation == 'tampered': articles[0]['content_text'] += '额外内容'
                with self.assertRaises(renderer.ReportError):
                    renderer.validate_report(config, articles, report)

    def test_duplicate_and_unknown_sections_rejected(self):
        for where in ('config', 'report', 'unknown'):
            with self.subTest(where=where):
                config, articles, report = sample()
                if where == 'config': config['output']['sections'].append(copy.deepcopy(config['output']['sections'][0]))
                elif where == 'report': report['sections'].append(copy.deepcopy(report['sections'][0]))
                else: report['sections'].append({'id': 'unconfigured', 'items': []})
                with self.assertRaises(renderer.ReportError):
                    renderer.validate_report(config, articles, report)

    def test_malicious_text_escaped_and_url_rejected(self):
        config, articles, report = sample()
        report['title'] = '<script>alert("x")</script>'
        report['sections'][0]['items'][0]['body'] = '<img src=x onerror=alert(1)> [click](javascript:evil)'
        normalized = renderer.validate_report(config, articles, report)
        page = renderer.render_html(normalized)
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img', page)
        self.assertIn('&lt;script&gt;', page)
        markdown = renderer.render_markdown(normalized)
        self.assertNotIn('<script>', markdown)
        self.assertIn('\\[click\\]\\(javascript:evil\\)', markdown)
        for url in ('javascript:alert(1)', 'file:///etc/passwd', 'https://example.com/\nattack', 'https://example.com:bad'):
            articles[0]['url'] = url
            with self.assertRaises(renderer.ReportError):
                renderer.validate_report(config, articles, report)

    def test_output_files_and_json_bom_cli(self):
        config, articles, report = sample()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, value in [('config', config), ('articles', articles), ('report', report)]:
                (root / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8-sig')
            result = renderer.main(['--config', str(root / 'config.json'), '--articles', str(root / 'articles.json'), '--report', str(root / 'report.json'), '--out', str(root / 'out')])
            self.assertEqual(result, 0)
            self.assertTrue((root / 'out/report.html').is_file())
            self.assertTrue((root / 'out/report.md').is_file())
            self.assertFalse((root / 'out/report.pdf').exists())

    def test_missing_pdf_dependency_has_clear_error(self):
        real_import = __import__
        def no_reportlab(name, *args, **kwargs):
            if name.startswith('reportlab'):
                raise ImportError('test dependency absent')
            return real_import(name, *args, **kwargs)
        with patch('builtins.__import__', no_reportlab):
            with self.assertRaisesRegex(renderer.ReportError, 'PDF 未生成.*reportlab'):
                renderer._pdf_dependencies()

    def test_chinese_pdf_pagination_and_links(self):
        try:
            import reportlab
            from pypdf import PdfReader
        except ImportError:
            self.skipTest('optional PDF integration test needs reportlab and pypdf')
        config, articles, report = sample()
        report['sections'][0]['items'][0]['body'] = ('中文阅读工具支持自由配置栏目。读者可以根据自己的需要整理材料，并通过原文链接核对依据。\n\n' * 70).strip()
        with tempfile.TemporaryDirectory() as temp:
            result = renderer.write_report(config, articles, report, temp, force_pdf=True)
            self.assertEqual(len(result['files']), 3)
            pdf = PdfReader(Path(temp) / 'report.pdf')
            self.assertGreater(len(pdf.pages), 1)
            extracted = '\n'.join(page.extract_text() for page in pdf.pages)
            self.assertIn('自定义阅读报告', extracted)
            self.assertIn('继续关注', extracted)
            self.assertIn('暂无内容', extracted)
            self.assertNotIn(articles[0]['content_hash'], extracted)
            self.assertNotIn('原文 ID:', extracted)
            self.assertNotIn('引文校验仅确认出处', extracted)
            links = [annotation.get_object().get('/A', {}).get('/URI') for page in pdf.pages for annotation in page.get('/Annots', [])]
            self.assertIn(articles[0]['url'], links)


if __name__ == '__main__':
    unittest.main()
