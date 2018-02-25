"""
This module provides WSGI application to serve the Home Assistant API.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/http/
"""
import asyncio
from ipaddress import ip_network
import json
import logging
import os
import ssl

from aiohttp import web
from aiohttp.web_exceptions import HTTPUnauthorized, HTTPMovedPermanently
import voluptuous as vol

from homeassistant.const import (
    SERVER_PORT, CONTENT_TYPE_JSON,
    EVENT_HOMEASSISTANT_STOP, EVENT_HOMEASSISTANT_START,)
from homeassistant.core import is_callback
import homeassistant.helpers.config_validation as cv
import homeassistant.remote as rem
import homeassistant.util as hass_util
from homeassistant.util.logging import HideSensitiveDataFilter

from .auth import setup_auth
from .ban import setup_bans
from .cors import setup_cors
from .real_ip import setup_real_ip
from .const import KEY_AUTHENTICATED, KEY_REAL_IP
from .static import (
    CachingFileResponse, CachingStaticResource, staticresource_middleware)

REQUIREMENTS = ['aiohttp_cors==0.6.0']

DOMAIN = 'http'

CONF_API_PASSWORD = 'api_password'
CONF_SERVER_HOST = 'server_host'
CONF_SERVER_PORT = 'server_port'
CONF_BASE_URL = 'base_url'
CONF_SSL_CERTIFICATE = 'ssl_certificate'
CONF_SSL_KEY = 'ssl_key'
CONF_CORS_ORIGINS = 'cors_allowed_origins'
CONF_USE_X_FORWARDED_FOR = 'use_x_forwarded_for'
CONF_TRUSTED_NETWORKS = 'trusted_networks'
CONF_LOGIN_ATTEMPTS_THRESHOLD = 'login_attempts_threshold'
CONF_IP_BAN_ENABLED = 'ip_ban_enabled'

# TLS configuration follows the best-practice guidelines specified here:
# https://wiki.mozilla.org/Security/Server_Side_TLS
# Intermediate guidelines are followed.
SSL_VERSION = ssl.PROTOCOL_SSLv23
SSL_OPTS = ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3
if hasattr(ssl, 'OP_NO_COMPRESSION'):
    SSL_OPTS |= ssl.OP_NO_COMPRESSION
CIPHERS = "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:" \
          "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:" \
          "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:" \
          "DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:" \
          "ECDHE-ECDSA-AES128-SHA256:ECDHE-RSA-AES128-SHA256:" \
          "ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES256-SHA384:" \
          "ECDHE-RSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA384:" \
          "ECDHE-ECDSA-AES256-SHA:ECDHE-RSA-AES256-SHA:" \
          "DHE-RSA-AES128-SHA256:DHE-RSA-AES128-SHA:DHE-RSA-AES256-SHA256:" \
          "DHE-RSA-AES256-SHA:ECDHE-ECDSA-DES-CBC3-SHA:" \
          "ECDHE-RSA-DES-CBC3-SHA:EDH-RSA-DES-CBC3-SHA:AES128-GCM-SHA256:" \
          "AES256-GCM-SHA384:AES128-SHA256:AES256-SHA256:AES128-SHA:" \
          "AES256-SHA:DES-CBC3-SHA:!DSS"

_LOGGER = logging.getLogger(__name__)

DEFAULT_SERVER_HOST = '0.0.0.0'
DEFAULT_DEVELOPMENT = '0'
NO_LOGIN_ATTEMPT_THRESHOLD = -1

HTTP_SCHEMA = vol.Schema({
    vol.Optional(CONF_API_PASSWORD): cv.string,
    vol.Optional(CONF_SERVER_HOST, default=DEFAULT_SERVER_HOST): cv.string,
    vol.Optional(CONF_SERVER_PORT, default=SERVER_PORT): cv.port,
    vol.Optional(CONF_BASE_URL): cv.string,
    vol.Optional(CONF_SSL_CERTIFICATE): cv.isfile,
    vol.Optional(CONF_SSL_KEY): cv.isfile,
    vol.Optional(CONF_CORS_ORIGINS, default=[]):
        vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_USE_X_FORWARDED_FOR, default=False): cv.boolean,
    vol.Optional(CONF_TRUSTED_NETWORKS, default=[]):
        vol.All(cv.ensure_list, [ip_network]),
    vol.Optional(CONF_LOGIN_ATTEMPTS_THRESHOLD,
                 default=NO_LOGIN_ATTEMPT_THRESHOLD):
        vol.Any(cv.positive_int, NO_LOGIN_ATTEMPT_THRESHOLD),
    vol.Optional(CONF_IP_BAN_ENABLED, default=True): cv.boolean
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: HTTP_SCHEMA,
}, extra=vol.ALLOW_EXTRA)


@asyncio.coroutine
def async_setup(hass, config):
    """Set up the HTTP API and debug interface."""
    conf = config.get(DOMAIN)

    if conf is None:
        conf = HTTP_SCHEMA({})

    api_password = conf.get(CONF_API_PASSWORD)
    server_host = conf[CONF_SERVER_HOST]
    server_port = conf[CONF_SERVER_PORT]
    ssl_certificate = conf.get(CONF_SSL_CERTIFICATE)
    ssl_key = conf.get(CONF_SSL_KEY)
    cors_origins = conf[CONF_CORS_ORIGINS]
    use_x_forwarded_for = conf[CONF_USE_X_FORWARDED_FOR]
    trusted_networks = conf[CONF_TRUSTED_NETWORKS]
    is_ban_enabled = conf[CONF_IP_BAN_ENABLED]
    login_threshold = conf[CONF_LOGIN_ATTEMPTS_THRESHOLD]

    if api_password is not None:
        logging.getLogger('aiohttp.access').addFilter(
            HideSensitiveDataFilter(api_password))

    server = HomeAssistantHTTP(
        hass,
        server_host=server_host,
        server_port=server_port,
        api_password=api_password,
        ssl_certificate=ssl_certificate,
        ssl_key=ssl_key,
        cors_origins=cors_origins,
        use_x_forwarded_for=use_x_forwarded_for,
        trusted_networks=trusted_networks,
        login_threshold=login_threshold,
        is_ban_enabled=is_ban_enabled
    )

    @asyncio.coroutine
    def stop_server(event):
        """Stop the server."""
        yield from server.stop()

    @asyncio.coroutine
    def start_server(event):
        """Start the server."""
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_server)
        yield from server.start()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, start_server)

    hass.http = server

    host = conf.get(CONF_BASE_URL)

    if host:
        port = None
    elif server_host != DEFAULT_SERVER_HOST:
        host = server_host
        port = server_port
    else:
        host = hass_util.get_local_ip()
        port = server_port

    hass.config.api = rem.API(host, api_password, port,
                              ssl_certificate is not None)

    return True


class HomeAssistantHTTP(object):
    """HTTP server for Home Assistant."""

    def __init__(self, hass, api_password, ssl_certificate,
                 ssl_key, server_host, server_port, cors_origins,
                 use_x_forwarded_for, trusted_networks,
                 login_threshold, is_ban_enabled):
        """Initialize the HTTP Home Assistant server."""
        app = self.app = web.Application(
            middlewares=[staticresource_middleware])

        # This order matters
        setup_real_ip(app, use_x_forwarded_for)

        if is_ban_enabled:
            setup_bans(hass, app, login_threshold)

        setup_auth(app, trusted_networks, api_password)

        if cors_origins:
            setup_cors(app, cors_origins)

        app['hass'] = hass

        self.hass = hass
        self.api_password = api_password
        self.ssl_certificate = ssl_certificate
        self.ssl_key = ssl_key
        self.server_host = server_host
        self.server_port = server_port
        self.is_ban_enabled = is_ban_enabled
        self._handler = None
        self.server = None

    def register_view(self, view):
        """Register a view with the WSGI server.

        The view argument must be a class that inherits from HomeAssistantView.
        It is optional to instantiate it before registering; this method will
        handle it either way.
        """
        if isinstance(view, type):
            # Instantiate the view, if needed
            view = view()

        if not hasattr(view, 'url'):
            class_name = view.__class__.__name__
            raise AttributeError(
                '{0} missing required attribute "url"'.format(class_name)
            )

        if not hasattr(view, 'name'):
            class_name = view.__class__.__name__
            raise AttributeError(
                '{0} missing required attribute "name"'.format(class_name)
            )

        view.register(self.app.router)

    def register_redirect(self, url, redirect_to):
        """Register a redirect with the server.

        If given this must be either a string or callable. In case of a
        callable it's called with the url adapter that triggered the match and
        the values of the URL as keyword arguments and has to return the target
        for the redirect, otherwise it has to be a string with placeholders in
        rule syntax.
        """
        def redirect(request):
            """Redirect to location."""
            raise HTTPMovedPermanently(redirect_to)

        self.app.router.add_route('GET', url, redirect)

    def register_static_path(self, url_path, path, cache_headers=True):
        """Register a folder or file to serve as a static path."""
        if os.path.isdir(path):
            if cache_headers:
                resource = CachingStaticResource
            else:
                resource = web.StaticResource
            self.app.router.register_resource(resource(url_path, path))
            return

        if cache_headers:
            @asyncio.coroutine
            def serve_file(request):
                """Serve file from disk."""
                return CachingFileResponse(path)
        else:
            @asyncio.coroutine
            def serve_file(request):
                """Serve file from disk."""
                return web.FileResponse(path)

        # aiohttp supports regex matching for variables. Using that as temp
        # to work around cache busting MD5.
        # Turns something like /static/dev-panel.html into
        # /static/{filename:dev-panel(-[a-z0-9]{32}|)\.html}
        base, ext = os.path.splitext(url_path)
        if ext:
            base, file = base.rsplit('/', 1)
            regex = r"{}(-[a-z0-9]{{32}}|){}".format(file, ext)
            url_pattern = "{}/{{filename:{}}}".format(base, regex)
        else:
            url_pattern = url_path

        self.app.router.add_route('GET', url_pattern, serve_file)

    @asyncio.coroutine
    def start(self):
        """Start the WSGI server."""
        yield from self.app.startup()

        if self.ssl_certificate:
            try:
                context = ssl.SSLContext(SSL_VERSION)
                context.options |= SSL_OPTS
                context.set_ciphers(CIPHERS)
                context.load_cert_chain(self.ssl_certificate, self.ssl_key)
            except OSError as error:
                _LOGGER.error("Could not read SSL certificate from %s: %s",
                              self.ssl_certificate, error)
                context = None
                return
        else:
            context = None

        # Aiohttp freezes apps after start so that no changes can be made.
        # However in Home Assistant components can be discovered after boot.
        # This will now raise a RunTimeError.
        # To work around this we now fake that we are frozen.
        # A more appropriate fix would be to create a new app and
        # re-register all redirects, views, static paths.
        self.app._frozen = True  # pylint: disable=protected-access

        self._handler = self.app.make_handler(loop=self.hass.loop)

        try:
            self.server = yield from self.hass.loop.create_server(
                self._handler, self.server_host, self.server_port, ssl=context)
        except OSError as error:
            _LOGGER.error("Failed to create HTTP server at port %d: %s",
                          self.server_port, error)

        # pylint: disable=protected-access
        self.app._middlewares = tuple(self.app._prepare_middleware())
        self.app._frozen = False

    @asyncio.coroutine
    def stop(self):
        """Stop the WSGI server."""
        if self.server:
            self.server.close()
            yield from self.server.wait_closed()
        yield from self.app.shutdown()
        if self._handler:
            yield from self._handler.shutdown(10)
        yield from self.app.cleanup()


class HomeAssistantView(object):
    """Base view for all views."""

    url = None
    extra_urls = []
    requires_auth = True  # Views inheriting from this class can override this

    # pylint: disable=no-self-use
    def json(self, result, status_code=200, headers=None):
        """Return a JSON response."""
        msg = json.dumps(
            result, sort_keys=True, cls=rem.JSONEncoder).encode('UTF-8')
        response = web.Response(
            body=msg, content_type=CONTENT_TYPE_JSON, status=status_code,
            headers=headers)
        response.enable_compression()
        return response

    def json_message(self, message, status_code=200, message_code=None,
                     headers=None):
        """Return a JSON message response."""
        data = {'message': message}
        if message_code is not None:
            data['code'] = message_code
        return self.json(data, status_code, headers=headers)

    @asyncio.coroutine
    # pylint: disable=no-self-use
    def file(self, request, fil):
        """Return a file."""
        assert isinstance(fil, str), 'only string paths allowed'
        return web.FileResponse(fil)

    def register(self, router):
        """Register the view with a router."""
        assert self.url is not None, 'No url set for view'
        urls = [self.url] + self.extra_urls

        for method in ('get', 'post', 'delete', 'put'):
            handler = getattr(self, method, None)

            if not handler:
                continue

            handler = request_handler_factory(self, handler)

            for url in urls:
                router.add_route(method, url, handler)

        # aiohttp_cors does not work with class based views
        # self.app.router.add_route('*', self.url, self, name=self.name)

        # for url in self.extra_urls:
        #     self.app.router.add_route('*', url, self)


def request_handler_factory(view, handler):
    """Wrap the handler classes."""
    assert asyncio.iscoroutinefunction(handler) or is_callback(handler), \
        "Handler should be a coroutine or a callback."

    @asyncio.coroutine
    def handle(request):
        """Handle incoming request."""
        if not request.app['hass'].is_running:
            return web.Response(status=503)

        authenticated = request.get(KEY_AUTHENTICATED, False)

        if view.requires_auth and not authenticated:
            raise HTTPUnauthorized()

        _LOGGER.info('Serving %s to %s (auth: %s)',
                     request.path, request.get(KEY_REAL_IP), authenticated)

        result = handler(request, **request.match_info)

        if asyncio.iscoroutine(result):
            result = yield from result

        if isinstance(result, web.StreamResponse):
            # The method handler returned a ready-made Response, how nice of it
            return result

        status_code = 200

        if isinstance(result, tuple):
            result, status_code = result

        if isinstance(result, str):
            result = result.encode('utf-8')
        elif result is None:
            result = b''
        elif not isinstance(result, bytes):
            assert False, ('Result should be None, string, bytes or Response. '
                           'Got: {}').format(result)

        return web.Response(body=result, status=status_code)

    return handle
