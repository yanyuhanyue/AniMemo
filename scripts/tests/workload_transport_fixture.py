"""Inspect a generated fixed transport without running its privileged body."""
import ast
import base64
import shlex
import zlib

from scripts.candidate_guest_session import MAX_REMOTE_PROGRAM_BYTES


def decode_workload_transport(command):
    wrapper = shlex.split(command)[-1]
    compile(wrapper, '<fixed-transport-test>', 'exec')
    values = [ast.literal_eval(node.args[0]) for node in ast.walk(ast.parse(wrapper))
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == 'b64decode']
    if len(values) != 1 or type(values[0]) is not str or len(values[0]) > MAX_REMOTE_PROGRAM_BYTES * 2:
        raise ValueError('test transport is not the bounded fixed wrapper')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(base64.b64decode(values[0], validate=True), MAX_REMOTE_PROGRAM_BYTES + 1)
    if len(raw) > MAX_REMOTE_PROGRAM_BYTES or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise ValueError('test transport exceeds its decoded bound')
    return raw.decode('utf-8')
