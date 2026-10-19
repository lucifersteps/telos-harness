"""微信接口回执与客户端可见性分开记录；外部网络使用桩。"""
import asyncio
from contextlib import contextmanager
import json
import logging
from urllib.error import HTTPError

import pytest

from telos.adapters.base import ConversationRef
from telos.adapters.delivery import delivery_fingerprint, delivery_log_context
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.outbound import create_wechat_send_text_fn
from telos.adapters.wechat.polling import create_wechat_get_updates_fn
from telos.core.logging import JsonLogFormatter


def events(caplog, name):
    return [r for r in caplog.records if getattr(r, 'event', '') == name]


def test_shared_http_transport_still_polls_without_send_receipts(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    class Response:
        def read(self):
            return b'{"ret":0,"msgs":[],"get_updates_buf":"next-cursor"}'

    @contextmanager
    def urlopen(req, timeout):
        assert 'get_updates_buf' in json.loads(req.data)
        yield Response()

    monkeypatch.setattr('telos.adapters.wechat.outbound.urllib_request.urlopen', urlopen)
    result = asyncio.run(create_wechat_get_updates_fn(WeChatConfig(token='secret'))('cursor'))
    assert result.ok and result.sync_buf == 'next-cursor'
    assert not any(getattr(r, 'event', '').startswith('wechat.send.') for r in caplog.records)


def test_chunks_share_delivery_id_but_keep_individual_request_and_server_ids(caplog):
    from telos.adapters.wechat.outbound import WeChatOutbound
    caplog.set_level(logging.INFO)
    calls = []

    async def post(url, body, headers, timeout):
        calls.append(body)
        return {'ret': 0, 'message_id': f'server-{len(calls)}'}

    async def run():
        config = WeChatConfig(token='secret')
        adapter = WeChatOutbound(config=config, send_text_fn=create_wechat_send_text_fn(config,
            post_json_fn=post, client_id_factory=lambda: f'client-{len(calls)}'))
        token = delivery_log_context.set({'delivery_id': 'multi-part'})
        try:
            return await adapter.send_text(ConversationRef(platform='wechat', chat_id='user'), '第一段\n---\n第二段')
        finally:
            delivery_log_context.reset(token)

    result = asyncio.run(run())
    assert result.ok and result.message_count == 2
    first, second = events(caplog, 'wechat.send.response')
    assert first.delivery_id == second.delivery_id == 'multi-part'
    assert first.client_id != second.client_id and first.message_id != second.message_id
    assert first.text_fingerprint != second.text_fingerprint


@pytest.mark.parametrize('response', [{'ret': 0, 'message_id': 7517436300997961736}, {'ret': 0}, {}])
def test_http_receipt_preserves_server_id_and_does_not_claim_client_delivery(monkeypatch, caplog, response):
    caplog.set_level(logging.INFO)
    calls = []

    class Response:
        status = 200
        def read(self):
            return json.dumps(response).encode()

    @contextmanager
    def urlopen(req, timeout):
        calls.append(json.loads(req.data))
        yield Response()

    monkeypatch.setattr('telos.adapters.wechat.outbound.urllib_request.urlopen', urlopen)

    async def run():
        token = delivery_log_context.set({'delivery_id': 'delivery-a', 'input_event_ids': ['input-a']})
        try:
            send = create_wechat_send_text_fn(WeChatConfig(token='secret-auth'),
                context_token_provider=lambda ref: 'secret-context', client_id_factory=lambda: 'client-a')
            return await send(ConversationRef(platform='wechat', chat_id='private-user'), '私人日程正文')
        finally:
            delivery_log_context.reset(token)

    result = asyncio.run(run())
    assert result.ok and len(calls) == 1
    receipt, = events(caplog, 'wechat.send.response')
    http, = events(caplog, 'wechat.send.http')
    assert http.http_status == 200 and http.delivery_id == receipt.delivery_id == 'delivery-a'
    assert receipt.message_id == str(response.get('message_id', ''))
    assert receipt.ret == response.get('ret')  # 缺省值不能在日志里被补为 0。
    assert receipt.receipt == 'api_accepted' and receipt.client_delivery == 'unknown'
    assert receipt.context_fingerprint == delivery_fingerprint('secret-context')
    assert http.client_id == receipt.client_id == 'client-a'
    serialized = '\n'.join(JsonLogFormatter().format(r) for r in caplog.records)
    for private in ('secret-auth', 'secret-context', 'private-user', '私人日程正文'):
        assert private not in serialized


def test_stale_context_retry_records_both_attempts_and_redacts_response(caplog):
    caplog.set_level(logging.INFO)
    calls, cleared = [], []

    async def post(url, body, headers, timeout):
        calls.append(body)
        if len(calls) == 1:
            return {'ret': -14, 'errmsg': 'expired secret-context secret-auth private-user 私人正文'}
        return {'ret': 0, 'msg_id': 'server-b'}

    async def run():
        send = create_wechat_send_text_fn(WeChatConfig(token='secret-auth'), post_json_fn=post,
            context_token_provider=lambda ref: 'secret-context',
            context_token_clearer=lambda ref: cleared.append(ref),
            client_id_factory=lambda: f'client-{len(calls)}')
        return await send(ConversationRef(platform='wechat', chat_id='private-user'), '私人正文')

    assert asyncio.run(run()).ok and len(cleared) == 1
    first, second = events(caplog, 'wechat.send.response')
    assert first.receipt == 'api_rejected' and second.receipt == 'api_accepted'
    assert first.attempt == 1 and second.attempt == 2
    assert first.context_fingerprint and not second.context_fingerprint
    assert len(events(caplog, 'wechat.send.context_retry')) == 1
    serialized = '\n'.join(JsonLogFormatter().format(r) for r in caplog.records)
    assert all(s not in serialized for s in ('secret-context', 'secret-auth', 'private-user', '私人正文'))


@pytest.mark.parametrize('failure', ['http', 'json', 'cancel'])
def test_transport_failure_and_cancellation_are_not_success(monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO)

    class Response:
        status = 200
        def read(self):
            return b'not json'

    @contextmanager
    def urlopen(req, timeout):
        if failure == 'http':
            raise HTTPError(req.full_url, 503, 'unavailable', {}, None)
        yield Response()

    monkeypatch.setattr('telos.adapters.wechat.outbound.urllib_request.urlopen', urlopen)

    async def cancelled(*args):
        raise asyncio.CancelledError()

    async def run():
        send = create_wechat_send_text_fn(WeChatConfig(token='secret'),
            post_json_fn=cancelled if failure == 'cancel' else None)
        return await send(ConversationRef(platform='wechat', chat_id='user'), 'test')

    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
        assert len(events(caplog, 'wechat.send.cancelled')) == 1
    else:
        assert not asyncio.run(run()).ok
        assert len(events(caplog, 'wechat.send.failed')) == 1
        if failure == 'http':
            assert events(caplog, 'wechat.send.http_failed')[0].http_status == 503
    assert not events(caplog, 'wechat.send.response')
