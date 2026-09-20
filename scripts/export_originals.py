#!/usr/bin/env python3
"""Export supplied article bodies to Markdown and local images, without a browser.

Plain-text export uses the standard library. HTML conversion additionally needs
beautifulsoup4 and markdownify. No article page or model API is requested here.
"""
import argparse
import hashlib
import html
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import sys
import time
import urllib.parse

MAX_HTML_CHARACTERS = 5_000_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT = 15
MAX_REDIRECTS = 3
WECHAT_FAKE_IP_HOSTS = frozenset({'mmbiz.qpic.cn'})
FAKE_IP_NETWORK = ipaddress.ip_network('198.18.0.0/15')
IMAGE_MIMES = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp',
    'bmp': 'image/bmp', 'tif': 'image/tiff', 'ico': 'image/x-icon', 'avif': 'image/avif',
}


class ExportError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _issue(code, message):
    return {'code': code, 'message': message}


def _digest_text(value):
    # Same content hash convention as watch.digest and the collection adapter.
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _text(value):
    return value if isinstance(value, str) else ''


def _public_ip(value):
    address = ipaddress.ip_address(value.split('%', 1)[0])
    return address.is_global and not address.is_multicast


def normalize_image_url(value, base_url=''):
    """Validate URL syntax and literal IPs without doing any network access."""
    if not isinstance(value, str) or not value.strip():
        raise ExportError('image_source_missing', '图片缺少可读取的源地址')
    value = html.unescape(value.strip())
    if re.search(r'[\s\x00-\x1f\x7f]', value):
        raise ExportError('unsafe_image_url', '图片地址包含空白或控制字符')
    if value.startswith('//'):
        value = 'https:' + value
    elif not urllib.parse.urlsplit(value).scheme:
        value = urllib.parse.urljoin(base_url, value)
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname or ''
        port = parts.port
        if (parts.scheme not in ('http', 'https') or not host or parts.username is not None
                or parts.password is not None or port not in (None, 80 if parts.scheme == 'http' else 443)):
            raise ValueError('invalid URL')
        host = host.rstrip('.').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError('local hostname')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode('idna').decode('ascii')
        else:
            if not _public_ip(str(address)):
                raise ValueError('private IP')
        netloc = '[' + host + ']' if ':' in host else host
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path or '/', parts.query, ''))
    except (ValueError, UnicodeError):
        raise ExportError('unsafe_image_url', '仅允许公共 HTTP(S) 图片地址，不接受本地、私网或带凭据的地址') from None


def _resolve_public_addresses(host, port, https=False):
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ExportError('image_dns_failed', '图片域名无法解析') from None
    addresses = []
    for record in records:
        address = record[4][0]
        try:
            public = _public_ip(address)
            # Some user-configured TUN resolvers map this verified public WeChat
            # CDN to RFC 2544 Fake-IP space. Keep the exception host-specific and
            # HTTPS-only; _open_pinned still verifies its real TLS certificate.
            fake_wechat_cdn = (https and port == 443 and host in WECHAT_FAKE_IP_HOSTS
                               and ipaddress.ip_address(address) in FAKE_IP_NETWORK)
        except ValueError:
            public, fake_wechat_cdn = False, False
        if not public and not fake_wechat_cdn:
            raise ExportError('unsafe_image_host', '图片域名解析到了本地、私网或保留地址，已拒绝下载')
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ExportError('image_dns_failed', '图片域名没有可用的公共地址')
    return addresses


def _open_pinned(url, timeout):
    """Connect to an already checked IP, keeping the original HTTP host and TLS SNI.

    This deliberately avoids proxy inheritance and a second hostname resolution.
    Redirects are handled by download_image and checked again for every hop.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = 443 if parts.scheme == 'https' else 80
    addresses = _resolve_public_addresses(host, port, https=parts.scheme == 'https')
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    raw_socket = None
    try:
        raw_socket = socket.create_connection((addresses[0], port), timeout=timeout)
        connection.sock = (ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
                           if parts.scheme == 'https' else raw_socket)
        target = urllib.parse.urlunsplit(('', '', parts.path or '/', parts.query, ''))
        connection.request('GET', target, headers={
            'Host': parts.netloc, 'User-Agent': 'WechatReaderOriginalExport/1.0',
            'Accept': 'image/*', 'Accept-Encoding': 'identity', 'Connection': 'close',
        })
        return connection, connection.getresponse()
    except Exception:
        connection.close()
        if raw_socket is not None:
            raw_socket.close()
        raise


def download_image(url, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_IMAGE_BYTES):
    """Return (bytes, response_content_type); no cookies, redirects to private IPs or proxies."""
    if timeout <= 0 or max_bytes <= 0:
        raise ExportError('invalid_download_limit', '图片大小和超时限制必须大于零')
    current = normalize_image_url(url)
    deadline = time.monotonic() + timeout
    for hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ExportError('image_timeout', '图片下载超时')
        connection = None
        try:
            connection, response = _open_pinned(current, remaining)
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or hop == MAX_REDIRECTS:
                    raise ExportError('image_redirect_failed', '图片重定向缺少地址或次数超限')
                target = normalize_image_url(urllib.parse.urljoin(current, location))
                if current.startswith('https://') and not target.startswith('https://'):
                    raise ExportError('image_redirect_failed', '拒绝将 HTTPS 图片请求降级到 HTTP')
                current = target
                continue
            if response.status != 200:
                raise ExportError('image_http_error', '图片服务器返回 HTTP ' + str(response.status))
            length = response.getheader('Content-Length')
            if length and length.isdigit() and int(length) > max_bytes:
                raise ExportError('image_too_large', '图片超过下载大小限制')
            if (response.getheader('Content-Encoding') or 'identity').lower() != 'identity':
                raise ExportError('image_encoding_unsupported', '图片响应采用了不支持的压缩传输格式')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExportError('image_timeout', '图片下载超时')
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ExportError('image_too_large', '图片超过下载大小限制')
                chunks.append(chunk)
            body = b''.join(chunks)
            if not body:
                raise ExportError('empty_image', '图片服务器返回了空内容')
            return body, response.getheader('Content-Type') or ''
        except ExportError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException):
            raise ExportError('image_download_failed', '图片下载失败或超时') from None
        finally:
            if connection is not None:
                connection.close()
    raise ExportError('image_redirect_failed', '图片重定向次数超限')


def image_type(data, content_type=''):
    """Use file signatures, never a URL suffix or an unverified image MIME alone."""
    if not isinstance(data, bytes) or not data:
        raise ExportError('invalid_image', '下载内容不是可识别的图片')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        extension = 'png'
    elif data.startswith(b'\xff\xd8\xff'):
        extension = 'jpg'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        extension = 'gif'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        extension = 'webp'
    elif data.startswith(b'BM'):
        extension = 'bmp'
    elif data.startswith((b'II*\x00', b'MM\x00*')):
        extension = 'tif'
    elif data.startswith(b'\x00\x00\x01\x00'):
        extension = 'ico'
    elif data[4:8] == b'ftyp' and (data[8:12] in (b'avif', b'avis') or b'avif' in data[16:40]):
        extension = 'avif'
    else:
        raise ExportError('invalid_image', '下载内容不是支持的图片；未将错误页或 SVG 保存成图片')
    return extension, IMAGE_MIMES[extension]


def _safe_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}', value)
            or value.endswith('.') or value.split('.')[0].upper() in
            {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(n) for n in range(1, 10)),
             *('LPT' + str(n) for n in range(1, 10))}):
        raise ExportError('invalid_article_id', '文章 id 不能作为安全的本地目录名')
    return value


def _inside(root, relative):
    candidate = root.resolve() / relative
    target = candidate.resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise ExportError('unsafe_output_path', '导出路径指向了输出目录之外') from None
    if target != candidate:
        raise ExportError('unsafe_output_path', '导出路径包含符号链接或不安全的路径组件')
    return target


def _check_output_directory(out):
    """Use a new delivery directory; never remove or overwrite an older export."""
    originals = _inside(out, 'originals')
    if not originals.exists():
        return
    if any(path.is_file() or path.is_symlink() for path in originals.rglob('*')):
        raise ExportError('output_exists', '输出目录中已有原文文件，未覆盖或删除；请选择新的 --out 目录')


def _write_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as handle:
        handle.write(value)
    temporary.replace(path)


def _label(value):
    value = re.sub(r'\s+', ' ', _text(value)).strip()
    return re.sub(r'([\\`*_{}\[\]()#+!|<>])', r'\\\1', value)


def _plain_markdown(value):
    """Render supplied text literally, retaining paragraph and line boundaries."""
    value = html.escape(value.strip().replace('\r\n', '\n').replace('\r', '\n'), quote=False)
    value = re.sub(r'([\\`*_{}\[\]()#+.!|~\-])', r'\\\1', value)
    return re.sub(r'(?<!\n)\n(?!\n)', '  \n', value)


def _markdown_link(url, label):
    # URLs in angle brackets need their own escaping; never emit executable schemes.
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None
                or parts.password is not None or re.search(r'[\s\x00-\x1f\x7f]', url)):
            return _label(label)
    except ValueError:
        return _label(label)
    escaped = url.replace('<', '%3C').replace('>', '%3E').replace('(', '%28').replace(')', '%29')
    return '[' + _label(label) + '](<' + escaped + '>)'


def _captcha_text(text):
    compact = re.sub(r'\s+', '', text).casefold()
    if len(compact) > 1000:
        return False
    return (('环境异常' in compact and '验证' in compact)
            or any(marker in compact for marker in ('请完成安全验证', '完成验证后继续访问',
                                                    '访问过于频繁，请稍后', 'verifyyouarehuman',
                                                    'checkingyourbrowser')))


def _metadata(article):
    title = _text(article.get('title')).strip() or '未提供标题'
    account = _text(article.get('account')).strip() or '未知'
    author = _text(article.get('author')).strip() or '未提供'
    published = _text(article.get('published_at')).strip() or '未知（未提供或未核实）'
    url = _text(article.get('original_wechat_url') or article.get('url')).strip()
    source = _markdown_link(url, '原文链接') if url else '未提供'
    return ('# ' + _label(title) + '\n\n公众号：' + _label(account) + '\n\n作者：' + _label(author)
            + '\n\n发布时间：' + _label(published) + '\n\n来源：' + source + '\n\n---\n\n')


def _convert_html(article, article_dir, download, offline, timeout, max_image_bytes):
    try:
        from bs4 import BeautifulSoup, Comment
        from markdownify import MarkdownConverter
    except ImportError:
        raise ExportError('html_dependencies_missing', 'HTML 导出需要安装 beautifulsoup4 和 markdownify') from None
    raw = article['content_html']
    if len(raw) > MAX_HTML_CHARACTERS:
        raise ExportError('html_too_large', 'HTML 超过字符数量限制，未截断导出')
    soup = BeautifulSoup(raw, 'html.parser')
    content = soup.select_one('#js_content')
    visible_page = soup.get_text(' ', strip=True)
    if _captcha_text(visible_page) or (content is None and 'wappoc_appmsgcaptcha' in raw.casefold()):
        raise ExportError('captcha_page', '输入 HTML 是验证码或访问验证页面，未导出为文章')
    if content is None:
        if soup.html is not None or soup.body is not None:
            raise ExportError('article_body_missing', '完整页面没有 #js_content 正文，不能把整个网页当作文章')
        content = soup
    issues = []
    elements = [content] + list(content.find_all())
    if (content.find('source') or any(element.get('srcset') for element in elements)
            or any(re.search(r'background(?:-image)?\s*:[^;]*url\s*\(', _text(element.get('style')), re.I)
                   for element in elements)):
        issues.append(_issue('additional_image_sources_unavailable', '存在 background-image、srcset 或 source 图片资源，未完整转换这些呈现方式'))
    unsupported_media = ['iframe', 'object', 'embed', 'video', 'audio', 'svg', 'canvas',
                         'mpvoice', 'mpvideo', 'mp-common-videosnap', 'mp-common-videoplayer']
    if content.find(unsupported_media):
        issues.append(_issue('embedded_media_omitted', '存在未导出的嵌入媒体；正文 Markdown 未执行或加载这些内容'))
    for element in list(content.find_all(['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta',
                                          'base', 'noscript', 'template'] + unsupported_media)):
        element.decompose()
    for comment in content.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    visible_text = content.get_text(' ', strip=True)
    if not visible_text.strip():
        raise ExportError('empty_body', 'HTML 没有可核对的正文文本，未作为成功文章导出')
    if _captcha_text(visible_text):
        raise ExportError('captcha_page', '正文是验证码或访问验证提示，未导出为文章')
    supplied_text = _text(article.get('content_text')).strip()
    if not supplied_text:
        issues.append(_issue('content_text_missing', '缺少 content_text，已转换 HTML 但无法与采集正文文本交叉检查'))
    elif re.sub(r'\s+', '', supplied_text) != re.sub(r'\s+', '', visible_text):
        issues.append(_issue('html_text_mismatch', 'HTML 与 content_text 的正文不同，请检查是否截断、额外混入内容或取错范围'))
    base_url = _text(article.get('original_wechat_url') or article.get('url'))
    for anchor in content.find_all('a'):
        href = _text(anchor.get('href'))
        candidate = urllib.parse.urljoin(base_url, href)
        try:
            parts = urllib.parse.urlsplit(candidate)
            good = (parts.scheme in ('http', 'https') and parts.hostname and not parts.username
                    and not parts.password and not re.search(r'[\s\x00-\x1f\x7f]', candidate))
        except ValueError:
            good = False
        if good:
            anchor['href'] = candidate.replace('(', '%28').replace(')', '%29').replace('<', '%3C').replace('>', '%3E')
        else:
            anchor.attrs.pop('href', None)
    image_rows, cached = [], {}
    references = 0
    for element in content.find_all('img'):
        references += 1
        source = next((_text(element.get(key)).strip() for key in ('data-src', 'data-original', 'data-actualsrc', 'src')
                       if _text(element.get(key)).strip()), '')
        alt = _text(element.get('alt')).strip() or '原文图片'
        try:
            url = normalize_image_url(source, base_url)
        except ExportError as error:
            key = 'invalid:' + source
            if key not in cached:
                cached[key] = {'status': 'failed', 'url': '', 'occurrences': 0,
                               'error_code': error.code, 'message': str(error)}
                image_rows.append(cached[key])
            cached[key]['occurrences'] += 1
            element['data-export-markdown'] = '[' + _label(alt) + '：图片地址不可用]'
            continue
        if url not in cached:
            row = {'url': url, 'status': 'failed', 'occurrences': 0}
            cached[url] = row
            image_rows.append(row)
            try:
                if offline:
                    raise ExportError('offline_image_unavailable', '离线模式没有下载图片，保留原始图片链接')
                data, content_type = download(url, timeout=timeout, max_bytes=max_image_bytes)
                if not isinstance(data, bytes) or len(data) > max_image_bytes:
                    raise ExportError('image_too_large', '下载结果超过图片大小限制或格式不正确')
                extension, detected_mime = image_type(data, content_type)
                filename = hashlib.sha256(url.encode('utf-8')).hexdigest()[:24] + '.' + extension
                path = _inside(article_dir, 'images/' + filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                row.update(status='downloaded', path='images/' + filename, bytes=len(data),
                           content_type=_text(content_type).split(';', 1)[0], detected_type=detected_mime)
            except ExportError as error:
                row.update(error_code=error.code, message=str(error))
            except Exception as error:
                row.update(error_code='image_download_failed', message='图片下载失败：' + type(error).__name__)
        row = cached[url]
        row['occurrences'] += 1
        element['data-export-markdown'] = ('![' + _label(alt) + '](' + row['path'] + ')'
                                           if row['status'] == 'downloaded'
                                           else _markdown_link(url, alt + '（图片未下载，打开源链接）'))

    class OriginalConverter(MarkdownConverter):
        def convert_img(self, el, text, *args, **kwargs):
            # Keep every image position, including images inside headings/links.
            return el.get('data-export-markdown', '')

    markdown = OriginalConverter(heading_style='ATX', bullets='-', wrap=False).convert(str(content)).strip()
    if not markdown:
        raise ExportError('empty_body', '转换后没有正文，未生成成功文章')
    failed = sum(row['status'] != 'downloaded' for row in image_rows)
    if failed:
        issues.append(_issue('images_partial', str(failed) + ' 张不同图片未能下载，保留原出现位置与可用源链接'))
    images = {'status': 'partial' if failed else ('complete' if image_rows else 'not_present'),
              'references': references, 'unique': len(image_rows), 'downloaded': len(image_rows) - failed,
              'failed': failed, 'items': image_rows}
    return markdown, images, issues


def export_originals(articles, out, download=None, offline=False, timeout=DEFAULT_TIMEOUT,
                     max_image_bytes=MAX_IMAGE_BYTES):
    """Export records and return the same dictionary written to export-status.json.

    Inject ``download(url, timeout=..., max_bytes=...) -> (bytes, content_type)``
    for offline tests. ``offline=True`` keeps remote image links without fetching.
    """
    if not isinstance(articles, list):
        raise ExportError('invalid_input', '输入应为 articles.json 中的文章列表')
    if not 0 < timeout <= 300 or not 0 < max_image_bytes <= 100 * 1024 * 1024:
        raise ExportError('invalid_download_limit', '超时须为 0 到 300 秒，单张图片上限须为 0 到 100 MiB')
    out = Path(out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    _check_output_directory(out)
    download = download_image if download is None else download
    rows, seen = [], set()
    for index, article in enumerate(articles):
        row = {'id': article.get('id') if isinstance(article, dict) else None,
               'status': 'failed', 'issues': []}
        rows.append(row)
        try:
            if not isinstance(article, dict):
                raise ExportError('invalid_article', '每篇文章应为一个对象')
            article_id = _safe_id(article.get('id'))
            if article_id in seen:
                raise ExportError('duplicate_article_id', '文章 id 重复，未覆盖前一篇文章')
            seen.add(article_id)
            supplied = _text(article.get('content_text'))
            raw_html = _text(article.get('content_html'))
            if article.get('content_kind') != 'fulltext':
                raise ExportError('not_fulltext', '输入必须显式标记 content_kind=fulltext，不能将摘要当作原文导出')
            if not supplied.strip():
                raise ExportError('content_text_missing', '缺少 content_text 正文；先采集或 import 校验正文，再导出原文')
            if not article.get('content_hash'):
                raise ExportError('content_hash_missing', '缺少采集正文的 content_hash，先采集或 import 校验正文')
            if article['content_hash'] != _digest_text(supplied):
                raise ExportError('content_hash_mismatch', '正文与采集时保存的 content_hash 不一致')
            if _captcha_text(supplied):
                raise ExportError('captcha_page', '正文为验证码或访问验证提示，未导出为文章')
            article_dir = _inside(out, 'originals/' + article_id)
            if raw_html.strip():
                markdown, images, issues = _convert_html(article, article_dir, download, offline, timeout, max_image_bytes)
                mode = 'html'
            else:
                if not supplied.strip():
                    raise ExportError('empty_body', '没有正文文本或 HTML，未生成文章文件')
                markdown = _plain_markdown(supplied)
                images = {'status': 'unavailable', 'references': None, 'unique': None,
                          'downloaded': 0, 'failed': 0, 'items': []}
                issues = [_issue('content_html_unavailable', '只有正文文本，图片和原版式不可恢复，未声称完整图文')]
                mode = 'text'
            destination = _inside(out, 'originals/' + article_id + '/article.md')
            _write_text(destination, _metadata(article) + markdown + '\n')
            row.update(status='partial' if issues else 'complete', mode=mode,
                       markdown=destination.relative_to(out).as_posix(), images=images, issues=issues,
                       published_at=_text(article.get('published_at')) or None)
        except ExportError as error:
            row['issues'].append(_issue(error.code, str(error)))
        except Exception as error:
            row['issues'].append(_issue('export_failed', '导出失败：' + type(error).__name__))
    exported = sum(row['status'] != 'failed' for row in rows)
    complete = sum(row['status'] == 'complete' for row in rows)
    result = {'schema_version': '1.0', 'status': ('complete' if rows and complete == len(rows)
                                                else 'partial' if exported else 'failed'),
              'articles_total': len(rows), 'articles_exported': exported,
              'articles_failed': len(rows) - exported, 'articles': rows}
    _write_text(_inside(out, 'export-status.json'), json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--articles', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--offline', action='store_true', help='Do not download images; retain source links and record partial export')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument('--max-image-bytes', type=int, default=MAX_IMAGE_BYTES)
    args = parser.parse_args(argv)
    try:
        records = json.loads(args.articles.read_text(encoding='utf-8-sig'))
        if isinstance(records, dict):
            records = records.get('articles')
        result = export_originals(records, args.out, offline=args.offline, timeout=args.timeout,
                                  max_image_bytes=args.max_image_bytes)
        print(json.dumps({key: value for key, value in result.items() if key != 'articles'}, ensure_ascii=False))
        return 0 if result['status'] == 'complete' else 2
    except (OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'message': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
