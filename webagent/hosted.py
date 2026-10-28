"""Single-owner gateway with authentication and a loopback sandbox listener."""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import json
import os
import re
import signal
from urllib.parse import urlsplit


@dataclass(frozen=True)
class HostedConfig:
    public_origin: str
    username: str
    password_sha256: str
    internal_port: int = 8777
    public_port: int = 8080

    def __post_init__(self):
        parsed = urlsplit(self.public_origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError('WEBAGENT_PUBLIC_ORIGIN must be an HTTPS origin.') from exc
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or parsed.hostname in {'localhost', '127.0.0.1', '::1'}
                or self.public_origin != 'https://' + parsed.netloc.lower()
                or any(c in parsed.netloc for c in '\r\n') or port == 0):
            raise ValueError('WEBAGENT_PUBLIC_ORIGIN must be a canonical HTTPS origin without a path.')
        if not self.username or len(self.username) > 100 or ':' in self.username or any(ord(c) < 32 for c in self.username):
            raise ValueError('Configure a nonempty WEBAGENT_AUTH_USER without colons or control characters.')
        if (not re.fullmatch(r'[0-9a-f]{64}', self.password_sha256)
                or self.password_sha256 == hashlib.sha256(b'').hexdigest()):
            raise ValueError('Configure WEBAGENT_AUTH_PASSWORD_SHA256 as a lowercase SHA256 hex digest.')
        if not 1 <= self.internal_port <= 65535 or not 1 <= self.public_port <= 65535 or self.internal_port == self.public_port:
            raise ValueError('Hosted public and internal ports must be distinct valid ports.')

    @property
    def public_host(self):
        return urlsplit(self.public_origin).netloc

    @property
    def internal_host(self):
        return '127.0.0.1:' + str(self.internal_port)

    @classmethod
    def from_environment(cls):
        try:
            inner_port = int(os.environ.get('WEBAGENT_INTERNAL_PORT', '8777'))
            pub_port = int(os.environ.get('PORT', os.environ.get('WEBAGENT_HOSTED_PORT', '8080')))
        except ValueError as exc:
            raise ValueError('Hosted ports must be integers.') from exc
        config = cls(os.environ.get('WEBAGENT_PUBLIC_ORIGIN', ''), os.environ.get('WEBAGENT_AUTH_USER', ''),
                     os.environ.get('WEBAGENT_AUTH_PASSWORD_SHA256', ''), inner_port, pub_port)
        key = os.environ.get('LLM_API_KEY')
        if key and hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), config.password_sha256):
            raise ValueError('The web login password must be different from the model API credential.')
        return config


async def respond(send, status, message, *, challenge=False):
    body = json.dumps({'detail': message}, ensure_ascii=False).encode()
    headers = [(b'content-type', b'application/json; charset=utf-8'), (b'cache-control', b'no-store'),
               (b'x-content-type-options', b'nosniff')]
    if challenge:
        headers.append((b'www-authenticate', b'Basic realm="ShopTrace", charset="UTF-8"'))
    await send({'type': 'http.response.start', 'status': status, 'headers': headers})
    await send({'type': 'http.response.body', 'body': body})


class HostedGateway:
    def __init__(self, app, config: HostedConfig):
        self.app, self.config = app, config
        self.state = app.state

    def authenticated(self, value):
        try:
            scheme, encoded = value.split(' ', 1)
            if scheme.lower() != 'basic' or len(encoded) > 4096:
                return False
            raw = base64.b64decode(encoded, validate=True).decode('utf-8')
            username, password = raw.split(':', 1)
            user_ok = hmac.compare_digest(username.encode(), self.config.username.encode())
            pass_ok = len(password) >= 16 and hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), self.config.password_sha256)
            return user_ok and pass_ok
        except (ValueError, UnicodeError):
            return False

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self.app(scope, receive, send)
            return
        if scope['type'] != 'http':
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
            return
        # Health probes expose no model settings or history.
        if scope['path'] == '/healthz' and scope['method'] in {'GET', 'HEAD'}:
            body = b'' if scope['method'] == 'HEAD' else b'{"ready":true}'
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'application/json'), (b'cache-control', b'no-store')]})
            await send({'type': 'http.response.body', 'body': body})
            return
        raw_hdrs = scope.get('headers', [])
        headers = {}
        for name, value in raw_hdrs:
            key = name.lower()
            if key in {b'host', b'authorization', b'origin'} and key in headers:
                await respond(send, 400, 'Duplicate request boundary header.')
                return
            headers[key] = value
        host = headers.get(b'host', b'').decode('latin-1').lower()
        if host != self.config.public_host:
            await respond(send, 400, 'Unexpected public host.')
            return
        if not self.authenticated(headers.get(b'authorization', b'').decode('latin-1')):
            await respond(send, 401, 'Authentication required.', challenge=True)
            return
        if scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}:
            if headers.get(b'origin', b'').decode('latin-1') != self.config.public_origin:
                await respond(send, 403, 'Write requests require the configured same-origin Origin header.')
                return
        # Strip login and proxy headers before forwarding.
        filtered = [(name, value) for name, value in raw_hdrs
                    if name.lower() not in {b'host', b'authorization', b'origin', b'cookie', b'forwarded'}
                    and not name.lower().startswith(b'x-forwarded-')]
        # Forward only sandbox cookies.
        keep = []
        try:
            cookies = SimpleCookie()
            cookies.load(headers.get(b'cookie', b'').decode('latin-1'))
            selected = cookies.get('shoptrace_case')
            if selected is not None:
                from .cases import get_case_public
                case_id = selected.value
                try:
                    get_case_public(case_id)
                    keep.append('shoptrace_case=' + case_id)
                except ValueError:
                    pass
            proc = cookies.get('st_proc')
            if proc is not None and re.fullmatch(r'[0-9a-f]{32}', proc.value):
                keep.append('st_proc=' + proc.value)
        except (CookieError, ValueError, UnicodeError):
            pass
        if keep:
            filtered.append((b'cookie', '; '.join(keep).encode('ascii')))
        filtered.append((b'host', self.config.internal_host.encode()))
        if b'origin' in headers:
            filtered.append((b'origin', ('http://' + self.config.internal_host).encode()))
        inner = dict(scope, headers=filtered, scheme='http', server=('127.0.0.1', self.config.internal_port))

        async def private_send(message):
            if message['type'] == 'http.response.start':
                message = dict(message)
                resp_hdrs = [(n, v) for n, v in message.get('headers', [])
                                    if n.lower() not in {b'cache-control', b'strict-transport-security'}]
                resp_hdrs.extend([(b'cache-control', b'no-store'),
                                         (b'strict-transport-security', b'max-age=31536000')])
                message['headers'] = resp_hdrs
            await send(message)
        await self.app(inner, receive, private_send)


class SandboxListener:
    """Loopback store access for Chromium."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
            return
        path, method = scope['path'], scope['method']
        writes = {'/sandbox/procurement/api/add', '/sandbox/procurement/api/cart',
                  '/sandbox/procurement/api/remove'}
        read = method in {'GET', 'HEAD'} and path.startswith('/sandbox/')
        write = method == 'POST' and path in writes
        if not (read or write):
            await respond(send, 404, 'Not found.')
            return
        if write:
            headers = {}
            for name, value in scope.get('headers', []):
                key = name.lower()
                if key in {b'host', b'origin'} and key in headers:
                    await respond(send, 400, 'Duplicate request boundary header.')
                    return
                headers[key] = value
            host = headers.get(b'host', b'').decode('latin-1')
            origin = headers.get(b'origin', b'').decode('latin-1')
            if not host or origin != scope.get('scheme', 'http') + '://' + host:
                await respond(send, 403, 'Store writes require the same-origin Origin header.')
                return
        await self.app(scope, receive, send)


def create_hosted_app(config=None, *, inner_app=None, data_dir=None, report_dir=None):
    config = config or HostedConfig.from_environment()
    if inner_app is None:
        from .app import create_app
        inner_app = create_app(data_dir)
    report_dir = report_dir if report_dir is not None else os.environ.get('SHOPTRACE_REPORT_DIR')
    if report_dir:
        from pathlib import Path
        from starlette.staticfiles import StaticFiles
        path = Path(report_dir).resolve()
        if not path.is_dir():
            raise ValueError('SHOPTRACE_REPORT_DIR must identify an existing report directory.')
        inner_app.mount('/reports', StaticFiles(directory=path, html=True, follow_symlink=False), name='hosted-reports')
    return HostedGateway(inner_app, config), SandboxListener(inner_app)


async def serve(config, *, data_dir=None):
    import uvicorn
    public_app, inner_app = create_hosted_app(config, data_dir=data_dir)
    public = uvicorn.Server(uvicorn.Config(public_app, host='0.0.0.0', port=config.public_port,
                                          proxy_headers=False, server_header=False, workers=1))
    internal = uvicorn.Server(uvicorn.Config(inner_app, host='127.0.0.1', port=config.internal_port,
                                            proxy_headers=False, server_header=False, lifespan='off', access_log=False))
    # One signal handler lets both listeners finish shutdown.
    public.capture_signals = internal.capture_signals = nullcontext
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    old_sigs = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_sigs[sig] = signal.getsignal(sig)
        loop.add_signal_handler(sig, stopped.set)
    tasks = [asyncio.create_task(internal.serve()), asyncio.create_task(public.serve())]
    stop_task = asyncio.create_task(stopped.wait())
    try:
        done, pending = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        public.should_exit = internal.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        for sig, handler in old_sigs.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', help='Persistent data directory; defaults to WEBAGENT_DATA_DIR.')
    args = parser.parse_args(argv)
    try:
        config = HostedConfig.from_environment()
        asyncio.run(serve(config, data_dir=args.data_dir))
    except (ValueError, OSError) as exc:
        # Keep environment values out of errors.
        print(json.dumps({'status': 'error', 'message': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
