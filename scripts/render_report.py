#!/usr/bin/env python3
"""Validate AI-authored, source-backed report JSON and render shareable artifacts.

No model calls or network requests are made. --out is an output directory.
PDF support is optional: python -m pip install reportlab
Set WECHAT_REPORT_FONT to a Chinese TrueType font path when needed.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit


class ReportError(ValueError):
    """Invalid report input or missing optional output dependency."""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _text(value, where, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ReportError(f"{where} 必须是{'可为空的' if allow_empty else '非空'}字符串")
    # XML-based PDF paragraphs cannot safely represent these control characters.
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ReportError(f"{where} 含不支持的控制字符")
    return value


def _list(value, where):
    if not isinstance(value, list):
        raise ReportError(f"{where} 必须是数组")
    return value


def _object(value, where):
    if not isinstance(value, dict):
        raise ReportError(f"{where} 必须是对象")
    return value


def safe_url(value):
    value = _text(value, '原文 URL')
    try:
        parts = urlsplit(value)
        valid = parts.scheme.lower() in ('http', 'https') and bool(parts.netloc) and bool(parts.hostname)
        parts.port  # Reject malformed ports before rendering links.
    except ValueError:
        valid = False
    if not valid or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ReportError('原文 URL 必须是有效的 http(s) 地址，且不能包含空白或控制字符')
    return value


def validate_report(config, articles, report):
    """Return normalized report. Evidence verification is exact, not semantic."""
    output = _object(_object(config, 'config').get('output'), 'config.output')
    configured = _list(output.get('sections'), 'config.output.sections')
    if not configured:
        raise ReportError('config.output.sections 至少需要一个栏目')
    configured_ids = set()
    for n, section in enumerate(configured):
        section = _object(section, f'config.output.sections[{n}]')
        section_id = _text(section.get('id'), '栏目 id')
        _text(section.get('title'), f'栏目 {section_id} title')
        if section_id in configured_ids:
            raise ReportError(f'配置包含重复栏目 id: {section_id}')
        configured_ids.add(section_id)

    if isinstance(articles, dict):
        articles = articles.get('articles')
    article_map = {}
    for n, article in enumerate(_list(articles, 'articles')):
        article = _object(article, f'articles[{n}]')
        article_id = _text(article.get('id'), 'article.id')
        if article_id in article_map:
            raise ReportError(f'重复 article id: {article_id}')
        article_map[article_id] = article

    report = _object(report, 'report')
    title = report.get('title') or output.get('title') or '公众号内容整理'
    title = _text(title, 'report.title')
    summary = _text(report.get('summary', ''), 'report.summary', allow_empty=True)
    report_sections = {}
    verified_articles = {}
    for n, section in enumerate(_list(report.get('sections'), 'report.sections')):
        section = _object(section, f'report.sections[{n}]')
        section_id = _text(section.get('id'), 'report section.id')
        if section_id in report_sections:
            raise ReportError(f'报告包含重复栏目 id: {section_id}')
        if section_id not in configured_ids:
            raise ReportError(f'报告包含未知栏目 id: {section_id}')
        items = []
        for i, item in enumerate(_list(section.get('items'), f'栏目 {section_id} items')):
            item = _object(item, f'栏目 {section_id} items[{i}]')
            item_title = _text(item.get('title'), 'item.title')
            body = _text(item.get('body'), 'item.body')
            sources = _list(item.get('sources'), 'item.sources')
            if not sources:
                raise ReportError(f'条目「{item_title}」至少需要一个原文 source')
            normalized_sources = []
            for source in sources:
                source = _object(source, 'source')
                article_id = _text(source.get('article_id'), 'source.article_id')
                if article_id not in article_map:
                    raise ReportError(f'未知 article_id: {article_id}')
                article = article_map[article_id]
                if article_id not in verified_articles:
                    if article.get('content_kind') != 'fulltext':
                        raise ReportError(f'文章 {article_id} 不是 fulltext，摘要不能作为原文交付')
                    content = _text(article.get('content_text'), f'文章 {article_id} content_text')
                    content_hash = digest(content)
                    if article.get('content_hash') != content_hash:
                        raise ReportError(f'文章 {article_id} content_hash 与正文不一致')
                    verified_articles[article_id] = {
                        'article_id': article_id,
                        'title': _text(article.get('title'), f'文章 {article_id} title'),
                        'account': _text(article.get('account', ''), f'文章 {article_id} account', allow_empty=True),
                        'url': safe_url(article.get('url')),
                        'content_hash': content_hash,
                        'content_text': content,
                    }
                verified = verified_articles[article_id]
                if source.get('content_hash') != verified['content_hash']:
                    raise ReportError(f'引用 {article_id} content_hash 不匹配，需重新核对该版本原文')
                quote = _text(source.get('quote'), f'引用 {article_id} quote')
                if quote not in verified['content_text']:
                    raise ReportError(f'引用 {article_id} quote 不是原文中连续出现的文字')
                normalized_source = {k: v for k, v in verified.items() if k != 'content_text'}
                normalized_source['quote'] = quote
                normalized_sources.append(normalized_source)
            items.append({'title': item_title, 'body': body, 'sources': normalized_sources})
        report_sections[section_id] = items
    return {
        'title': title,
        'summary': summary,
        'sections': [{'id': s['id'], 'title': s['title'], 'items': report_sections.get(s['id'], [])} for s in configured],
    }


def _paragraphs(value):
    return [p for p in re.split(r'\n\s*\n', value.strip()) if p]


def render_html(report):
    e = html.escape
    def paragraphs(value):
        return '\n'.join('<p>' + e(p).replace('\n', '<br>') + '</p>' for p in _paragraphs(value))
    parts = [f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(report['title'])}</title>
<style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#17232d;font-family:"Microsoft YaHei","PingFang SC",sans-serif;line-height:1.85}}
main{{max-width:900px;margin:40px auto;padding:44px 52px;background:white;border-top:6px solid #176463}}h1{{font-size:30px;line-height:1.45;margin:0 0 22px}}h2{{font-size:22px;border-bottom:1px solid #dbe5e8;padding-bottom:10px;margin-top:36px}}h3{{font-size:18px;margin:26px 0 8px}}p{{margin:9px 0}}a{{color:#176463;text-decoration:underline;overflow-wrap:anywhere}}.summary{{background:#edf5f4;padding:14px 20px;border-left:3px solid #176463}}.sources{{margin:18px 0 28px;padding:12px 18px;background:#f7f8fa;font-size:13px}}blockquote{{margin:8px 0;border-left:2px solid #b6c6cb;padding:0 12px;color:#485762;white-space:pre-wrap;overflow-wrap:anywhere}}.empty{{color:#66757d}}@media(max-width:650px){{main{{margin:0;padding:28px 20px}}}}@media print{{body{{background:white}}main{{margin:0;padding:0;border:0;max-width:none}}h2,h3{{break-after:avoid}}a{{color:inherit}}}}
</style></head><body><main><h1>{e(report['title'])}</h1>''']
    if report['summary']:
        parts.append('<div class="summary">' + paragraphs(report['summary']) + '</div>')
    for section in report['sections']:
        parts.append(f'<section><h2>{e(section["title"])}</h2>')
        if not section['items']:
            parts.append('<p class="empty">暂无内容</p>')
        for item in section['items']:
            parts.append('<article><h3>' + e(item['title']) + '</h3>' + paragraphs(item['body']))
            parts.append('<div class="sources"><strong>原文依据</strong>')
            for source in item['sources']:
                label = source['title'] + (f' · {source["account"]}' if source['account'] else '')
                parts.append(f'<p><a href="{e(source["url"], quote=True)}" rel="noopener noreferrer">{e(label)}</a></p><blockquote>{e(source["quote"])}</blockquote>')
            parts.append('</div></article>')
        parts.append('</section>')
    parts.append('</main></body></html>')
    return '\n'.join(parts)


def _md(value):
    # Treat every supplied field as plain text, including AI-authored body text.
    value = html.escape(value, quote=False)
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', value)


def render_markdown(report):
    parts = ['# ' + _md(' '.join(report['title'].split())), '']
    if report['summary']:
        parts.extend([_md(report['summary']), ''])
    for section in report['sections']:
        parts.extend(['## ' + _md(' '.join(section['title'].split())), ''])
        if not section['items']:
            parts.extend(['暂无内容', ''])
        for item in section['items']:
            parts.extend(['### ' + _md(' '.join(item['title'].split())), '', _md(item['body']), '', '**原文依据**', ''])
            for source in item['sources']:
                label = source['title'] + (f' · {source["account"]}' if source['account'] else '')
                url = source['url'].replace('<', '%3C').replace('>', '%3E')
                parts.extend([f'[{_md(label)}](<{url}>)', '', '\n'.join('> ' + _md(line) for line in source['quote'].splitlines()), ''])
    return '\n'.join(parts) + '\n'


def _pdf_dependencies():
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph
    except ImportError as exc:
        raise ReportError('PDF 未生成：缺少可选依赖 reportlab。请使用当前 Python 执行 python -m pip install reportlab，或移除 pdf 格式。') from exc
    return colors, TA_LEFT, A4, ParagraphStyle, pdfmetrics, UnicodeCIDFont, TTFont, SimpleDocTemplate, Paragraph


def render_pdf(report, path):
    colors, TA_LEFT, A4, ParagraphStyle, pdfmetrics, UnicodeCIDFont, TTFont, SimpleDocTemplate, Paragraph = _pdf_dependencies()
    configured_font = os.environ.get('WECHAT_REPORT_FONT')
    candidates = [configured_font] if configured_font else [
        'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simsun.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/System/Library/Fonts/PingFang.ttc',
    ]
    font_name = None
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                font_name = 'WeChatReportCJK'
                pdfmetrics.registerFont(TTFont(font_name, candidate))
                break
            except Exception as exc:
                font_name = None
                if configured_font:
                    raise ReportError(f'PDF 字体无法加载: {candidate}: {exc}') from exc
    if configured_font and font_name is None:
        raise ReportError(f'PDF 字体文件不存在: {configured_font}')
    if font_name is None:
        font_name = 'STSong-Light'
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    def style(name, **kwargs):
        defaults = dict(fontName=font_name, fontSize=10, leading=17, wordWrap='CJK', alignment=TA_LEFT, textColor=colors.HexColor('#17232d'), spaceAfter=9)
        defaults.update(kwargs)
        return ParagraphStyle(name, **defaults)
    styles = {
        'title': style('title', fontSize=23, leading=33, spaceAfter=20, keepWithNext=True),
        'section': style('section', fontSize=16, leading=24, spaceBefore=21, spaceAfter=12, keepWithNext=True, textColor=colors.HexColor('#176463')),
        'item': style('item', fontSize=12, leading=20, spaceBefore=10, keepWithNext=True),
        'body': style('body'),
        'summary': style('summary', backColor=colors.HexColor('#edf5f4'), borderPadding=10, spaceAfter=16),
        'source': style('source', fontSize=8, leading=13, textColor=colors.HexColor('#4b626b'), spaceAfter=5),
        'quote': style('quote', fontSize=9, leading=15, leftIndent=12, borderPadding=5, backColor=colors.HexColor('#f4f6f8')),
    }
    def markup(value):
        return html.escape(value).replace('\n', '<br/>')
    story = [Paragraph(markup(report['title']), styles['title'])]
    if report['summary']:
        story.append(Paragraph(markup(report['summary']), styles['summary']))
    for section in report['sections']:
        story.append(Paragraph(markup(section['title']), styles['section']))
        if not section['items']:
            story.append(Paragraph('暂无内容', styles['body']))
        for item in section['items']:
            story.append(Paragraph(markup(item['title']), styles['item']))
            for paragraph in _paragraphs(item['body']):
                story.append(Paragraph(markup(paragraph), styles['body']))
            for source in item['sources']:
                label = source['title'] + (f' · {source["account"]}' if source['account'] else '')
                story.append(Paragraph(f'原文依据：<link href="{html.escape(source["url"], quote=True)}" color="#176463">{markup(label)}</link>', styles['source']))
                story.append(Paragraph(markup(source['quote']), styles['quote']))
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor('#66757d'))
        canvas.drawRightString(A4[0] - 48, 28, f'第 {doc.page} 页')
        canvas.restoreState()
    document = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=48, rightMargin=48, topMargin=42, bottomMargin=48, title=report['title'], author='')
    document.build(story, onFirstPage=footer, onLaterPages=footer)


def write_report(config, articles, report, out, force_pdf=False):
    normalized = validate_report(config, articles, report)
    formats = config['output'].get('formats', ['html', 'markdown'])
    if not isinstance(formats, list) or not formats or any(f not in ('html', 'markdown', 'pdf') for f in formats):
        raise ReportError('output.formats 必须是 html、markdown、pdf 组成的非空数组')
    formats = list(dict.fromkeys(formats + (['pdf'] if force_pdf else [])))
    if 'pdf' in formats:
        _pdf_dependencies()
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    files = []
    # Stage all requested outputs first; dependency/layout failures produce no new final report.
    with tempfile.TemporaryDirectory(prefix='.report-', dir=str(out)) as staging:
        staging = Path(staging)
        for fmt in formats:
            suffix = 'md' if fmt == 'markdown' else fmt
            path = staging / ('report.' + suffix)
            if fmt == 'pdf':
                try:
                    render_pdf(normalized, path)
                except ReportError:
                    raise
                except Exception as exc:
                    raise ReportError(f'PDF 未生成：排版失败：{exc}') from exc
            else:
                path.write_text(render_html(normalized) if fmt == 'html' else render_markdown(normalized), encoding='utf-8')
            files.append((path, out / path.name))
        for source, target in files:
            source.replace(target)
    return {'status': 'ok', 'files': [str(target) for _, target in files], 'evidence_check': 'article_id + fulltext + sha256 + exact contiguous quote; no semantic entailment check'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--articles', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path, help='交付文件输出目录')
    parser.add_argument('--pdf', action='store_true', help='除配置的格式外另生成 PDF')
    args = parser.parse_args(argv)
    try:
        inputs = [json.loads(path.read_text(encoding='utf-8-sig')) for path in (args.config, args.articles, args.report)]
        result = write_report(*inputs, out=args.out, force_pdf=args.pdf)
    except (ReportError, OSError, json.JSONDecodeError) as exc:
        print(f'报告生成失败：{exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
