#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# AI 白板 · MCP 服务器 —— 把画板能力暴露给任意 MCP 客户端
# 作者：ccjianxing ｜ https://github.com/ccjianxing/ai-whiteboard ｜ MIT License
"""AI 白板 · MCP 服务器（stdio）

把白板的能力暴露给任意 MCP 客户端（DSH / Claude Code / Cursor / …），
**由外部 agent 来驱动画板**，白板本身不必内置模型。

调用链：
    MCP 客户端 ──stdio JSON-RPC──> 本进程 ──HTTP──> 白板服务端 /api/agent/call
        ──> 浏览器里打开的 board.html 长轮询取任务 ──> execTool 执行 ──> 回传结果

工具清单**直接从服务端的 WB_TOOLS 取**（本地有 server_v2.py 就解析它，
没有就向 /api/tools 要），只维护一份定义，避免"实现了却忘了告诉 AI"。

用法（MCP 客户端配置里）：
    command: python
    args: ["/absolute/path/to/mcp_server.py"]
环境变量：
    WB_SERVER  白板服务地址，默认 http://127.0.0.1:9091
"""
import ast
import io
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get('WB_SERVER', 'http://127.0.0.1:9091')
PROTOCOL = '2024-11-05'
LANG_EN = (os.environ.get('WB_LANG') or '').lower().startswith('en')   # WB_LANG=en → 工具说明用英文
BOARD = (os.environ.get('WB_BOARD') or '').strip()[:64]   # 驻扎哪块板（留空=默认板）


def token():
    try:
        with io.open(os.path.join(HERE, 'wb_token.txt'), encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def _http_post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    tk = token()
    if tk:
        req.add_header('X-WB-Token', tk)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _http_get(path):
    req = urllib.request.Request(BASE + path)
    tk = token()
    if tk:
        req.add_header('X-WB-Token', tk)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def load_tools():
    """解析 server_v2.py 里的 WB_TOOLS（用 ast，不 import，避免任何副作用污染 stdout）。"""
    try:
        src = io.open(os.path.join(HERE, 'server_v2.py'), encoding='utf-8').read()
    except Exception as e:
        print('[mcp] 读不到 server_v2.py: %s' % e, file=sys.stderr)
        return []
    i = src.find('WB_TOOLS = [')
    if i < 0:
        return []
    j = src.find('\n]', i)
    if j < 0:
        return []
    try:
        return ast.literal_eval(src[i + len('WB_TOOLS = '):j + 2])
    except Exception as e:
        print('[mcp] 解析 WB_TOOLS 失败: %s' % e, file=sys.stderr)
        return []


def fetch_tools():
    """本地解析不到就向服务端要 —— 这样 agent 那台机器上只要 mcp_server.py + wb_token.txt，
    不必再放一份 server_v2.py（否则工具清单为空，agent 只剩对话工具，画板功能全用不了）。"""
    try:
        r = _http_get('/api/tools' + ('?lang=en' if LANG_EN else ''))
        return r.get('tools') or []
    except Exception:
        return []


def translate(tools):
    """本地解析出来的是中文说明；要英文就用同一份词典换掉（agent 挑工具靠它，值得翻）。

    词典在 server_v2.py 旁边（工具英文说明.py）。不在也没关系 —— 回落中文，不影响使用。
    """
    if not LANG_EN or not tools:
        return tools
    d = {}
    try:
        src = io.open(os.path.join(HERE, '工具英文说明.py'), encoding='utf-8').read()
        i = src.find('TOOL_DESC_EN = {')
        j = src.find('\n}', i)
        if i >= 0 and j >= 0:
            d = ast.literal_eval(src[i + len('TOOL_DESC_EN = '):j + 2])
    except Exception:
        d = {}
    if not d:
        return tools
    out = []
    for t in tools:
        fn = (t.get('function') or {})
        nm = fn.get('name') or ''
        t2 = json.loads(json.dumps(t))          # 深拷贝，别改到原对象
        if d.get(nm):
            t2['function']['description'] = d[nm]
        out.append(t2)
    return out


TOOLS = translate(load_tools()) or fetch_tools()
if not TOOLS:
    print('[mcp] 警告：没能拿到工具清单（本地没有 server_v2.py，也连不上服务端）——'
          'agent 将只能使用对话工具', file=sys.stderr)
# MCP 原生工具：共享对话。它们不需要浏览器（对话存在服务端），
# 这样远程 agent 即使没人开着白板，也能把话留下、把白板上的讨论读走。
CHAT_TOOLS = [
    {'name': 'board_chat_say',
     'description': '对白板说一句话（会出现在白板的对话面板里，用户能直接看到）。'
                    '做完了什么、准备做什么、需要用户确认什么，都用它说。',
     'inputSchema': {'type': 'object', 'properties': {
         'text': {'type': 'string', 'description': '要说的话'}},
         'required': ['text']}},
    {'name': 'board_chat_wait',
     'description': '等用户在白板上说话（阻塞最多 timeout 秒，有消息立刻返回）。'
                    '用户跟你对话时用它"听"——比反复 read 省事，也不会漏消息。',
     'inputSchema': {'type': 'object', 'properties': {
         'since': {'type': 'integer', 'description': '只等这个序号之后的消息，默认用上次读到的位置'},
         'timeout': {'type': 'integer', 'description': '最多等多少秒，默认 50，最大 120'}},
         'required': []}},
    {'name': 'board_chat_read',
     'description': '读取白板上的对话记录（用户说的 + 其它 agent 说的），用来了解上下文。',
     'inputSchema': {'type': 'object', 'properties': {
         'limit': {'type': 'integer', 'description': '最多读多少条，默认 40'}},
         'required': []}},
]

TOOL_LIST = [{'name': t['function']['name'],
              'description': t['function'].get('description', ''),
              'inputSchema': t['function'].get('parameters') or {'type': 'object', 'properties': {}}}
             for t in TOOLS]


CHAT_TOOLS_EN = [
    {'name': 'board_chat_say',
     'description': 'Say something on the board (it shows up in the board conversation panel, '
                    'so the user sees it immediately). Use it to report what you did, what you are about to do, '
                    'or what you need the user to confirm.',
     'inputSchema': {'type': 'object', 'properties': {
         'text': {'type': 'string', 'description': 'what to say'}}, 'required': ['text']}},
    {'name': 'board_chat_read',
     'description': 'Read the board conversation (what the user and other agents said).',
     'inputSchema': {'type': 'object', 'properties': {
         'limit': {'type': 'integer', 'description': 'how many recent messages (default 40)'}}}},
    {'name': 'board_chat_wait',
     'description': 'Wait for the user to speak (blocks up to timeout seconds, returns at once on a message).',
     'inputSchema': {'type': 'object', 'properties': {
         'since': {'type': 'integer', 'description': 'last sequence number you saw (0 to start)'},
         'timeout': {'type': 'integer', 'description': 'how long to wait, max 120s'}}}},
]


def call_board(tool, args, timeout=180):
    """把工具调用交给白板服务端，由浏览器执行后带回结果。"""
    body = json.dumps({'tool': tool, 'args': args or {}, 'board': BOARD}).encode('utf-8')
    req = urllib.request.Request(BASE + '/api/agent/call', data=body,
                                 headers={'Content-Type': 'application/json'})
    tk = token()
    if tk:
        req.add_header('X-WB-Token', tk)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except Exception:
            return {'ok': False, 'error': 'HTTP %s' % e.code}
    except Exception as e:
        return {'ok': False, 'error': '连不上白板服务（%s）：%s' % (BASE, e)}


_LAST_SEQ = [0]          # agent 读到哪了


def hello():
    """向白板报到：用户能在面板上看到"哪个 agent 接进来了"。"""
    try:
        _http_post('/api/agent/hello', {'name': os.environ.get('WB_AGENT_NAME') or 'agent',
                                        'ver': 'mcp/1.0', 'board': BOARD})
    except Exception:
        pass


def send(obj):
    """MCP stdio：一行一个 JSON，stdout 必须干净。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def reply(mid, result=None, error=None):
    msg = {'jsonrpc': '2.0', 'id': mid}
    if error is not None:
        msg['error'] = error
    else:
        msg['result'] = result
    send(msg)


def handle(msg):
    method = msg.get('method')
    mid = msg.get('id')

    if method == 'initialize':
        hello()
        reply(mid, {'protocolVersion': msg.get('params', {}).get('protocolVersion') or PROTOCOL,
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'ai-whiteboard', 'version': '1.0.0'}})
        return
    if method in ('notifications/initialized', 'initialized'):
        return                                    # 通知：不需要回
    if method == 'ping':
        reply(mid, {})
        return
    if method == 'tools/list':
        reply(mid, {'tools': TOOL_LIST + (CHAT_TOOLS_EN if LANG_EN else CHAT_TOOLS)})
        return
    if method == 'tools/call':
        p = msg.get('params') or {}
        name = p.get('name') or ''
        args = p.get('arguments') or {}
        if name == 'board_chat_say':
            text = str(args.get('text') or '').strip()
            if not text:
                reply(mid, {'content': [{'type': 'text', 'text': '缺少 text'}], 'isError': True})
                return
            r = _http_post('/api/chat/push', {'who': os.environ.get('WB_AGENT_NAME') or 'agent',
                                              'text': text, 'src': 'mcp', 'board': BOARD})
            if r.get('ok'):
                reply(mid, {'content': [{'type': 'text', 'text': '已发到白板对话：%s' % text[:60]}], 'isError': False})
            else:
                reply(mid, {'content': [{'type': 'text', 'text': '发送失败：%s' % r.get('error')}], 'isError': True})
            return
        if name == 'board_chat_wait':
            try:
                since = int(args.get('since') or _LAST_SEQ[0])
                tmo = int(args.get('timeout') or 50)
            except Exception:
                since, tmo = _LAST_SEQ[0], 50
            r = _http_post('/api/chat/wait', {'since': since, 'timeout': tmo, 'board': BOARD})
            msgs = r.get('messages') or []
            if msgs:
                _LAST_SEQ[0] = max(_LAST_SEQ[0], msgs[-1].get('seq') or 0)
            lines = ['[%s] %s' % (m.get('who'), m.get('text')) for m in msgs]
            reply(mid, {'content': [{'type': 'text',
                                     'text': '\n'.join(lines) if lines else '（这段时间没人说话）'}],
                        'isError': False})
            return
        if name == 'board_chat_read':
            try:
                lim = int(args.get('limit') or 40)
            except Exception:
                lim = 40
            r = _http_get('/api/chat/pull?since=0&board=' + BOARD)
            allmsgs = r.get('messages') or []
            if allmsgs:
                _LAST_SEQ[0] = max(_LAST_SEQ[0], allmsgs[-1].get('seq') or 0)
            msgs = allmsgs[-max(1, min(200, lim)):]
            lines = ['[%s] %s' % (m.get('who'), m.get('text')) for m in msgs]
            reply(mid, {'content': [{'type': 'text', 'text': '\n'.join(lines) or '（还没有对话）'}],
                        'isError': False})
            return
        if not any(t['name'] == name for t in TOOL_LIST):
            reply(mid, {'content': [{'type': 'text', 'text': '没有这个工具：%s' % name}], 'isError': True})
            return
        hello()                      # 心跳：让白板知道 agent 还在
        res = call_board(name, args)
        if not res.get('ok'):
            txt = '调用失败：%s' % (res.get('error') or res)
            reply(mid, {'content': [{'type': 'text', 'text': txt}], 'isError': True})
            return
        out = res.get('result')
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        reply(mid, {'content': [{'type': 'text', 'text': text}], 'isError': False})
        return
    if mid is not None:
        reply(mid, error={'code': -32601, 'message': 'method not found: %s' % method})


def main():
    print('[mcp] AI 白板 MCP 服务器已启动：%d 个工具，服务端 %s' % (len(TOOL_LIST), BASE), file=sys.stderr)
    hello()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            print('[mcp] 收到非 JSON 输入: %s' % e, file=sys.stderr)
            continue
        try:
            handle(msg)
        except Exception as e:
            print('[mcp] 处理出错: %s' % e, file=sys.stderr)
            if msg.get('id') is not None:
                reply(msg['id'], error={'code': -32603, 'message': str(e)})


if __name__ == '__main__':
    main()
