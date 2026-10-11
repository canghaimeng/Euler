# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Local provider RPC transport. Raw history/reasoning is never logged or served."""
from __future__ import annotations
import json
import queue
import subprocess
import threading
import time
import os
import signal


class RpcError(RuntimeError):
    pass


class SessionRpc:
    def __init__(self, command, *, cwd, env, callback=None, preexec_fn=None):
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True, preexec_fn=preexec_fn)
        self.pending = {}; self.counter = 0; self.lock = threading.Lock()
        self.counts = {}
        self.callback = callback or (lambda event: None)
        self.thread = threading.Thread(target=self._read, daemon=True); self.thread.start()
        # Drain provider diagnostics without storing or publishing them. A pipe
        # also gives CLIs a normal stream descriptor under filesystem isolation.
        self.stderr_thread = threading.Thread(target=self._discard_stderr, daemon=True)
        self.stderr_thread.start()

    def _discard_stderr(self):
        for _ in self.process.stderr:
            pass

    def _send(self, message):
        with self.lock:
            self.process.stdin.write(json.dumps(message,ensure_ascii=False)+'\n'); self.process.stdin.flush()

    def _read(self):
        try:
            for line in self.process.stdout:
                try: message=json.loads(line)
                except ValueError: continue
                if 'method' in message:
                    method=message['method'];self.counts[method]=self.counts.get(method,0)+1
                    if 'id' in message:
                        # Unattended restoration never escalates an approval request.
                        if message['method']=='session/request_permission':
                            self._send({'jsonrpc':'2.0','id':message['id'],'result':{'outcome':{'outcome':'cancelled'}}})
                        else:
                            self._send({'id':message['id'],'error':{'code':-32601,'message':'Not authorized by Euler team host'}})
                    else:
                        self.callback(message)
                elif message.get('id') in self.pending:
                    self.pending[message['id']].put(message)
        finally:
            for future in list(self.pending.values()):future.put({'error':{'code':'transport_closed'}})

    def request(self, method, params=None, timeout=45):
        self.counter+=1; ident=self.counter; future=queue.Queue();self.pending[ident]=future
        try:
            self._send({'jsonrpc':'2.0','id':ident,'method':method,'params':params or {}})
            result=future.get(timeout=timeout)
            if 'error' in result:
                # Provider error text may contain a private transcript fragment.
                error=RpcError(method+': '+str(result['error'].get('code','unknown')))
                error.diagnostic=result['error'].get('message','')
                raise error
            return result.get('result',{})
        except queue.Empty:raise RpcError(method+': timeout') from None
        finally:self.pending.pop(ident,None)

    def notify(self,method,params=None):self._send({'jsonrpc':'2.0','method':method,'params':params or {}})

    def close(self):
        # Provider MCP children share this dedicated process group.
        try:os.killpg(self.process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        if self.process.poll() is None:
            try:self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:os.killpg(self.process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                self.process.wait()
        self.thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
        self.process.stdout.close();self.process.stderr.close()

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
