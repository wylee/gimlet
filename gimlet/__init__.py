import os
import time
import itertools
import cPickle as pickle

from struct import Struct
from datetime import datetime
from collections import MutableMapping

from webob import Request
from itsdangerous import Serializer, URLSafeSerializerMixin


class Session(MutableMapping):

    def __init__(self, channels):
        self.insecure = channels['insecure']
        self.secure_nonperm = channels['secure_nonperm']
        self.secure_perm = channels['secure_perm']

    @property
    def id(self):
        return self.insecure.id

    @property
    def created_timestamp(self):
        return self.insecure.created_timestamp

    @property
    def created_time(self):
        return self.insecure.created_time

    def __getitem__(self, key):
        for channel in [self.insecure, self.secure_nonperm, self.secure_perm]:
            if key in channel:
                return channel.get(key)
        raise KeyError

    def get(self, key, secure=False, permanent=False, clientside=False):
        channel = self.insecure
        if secure:
            if permanent:
                channel = self.secure_perm
            else:
                channel = self.secure_nonperm
        return channel.get(key, clientside=clientside)

    def __setitem__(self, key, val):
        return self.set(key, val)

    def set(self, key, val, secure=False, permanent=False, clientside=False):
        if key in self:
            del self[key]
        channel = self.insecure
        if secure:
            if permanent:
                channel = self.secure_perm
            else:
                channel = self.secure_nonperm
        channel.set(key, val, clientside=clientside)

    def __delitem__(self, key):
        if key not in self:
            raise KeyError
        for channel in [self.insecure, self.secure_nonperm, self.secure_perm]:
            if key in channel:
                channel.delete(key)

    def __contains__(self, key):
        return any((key in channel) for channel in
                   [self.insecure, self.secure_nonperm, self.secure_perm])

    def __iter__(self):
        return itertools.chain(iter(self.insecure),
                               iter(self.secure_nonperm),
                               iter(self.secure_perm))

    def __len__(self):
        return (len(self.insecure) +
                len(self.secure_nonperm) +
                len(self.secure_perm))

    def is_permanent(self, key):
        return (key in self.secure_perm) or (key in self.insecure)

    def is_secure(self, key):
        return (key in self.secure_nonperm) or (key in self.secure_perm)


class SessionChannel(object):

    def __init__(self, id, created_timestamp, backend, fresh,
                 client_data=None):
        self.dirty_keys = set()
        self.id = id
        self.created_timestamp = created_timestamp
        self.backend = backend
        self.fresh = fresh

        self.client_data = client_data or {}
        self.client_dirty = False

        self.backend_data = {}
        self.backend_dirty = False
        self.backend_loaded = False

    def backend_read(self):
        if not self.backend_loaded:
            try:
                self.backend_data = self.backend[self.id]
            except KeyError:
                self.backend_data = {}
            self.backend_loaded = True

    def backend_write(self):
        self.backend[self.id] = self.backend_data

    @property
    def created_time(self):
        return datetime.utcfromtimestamp(self.created_timestamp)

    def __iter__(self):
        self.backend_read()
        return itertools.chain(iter(self.client_data), iter(self.backend_data))

    def __len__(self):
        self.backend_read()
        return len(self.backend_data) + len(self.client_data)

    def get(self, key, clientside=None):
        if ((clientside is None) and (key in self.client_data)) or clientside:
            return self.client_data[key]
        else:
            self.backend_read()
            return self.backend_data[key]

    def set(self, key, value, clientside=None):
        if clientside:
            self.client_data[key] = value
            self.client_dirty = True
        else:
            self.backend_data[key] = value
            self.backend_dirty = True

    def delete(self, key):
        if key in self.client_data:
            del self.client_data[key]
            self.client_dirty = True
        else:
            self.backend_read()
            del self.backend_data[key]
            self.backend_dirty = True


class CookieSerializer(Serializer):
    packer = Struct('16si')

    def __init__(self, secret, backend):
        Serializer.__init__(self, secret)
        self.backend = backend

    def load_payload(self, payload):
        """
        Convert a cookie into a SessionChannel instance.
        """
        raw_id, created_timestamp = \
            self.packer.unpack(payload[:self.packer.size])
        client_data_pkl = payload[self.packer.size:]

        id = raw_id.encode('hex')
        client_data = pickle.loads(client_data_pkl)
        return SessionChannel(id, created_timestamp, self.backend,
                              fresh=False, client_data=client_data)

    def dump_payload(self, channel):
        """
        Convert a Session instance into a cookie by packing it precisely into a
        string.
        """
        client_data_pkl = pickle.dumps(channel.client_data)
        raw_id = channel.id.decode('hex')
        return (self.packer.pack(raw_id, channel.created_timestamp) +
                client_data_pkl)


class URLSafeCookieSerializer(URLSafeSerializerMixin, CookieSerializer):
    pass


class SessionMiddleware(object):
    def __init__(self, app, secret, backend,
                 cookie_name='gimlet', environ_key='gimlet.session'):
        self.app = app
        self.backend = backend

        self.cookie_name = cookie_name
        self.environ_key = environ_key

        self.serializer = URLSafeCookieSerializer(secret, backend)

        self.channel_names = {
            'insecure': self.cookie_name,
            'secure_perm': self.cookie_name + '-sp',
            'secure_nonperm': self.cookie_name + '-sn'
        }

        self.channel_opts = {
            'insecure': {},
            'secure_perm': dict(secure=True),
            'secure_nonperm': dict(secure=True, max_age=0)
        }

    def make_session_id(self):
        return os.urandom(16).encode('hex')

    def new_session_channel(self):
        id = self.make_session_id()
        return SessionChannel(id, int(time.time()), self.backend, fresh=True)

    def read_channel(self, req, key):
        name = self.channel_names[key]
        if name in req.cookies:
            sc = self.serializer.loads(req.cookies[name])
        else:
            sc = self.new_session_channel()
        return sc

    def write_channel(self, resp, key, channel):
        name = self.channel_names[key]

        # Set a cookie IFF the following conditions:
        # - data has been changed on the client
        # OR
        # - the cookie is fresh
        if channel.client_dirty or channel.fresh:
            resp.set_cookie(name, self.serializer.dumps(channel),
                            httponly=True, **self.channel_opts[key])

        # Write to the backend IFF the following conditions:
        # - data has been changed on the backend
        if channel.backend_dirty:
            channel.backend_write()

    def __call__(self, environ, start_response):
        req = Request(environ)

        channels = {}
        for key in self.channel_names:
            channels[key] = self.read_channel(req, key)

        req.environ[self.environ_key] = Session(channels)

        resp = req.get_response(self.app)

        for key in self.channel_names:
            self.write_channel(resp, key, channels[key])

        return resp(environ, start_response)
