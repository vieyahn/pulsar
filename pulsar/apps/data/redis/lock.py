import uuid
import threading
from asyncio import sleep

from pulsar import is_async


class LockError(Exception):
    pass


class RedisScript(object):
    '''An executable Lua script object
    '''
    def __init__(self, script):
        self.script = script
        self.sha = None

    def __call__(self, client, keys=None, args=None):
        '''Execute the script, passing any required ``args``
        '''
        if self.sha not in client.store.loaded_scripts:
            sha = yield from client.immediate_execute('SCRIPT', 'LOAD',
                                                      self.script)
            self.sha = sha.decode('utf-8')
            client.store.loaded_scripts.add(self.sha)

        result = client.evalsha(self.sha, keys, args)
        if is_async(result):
            result = yield from result
        return result


class Lock(object):
    '''Asynchronous locking primitive for distributing computing

    A primitive lock is in one of two states, 'locked' or 'unlocked'.
    It is created in the unlocked state. It has two basic methods,
    :meth:`.acquire` and :meth:`.release. When the state is unlocked,
    :meth:`.acquire` changes the state to locked and returns immediately.

    When the state is locked, :meth:`.acquire` wait
    until a call to :meth:`.release` changes it to unlocked,
    then the :meth:`.acquire` call resets it to locked and returns.
    '''
    def __init__(self, client, name, timeout=None, blocking=0, sleep=0.2):
        self._local = threading.local()
        self._local.token = None
        self.client = client
        self.name = name
        self.timeout = timeout
        self.blocking = blocking
        self.sleep = sleep
        if self.timeout and self.sleep > self.timeout:
            raise LockError("'sleep' must be less than 'timeout'")

    @property
    def _loop(self):
        return self.client._loop

    @property
    def token(self):
        ''''Return the token that acquire the lock or None.
        '''
        return self._local.token

    def acquire(self):
        token = uuid.uuid1().hex.encode('utf-8')
        timeout = self.timeout and int(self.timeout * 1000) or ''
        acquired = yield from self.lua_acquire(self.client,
                                               keys=[self.name],
                                               args=[token, timeout])
        if not acquired:
            loop = self._loop
            start = loop.time()
            while 1:
                if (self.blocking is not None and
                        loop.time() - start >= self.blocking):
                    return False
                yield from sleep(self.sleep)
        else:
            self._local.token = token
            return True

    def release(self):
        expected_token = self.token
        if not expected_token:
            raise LockError("Cannot release an unlocked lock")
        self._local.token = None
        released = yield from self.lua_release(self.client,
                                               keys=[self.name],
                                               args=[expected_token])
        if not released:
            raise LockError("Cannot release a lock that's no longer owned")
        return True

    lua_acquire = RedisScript("""
        if redis.call('setnx', KEYS[1], ARGV[1]) == 1 then
            if ARGV[2] ~= '' then
                redis.call('pexpire', KEYS[1], ARGV[2])
            end
            return 1
        end
        return 0
    """)

    # KEYS[1] - lock name
    # ARGS[1] - token
    # return 1 if the lock was released, otherwise 0
    lua_release = RedisScript("""
        local token = redis.call('get', KEYS[1])
        if not token or token ~= ARGV[1] then
            return 0
        end
        redis.call('del', KEYS[1])
        return 1
    """)
