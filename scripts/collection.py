"""Resolve publisher names and export verified body text, with explicit coverage limits.

The collector is an optional locally installed WeRSS service. No cookies, database,
or account-specific credentials are included in this portable adapter.
"""
import base64
import csv
import datetime as dt
from email.utils import parsedate_to_datetime
import io
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

try:
    from watch import canonical, digest, extract_text, parse_feed, read_json, request, write_json, NoRedirect
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from watch import canonical, digest, extract_text, parse_feed, read_json, request, write_json, NoRedirect


DIRECTORY_URL = 'https://raw.githubusercontent.com/hellodword/wechat-feeds/master/list.csv'


class CollectionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _name(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip().casefold()


def _path(value, config_dir):
    path = Path(os.path.expandvars(str(value))).expanduser()
    return path if path.is_absolute() else Path(config_dir) / path


def _mp_id(value):
    value = str(value or '').strip()
    if value.startswith('WEREAD_'):
        value = value[len('WEREAD_'):]
    if re.fullmatch(r'MP_WXS_\d+', value):
        return value
    if value.isdigit():
        return 'MP_WXS_' + value
    try:
        decoded = base64.b64decode(value, validate=True).decode('ascii')
        if decoded.isdigit():
            return 'MP_WXS_' + decoded
    except (ValueError, UnicodeError):
        pass
    raise CollectionError('invalid_account_id', '账号标识须为 MP_WXS_数字、数字或有效的 __biz 标识')


def _issue(account, code, message, severity='error'):
    return {'account': account, 'code': code, 'message': message, 'severity': severity}


def _error(account, exc):
    return _issue(account, getattr(exc, 'code', 'request_failed'),
                  str(exc) if isinstance(exc, CollectionError) else '请求失败：' + type(exc).__name__)


def normalize_article_url(url):
    """Validate a WeChat article URL and retain article identity, including idx."""
    try:
        if not isinstance(url, str) or re.search(r'\s', url.strip()):
            raise ValueError('URL is not a string')
        normalized = canonical(url)
        parts = urllib.parse.urlsplit(normalized)
        if (parts.hostname != 'mp.weixin.qq.com' or parts.port not in (None, 80 if parts.scheme == 'http' else 443)
                or not (parts.path == '/s' or re.fullmatch(r'/s/[^/]+', parts.path))):
            raise ValueError('not a WeChat article')
        if parts.path == '/s':
            query = urllib.parse.parse_qs(parts.query)
            if not query.get('__biz') or not query.get('mid'):
                raise ValueError('article identity missing')
        return urllib.parse.urlunsplit(('https', 'mp.weixin.qq.com', parts.path, parts.query, ''))
    except (ValueError, TypeError):
        raise CollectionError('not_wechat_article', '需要 HTTP(S) mp.weixin.qq.com/s 的公众号原文链接，不接受其它网站或带登录信息的地址') from None


def _links(config):
    rows, indexed, issues = [], {}, []
    for raw in config.get('article_urls', []):
        try:
            url = normalize_article_url(raw)
            key = url
            row = {'url': url, 'requested_urls': [raw], 'status': 'ready'}
        except CollectionError as exc:
            key = 'invalid:' + str(raw)
            row = {'url': str(raw), 'requested_urls': [raw], 'status': 'failed', 'error_code': exc.code}
            issues.append({**_error('', exc), 'url': str(raw)})
        if key in indexed:
            indexed[key]['requested_urls'].append(raw)
        else:
            indexed[key] = row
            rows.append(row)
    return rows, issues


class Client:
    def __init__(self, config, config_dir):
        self.config = config.get('collector', {})
        base = self.config.get('base_url', 'http://127.0.0.1:8006').rstrip('/')
        parts = urllib.parse.urlsplit(base)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise CollectionError('invalid_collector_url', 'collector.base_url 必须是有效的 HTTP(S) 服务地址')
        self.base = base if base.endswith('/api/v1/wx') else base + '/api/v1/wx'
        self.config_dir = config_dir
        self.token = os.environ.get(self.config.get('token_env', 'WECHAT_COLLECTOR_TOKEN'), '')
        self.auth_attempted = False

    def _request(self, path, payload=None, form=None, auth=True):
        headers = {'Accept': 'application/json'}
        if auth and self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                    req, timeout=int(self.config.get('request_timeout_seconds', 180))) as response:
                raw = response.read(20_000_001)
            if len(raw) > 20_000_000:
                raise CollectionError('response_too_large', '采集服务响应超过大小限制')
            result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            code = 'collector_login_required' if exc.code in (401, 403) else 'collector_http_error'
            raise CollectionError(code, '采集服务返回 HTTP ' + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CollectionError('collector_unavailable', '无法连接采集服务；请先安装并启动本机采集器') from None
        except (ValueError, UnicodeError):
            raise CollectionError('invalid_response', '采集服务没有返回有效 JSON') from None
        if not isinstance(result, dict):
            raise CollectionError('invalid_response', '采集服务响应结构错误')
        if result.get('code', 0) != 0:
            # Never echo upstream request headers, cookies, or authentication bodies.
            detail = result.get('data') or {}
            upstream = detail.get('code', result.get('code')) if isinstance(detail, dict) else result.get('code')
            code = 'wechat_login_required' if upstream in (-2012, -2010, -2041, 'missing_cookie') else 'collector_rejected'
            raise CollectionError(code, '采集服务拒绝请求；错误代码：' + str(upstream))
        return result.get('data', result)

    def call(self, path, payload=None):
        if not self.auth_attempted:
            self.auth_attempted = True
            credentials_file = self.config.get('credentials_file')
            if not self.token and credentials_file:
                try:
                    credentials = read_json(_path(credentials_file, self.config_dir))
                    result = self._request('/auth/login', form={k: credentials[k] for k in ('username', 'password')}, auth=False)
                    self.token = result['access_token']
                except (OSError, ValueError, KeyError):
                    raise CollectionError('collector_credentials_missing', '无法读取采集服务登录配置') from None
        return self._request(path, payload=payload)


def _directory(config, config_dir):
    settings = config.get('collector', {})
    filename = settings.get('directory_file')
    if filename:
        path = _path(filename, config_dir)
        if path.suffix.lower() == '.json':
            value = read_json(path)
            return value.get('accounts', []) if isinstance(value, dict) else value
        raw = path.read_text(encoding='utf-8-sig')
    else:
        url = settings.get('directory_url', DIRECTORY_URL)
        if not url:
            return []
        raw, _ = request(url, timeout=30)
    return list(csv.DictReader(io.StringIO(raw.lstrip('\ufeff'))))


def _direct_article(url, expected_name=None):
    url = normalize_article_url(url)
    parts = urllib.parse.urlsplit(url)
    raw, final_url = request(url)
    has_body_container = re.search(r'\bid\s*=\s*[\'\"]js_content[\'\"]', raw, re.I)
    if ('captcha' in final_url.lower() or
            (not has_body_container and re.search(r'\b(?:wappoc_appmsgcaptcha|id=[\'\"](?:captcha|verify))', raw, re.I))):
        raise CollectionError('captcha_required', '微信要求验证码，未取得原文')
    normalize_article_url(final_url)
    try:
        body = extract_text(raw, '#js_content')
        publisher = extract_text(raw, '#js_name').strip()
        title = extract_text(raw, '#activity-name').strip()
    except ValueError:
        raise CollectionError('article_body_unavailable', '原文正文、公众号名或标题不可读取') from None
    if not publisher or not title:
        raise CollectionError('article_body_unavailable', '原文公众号名或标题为空')
    if expected_name is not None and _name(publisher) != _name(expected_name):
        raise CollectionError('account_name_mismatch', '原文显示的公众号名与输入不一致：' + publisher)
    if len(body) < 80:
        raise CollectionError('body_too_short', '正文少于 80 字符，未认定为完整原文')
    published = ''
    match = re.search(r'(?:var\s+)?(?:ct|publish_time)\s*=\s*[\'\"]?(\d{10})', raw)
    if match:
        published = dt.datetime.fromtimestamp(int(match.group(1)), dt.timezone.utc).isoformat()
    biz = urllib.parse.parse_qs(parts.query).get('__biz', [''])[0]
    if not biz:
        match = re.search(r'\bvar\s+biz\s*=\s*[\'\"]([^\'\"]+)', raw)
        biz = match.group(1) if match else ''
    account_id = ''
    if biz:
        try:
            account_id = _mp_id(biz)
        except CollectionError:
            pass
    return {'title': title, 'account': publisher, 'account_id': account_id, 'url': url,
            'original_wechat_url': url, 'content_text': body, 'content_kind': 'fulltext',
            'content_hash': digest(body), 'published_at': published,
            'date_source': 'original_page_timestamp' if published else 'unknown',
            'fetched_at': _now(), 'provenance': ('公众号原文页面在线读取，并核对公众号名称' if expected_name is not None
                                               else '用户指定文章链接在线读取；公众号名称取自原文页面')}


def _resolve(config, config_dir, client):
    resolved, issues = [], []
    directory = None
    directory_error = None
    max_pages = int(config.get('collector', {}).get('max_pages', 20))
    for raw in config.get('accounts', []):
        account = {'name': raw} if isinstance(raw, str) else dict(raw)
        wanted = account.get('name', '').strip()
        row = {**account, 'requested_name': wanted, 'status': 'unresolved', 'candidates': []}
        resolved.append(row)
        if not wanted:
            issues.append(_issue(wanted, 'account_name_missing', '每个公众号都需要填写名称，以便核对来源'))
            continue
        if account.get('article_url') and not account.get('id'):
            try:
                article = _direct_article(account['article_url'], wanted)
                row.update(status='resolved_article', evidence='live_original_page', verified_at=_now())
                if article['account_id']:
                    row['id'] = article['account_id']
                    try:
                        identity = client.call('/weread/mp/test', {'mp_id': row['id']})
                        if _name(identity.get('mp_name')) != _name(wanted):
                            raise CollectionError('account_name_mismatch', '文章对应账号在采集器中的名称不一致')
                        row.update(status='resolved', evidence='live_original_page_and_weread_cover')
                    except Exception as exc:
                        issues.append(_error(wanted, exc))
                        issues.append(_issue(wanted, 'article_only_fallback', '原文可读取，但账号采集入口未通过核验，本次只能交付这篇文章', 'warning'))
                continue
            except Exception as exc:
                issues.append(_error(wanted, exc))
                continue
        if account.get('rss_url') and not account.get('id'):
            row.update(status='resolved_rss', evidence='verify_each_original_page_during_collection')
            continue
        candidates = {}
        resolution_errors = []
        truncated = False

        def add(name, identifier, evidence):
            try:
                identifier = _mp_id(identifier)
            except CollectionError:
                return
            if _name(name) == _name(wanted):
                candidates.setdefault(identifier, {'id': identifier, 'name': name, 'evidence': evidence})

        if account.get('id'):
            try:
                add(wanted, _mp_id(account['id']), 'explicit_id')
            except CollectionError as exc:
                issues.append(_error(wanted, exc))
                continue
        else:
            # Existing subscription names and publisher-platform search are separate capabilities.
            for search_mode in ('local', 'platform'):
                try:
                    for page in range(max_pages):
                        path = ('/mps?kw=' + urllib.parse.quote(wanted) if search_mode == 'local'
                                else '/mps/search/' + urllib.parse.quote(wanted) + '?')
                        path += ('&' if search_mode == 'local' else '') + urllib.parse.urlencode({'limit': 100, 'offset': page * 100})
                        data = client.call(path)
                        entries = data.get('list') or []
                        for item in entries:
                            add(item.get('mp_name') or item.get('nickname') or item.get('name'),
                                item.get('id') or item.get('fakeid') or item.get('bizid'), search_mode)
                        total = int(data.get('total') or 0)
                        if not entries or (page + 1) * 100 >= total:
                            break
                    else:
                        truncated = True
                except Exception as exc:
                    resolution_errors.append(_error(wanted, exc))
            if directory is None and directory_error is None:
                try:
                    directory = _directory(config, config_dir)
                except Exception as exc:
                    directory_error = _error(wanted, exc)
            if directory is not None:
                for item in directory:
                    add(item.get('name'), item.get('id') or item.get('bizid'), 'public_directory_candidate')
            if directory_error:
                resolution_errors.append({**directory_error, 'account': wanted})
        verified = []
        failed_candidates = []
        for candidate in candidates.values():
            try:
                identity = client.call('/weread/mp/test', {'mp_id': candidate['id']})
                live_name = identity.get('mp_name', '')
                candidate['live_name'] = live_name
                if _name(live_name) == _name(wanted):
                    verified.append(candidate)
                else:
                    failed_candidates.append(_issue(wanted, 'account_name_mismatch', '候选账号当前名称为：' + live_name))
            except Exception as exc:
                failed_candidates.append(_error(wanted, exc))
        row['candidates'] = list(candidates.values())
        if len(verified) == 1 and not truncated and (account.get('id') or not failed_candidates):
            row.update(id=verified[0]['id'], name=verified[0]['live_name'], status='resolved',
                       evidence='live_weread_cover', verified_at=_now())
        elif len(verified) > 1:
            row['status'] = 'ambiguous'
            issues.append(_issue(wanted, 'ambiguous_account', '多个账号通过同名核验，请在配置中填写准确 id'))
        else:
            issues.extend(failed_candidates or resolution_errors)
            if truncated:
                issues.append(_issue(wanted, 'search_truncated', '账号搜索达到分页上限；请提供准确 id'))
            issues.append(_issue(wanted, 'account_unresolved', '无法唯一核验公众号；可提供 id、该号文章链接或自己的目录文件。公众号平台搜索需要另外的后台登录，空结果不等于账号不存在。'))
    return {'accounts': resolved, 'issues': issues}


def resolve(config, config_dir):
    client = Client(config, Path(config_dir)) if config.get('accounts') else None
    result = _resolve(config, Path(config_dir), client)
    links, link_issues = _links(config)
    result['links'] = links
    result['issues'].extend(link_issues)
    return result


def _backend_article(item, account):
    content = item.get('content') or ''
    text = extract_text(content)
    if len(text) < 80:
        raise CollectionError('body_missing', '采集记录没有可用的正文，未导出摘要')
    url = normalize_article_url(item.get('url') or item.get('link') or '')
    if item.get('mp_id') and _mp_id(item['mp_id']) != _mp_id(account['id']):
        raise CollectionError('account_id_mismatch', '采集记录的公众号标识不一致')
    return {'title': item.get('title') or '未命名文章', 'account': account['name'],
            'account_id': account['id'], 'url': url, 'original_wechat_url': url,
            'content_text': text, 'content_kind': 'fulltext', 'content_hash': digest(text),
            # Current WeRSS writes capture time into publish_time in cover fallback mode.
            'published_at': '', 'date_source': 'unknown_backend_may_use_capture_time',
            'backend_publish_time': item.get('publish_time'), 'fetched_at': _now(),
            'provenance': '已登录微信读书采集正文；账号名在线核验；原文链接由采集器提供'}


def _timestamp(value, end=False):
    if not value:
        return None
    try:
        result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        try:
            result = parsedate_to_datetime(str(value))
        except (ValueError, TypeError):
            return None
    if not result.tzinfo:
        result = result.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    if end and re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)):
        result += dt.timedelta(days=1, microseconds=-1)
    return result


def _selected(article, selection):
    date = _timestamp(article.get('published_at'))
    since = _timestamp(selection.get('since'))
    until = _timestamp(selection.get('until'), end=True)
    if date is None and selection.get('unknown_date', 'include') == 'exclude':
        return False, 'unknown_date'
    if date and ((since and date < since) or (until and date > until)):
        return False, 'outside_date_range'
    text = _name(article.get('title', '') + '\n' + article['content_text'])
    include = selection.get('include_keywords') or []
    exclude = selection.get('exclude_keywords') or []
    if include and not any(_name(word) in text for word in include):
        return False, 'include_keywords'
    if any(_name(word) in text for word in exclude):
        return False, 'exclude_keywords'
    return True, ''


def collect(config, config_dir, out):
    config_dir, out = Path(config_dir), Path(out)
    client = Client(config, config_dir) if config.get('accounts') else None
    result = _resolve(config, config_dir, client)
    issues, accounts = result['issues'], result['accounts']
    selection = config.get('selection', {})
    links, link_issues = _links(config)
    issues.extend(link_issues)
    fingerprint = digest({'accounts': config.get('accounts', []), 'article_urls': config.get('article_urls', []),
                          'selection': selection, 'collector_url': client.base if client else None})
    previous = {}
    if (out / 'collection-status.json').exists() and (out / 'articles.json').exists():
        try:
            status = read_json(out / 'collection-status.json')
            if status.get('config_fingerprint') == fingerprint:
                for article in read_json(out / 'articles.json'):
                    if (article.get('content_kind') == 'fulltext' and len(article.get('content_text', '')) >= 80
                            and article.get('content_hash') == digest(article['content_text'])):
                        previous[normalize_article_url(article['url'])] = {**article, 'reused_from_previous_run': True}
        except (OSError, ValueError, KeyError, TypeError):
            issues.append(_issue('', 'previous_export_invalid', '上一轮原文文件未通过完整性检查，已忽略', 'warning'))
    articles = dict(previous)
    source_coverage = []
    max_pages = int(config.get('collector', {}).get('max_pages', 20))
    for account in accounts:
        name = account['requested_name']
        coverage = {'account': name, 'status': 'failed', 'historical_coverage': 'unknown',
                    'backend_records': 0, 'body_failures': 0, 'list_truncated': False}
        source_coverage.append(coverage)
        if account['status'] not in ('resolved', 'resolved_article', 'resolved_rss'):
            continue
        received = []
        source_ok = True
        if account['status'] == 'resolved_article':
            try:
                received.append(_direct_article(account['article_url'], name))
                coverage['scope'] = 'one_supplied_article'
                coverage['historical_coverage'] = 'not_requested'
            except Exception as exc:
                issues.append(_error(name, exc))
                source_ok = False
        elif account.get('rss_url'):
            coverage['scope'] = 'entries_currently_present_in_rss'
            try:
                raw, _ = request(account['rss_url'])
                entries = parse_feed(raw, name)
                coverage['backend_records'] = len(entries)
                for item in entries:
                    try:
                        article = _direct_article(item['url'], name)
                        if not article['published_at'] and item.get('published_at'):
                            article['published_at'] = item['published_at']
                            article['date_source'] = 'rss_metadata_unverified'
                        received.append(article)
                    except Exception as exc:
                        issues.append(_error(name, exc))
                        coverage['body_failures'] += 1
                        source_ok = False
            except Exception as exc:
                issues.append(_error(name, exc))
                source_ok = False
        else:
            coverage['scope'] = 'collector_increment_plus_existing_database'
            items = {}
            def merge_item(item):
                url = item.get('url') or item.get('link')
                key = canonical(url) if url else str(item.get('id'))
                existing = items.get(key)
                # Body persistence may lag the immediate collect response.
                if existing and len(existing.get('content') or '') > len(item.get('content') or ''):
                    return
                items[key] = item
            try:
                response = client.call('/weread/collect', {'mp_id': account['id'], 'faker_id': account['id'],
                                                          'mp_name': account['name'], 'gather_content': True})
                for item in response.get('articles', []):
                    merge_item(item)
                coverage['new_records_reported'] = response.get('collected', 0)
            except Exception as exc:
                issues.append(_error(name, exc))
                source_ok = False
            try:
                for page in range(max_pages):
                    data = client.call('/articles?' + urllib.parse.urlencode(
                        {'mp_id': account['id'], 'limit': 100, 'offset': page * 100}))
                    entries = data.get('list') or []
                    total = int(data.get('total') or 0)
                    coverage['backend_records'] = total
                    for item in entries:
                        identifier = item.get('id')
                        if not identifier:
                            continue
                        # List API deliberately omits body text; detail API returns stored content.
                        try:
                            detail = client.call('/articles/' + urllib.parse.quote(str(identifier), safe=''))
                            merge_item(detail)
                        except Exception as exc:
                            issues.append(_error(name, exc))
                            coverage['body_failures'] += 1
                            source_ok = False
                    if not entries or (page + 1) * 100 >= total:
                        break
                else:
                    coverage['list_truncated'] = True
                    issues.append(_issue(name, 'database_page_limit', '数据库文章列表达到分页上限，部分存量未导出', 'warning'))
                    source_ok = False
            except Exception as exc:
                issues.append(_error(name, exc))
                source_ok = False
            for item in items.values():
                try:
                    received.append(_backend_article(item, account))
                except Exception as exc:
                    issues.append(_error(name, exc))
                    coverage['body_failures'] += 1
                    source_ok = False
            issues.append(_issue(name, 'historical_coverage_unknown',
                                 '采集器可能只返回最新一篇；数据库存量不等于公众号完整历史，无法保证指定日期范围全量', 'warning'))
        for article in received:
            articles[normalize_article_url(article['url'])] = article
        coverage['fetched_bodies'] = len(received)
        coverage['status'] = 'available_sources_read' if source_ok else ('partial' if received else 'failed')
    for link in links:
        if link['status'] == 'failed':
            continue
        key = link['url']
        link['attempted_at'] = _now()
        try:
            # Explicit top-level article_urls never discover, subscribe to, or collect an account.
            article = _direct_article(key)
            articles[key] = article
            link.update(status='fetched', account=article['account'], fetched_at=article['fetched_at'])
        except Exception as exc:
            issue = {**_error('', exc), 'url': key}
            issues.append(issue)
            link.update(status='failed', error_code=issue['code'])
            if key in previous:
                articles[key].update(last_attempt_status='failed', last_attempt_at=link['attempted_at'])
                link['reused_from_previous_run'] = True
    return _export(config, out, articles.values(), accounts, issues, source_coverage, fingerprint, links)


def _export(config, out, records, accounts, issues, source_coverage, fingerprint, links=None):
    out = Path(out)
    previously_managed = []
    if (out / 'articles.json').exists():
        try:
            for old in read_json(out / 'articles.json'):
                expected = 'originals/' + digest(canonical(old['url']))[:20] + '.md'
                if old.get('original_file') == expected:
                    previously_managed.append(expected)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    selection = config.get('selection', {})
    selected, excluded = [], {}
    counts = {}
    allowed = {_name(a['requested_name']) for a in accounts}
    links = links or []
    allowed_urls = {link['url'] for link in links if link.get('error_code') != 'not_wechat_article'}
    selection_by_url = {}
    for article in sorted(records, key=lambda a: (a.get('published_at') or '', a.get('fetched_at') or ''), reverse=True):
        article_url = normalize_article_url(article['url'])
        if _name(article.get('account')) not in allowed and article_url not in allowed_urls:
            continue
        keep, reason = _selected(article, selection)
        key = _name(article['account'])
        if keep and counts.get(key, 0) >= int(selection.get('max_articles_per_account', 20)):
            keep, reason = False, 'max_articles_per_account'
        if not keep:
            excluded[reason] = excluded.get(reason, 0) + 1
            selection_by_url[article_url] = {'selected': False, 'selection_reason': reason}
            continue
        counts[key] = counts.get(key, 0) + 1
        identifier = digest(canonical(article['url']))[:20]
        article['id'] = identifier
        article['article_id'] = identifier
        article['original_file'] = 'originals/' + identifier + '.md'
        selected.append(article)
        selection_by_url[article_url] = {'selected': True, 'article_id': identifier}
    unknown_count = sum(not _timestamp(a.get('published_at')) for a in selected)
    if unknown_count:
        issues.append(_issue('', 'unknown_publication_dates',
                             f'{unknown_count} 篇正文的发布日期未核实；不能证明属于所选日期范围', 'warning'))
    if excluded.get('max_articles_per_account'):
        issues.append(_issue('', 'selection_article_limit', '部分已取得正文超过每个公众号的篇数上限，未列入本次交付', 'warning'))
    if not selected:
        issues.append(_issue('', 'no_matching_fulltext', '没有取得符合条件的可交付原文'))
    coverage = {'status': 'partial' if selected and issues else ('complete' if selected else 'failed'),
                'historical_coverage': 'unknown' if any(x['historical_coverage'] == 'unknown' for x in source_coverage) else 'not_requested',
                'sources': source_coverage, 'selected_articles': len(selected), 'unknown_date_articles': unknown_count,
                'reused_articles': sum(bool(a.get('reused_from_previous_run')) for a in selected), 'excluded': excluded}
    for link in links:
        link.update(selection_by_url.get(link['url'], {'selected': False}))
        source_coverage.append({'source_type': 'article_url', 'scope': 'one_supplied_article',
                                'historical_coverage': 'not_requested', **link})
    coverage['links'] = {'requested': len(config.get('article_urls', [])), 'unique_requested': len(links),
                         'duplicates': len(config.get('article_urls', [])) - len(links),
                         'succeeded': sum(link['status'] == 'fetched' for link in links),
                         'imported': sum(link['status'] == 'imported' for link in links),
                         'failed': sum(link['status'] == 'failed' for link in links),
                         'selected': sum(link['selected'] for link in links),
                         'filtered': sum(bool(link.get('selection_reason')) for link in links),
                         'reused': sum(bool(link.get('reused_from_previous_run')) for link in links)}
    current_files = {a['original_file'] for a in selected}
    originals_root = (out / 'originals').resolve()
    for relative in previously_managed:
        if relative in current_files:
            continue
        target = (out / relative).resolve()
        # Remove only exact generated files from the previous manifest, never directories.
        if target.parent == originals_root and re.fullmatch(r'[a-f0-9]{20}\.md', target.name) and target.is_file():
            target.unlink()
    for article in selected:
        target = out / article['original_file']
        target.parent.mkdir(parents=True, exist_ok=True)
        read_status = ('本轮读取失败；保留上轮已保存正文' if article.get('last_attempt_status') == 'failed'
                       else '复用上轮已保存正文' if article.get('reused_from_previous_run') else '本轮取得正文')
        target.write_text(f"# {article['title']}\n\n公众号：{article['account']}\n\n原文：{article['url']}\n\n"
                          f"发布日期：{article['published_at'] or '未核实'}\n\n时间来源：{article['date_source']}\n\n"
                          f"读取时间：{article['fetched_at']}\n\n本轮状态：{read_status}\n\n正文哈希：{article['content_hash']}\n\n"
                          f"来源：{article['provenance']}\n\n---\n\n{article['content_text']}\n", encoding='utf-8')
    write_json(out / 'articles.json', selected)
    write_json(out / 'collection-status.json', {'config_fingerprint': fingerprint, 'generated_at': _now(),
                                                'accounts': accounts, 'links': links, 'issues': issues, 'coverage': coverage})
    return {'articles': selected, 'accounts': accounts, 'links': links, 'issues': issues, 'coverage': coverage}


def import_articles(config, records, out):
    """Export explicitly supplied full text; this does not assert a fresh online read."""
    accounts = []
    for value in config.get('accounts', []):
        value = {'name': value} if isinstance(value, str) else dict(value)
        accounts.append({**value, 'requested_name': value['name'], 'status': 'import_only',
                         'evidence': 'user_supplied_text_not_live_verified'})
    allowed = {_name(a['name']): a for a in accounts}
    links, issues = _links(config)
    allowed_urls = {link['url'] for link in links if link['status'] == 'ready'}
    accepted = {}
    for raw in records:
        item = dict(raw)
        name = item.get('account', '')
        try:
            url = normalize_article_url(item.get('original_wechat_url') or item.get('url') or '')
            if _name(name) not in allowed and url not in allowed_urls:
                continue
            if not name.strip():
                raise CollectionError('account_name_missing', '导入正文需要保留来源公众号名称')
            if item.get('content_kind') != 'fulltext':
                raise CollectionError('not_fulltext', '导入正文必须显式标记 content_kind=fulltext')
            text = item.get('content_text', '').strip()
            if len(text) < 80:
                raise CollectionError('body_too_short', '导入正文少于 80 字符')
            if item.get('content_hash') and item['content_hash'] != digest(text):
                raise CollectionError('content_hash_mismatch', '导入正文与保存的哈希不一致')
            configured_id = allowed.get(_name(name), {}).get('id')
            if configured_id and item.get('account_id') and _mp_id(configured_id) != _mp_id(item['account_id']):
                raise CollectionError('account_id_mismatch', '导入正文与配置的账号标识不一致')
            item.update(url=url, original_wechat_url=url, content_text=text, content_hash=digest(text),
                        title=item.get('title') or '未命名文章', account=allowed.get(_name(name), {}).get('name', name))
            item.setdefault('published_at', '')
            item.setdefault('date_source', 'import_metadata_unverified' if item['published_at'] else 'unknown')
            item.setdefault('fetched_at', _now())
            item['imported_at'] = _now()
            item['provenance'] = '用户提供的正文，未在线重新核验；' + item.get('provenance', '来源未注明')
            accepted[canonical(url)] = item
        except Exception as exc:
            issues.append(_error(name, exc))
    issues.append(_issue('', 'import_not_live_verified', '本次导入已有正文，未在线重新核验账号、发布日期和历史覆盖', 'warning'))
    coverage = [{'account': a['name'], 'status': 'imported', 'scope': 'provided_fulltext_only',
                 'historical_coverage': 'unknown'} for a in accounts]
    for link in links:
        if link['status'] == 'failed':
            continue
        if link['url'] in accepted:
            link.update(status='imported', account=accepted[link['url']]['account'])
        else:
            link.update(status='failed', error_code='specified_article_not_imported')
            issues.append({**_issue('', 'specified_article_not_imported', '指定文章链接没有对应的有效导入正文'), 'url': link['url']})
    fingerprint = digest({'accounts': config.get('accounts', []), 'article_urls': config.get('article_urls', []),
                          'selection': config.get('selection', {}), 'source': 'import'})
    return _export(config, Path(out), accepted.values(), accounts, issues, coverage, fingerprint, links)
