"""AI白板后端 v2 —— 统一 LLM 链路（意图判断 + 元素生成 + 视觉理解）

相对 server.py 的改动：
  1. API Key 不再硬编码：读环境变量 AI_API_KEY / MIMO_API_KEY，或同目录 ai_config.json
  2. 恢复 TLS 证书校验（原版 verify_mode=CERT_NONE 有中间人风险）
  3. 新增 POST /api/ai/act —— 一次调用完成「意图判断 + 画图元素生成」，并支持画布截图（视觉）
  4. POST /api/ai/draw 优先用 LLM 生成元素 JSON，失败再回落原模板引擎
  5. GET /api/health 探活；端口占用时给出明确提示
  6. 多轮上下文（前端可传 history）

启动：
    # Windows PowerShell
    $env:AI_API_KEY = "tp-你的新key"
    python server_v2.py

可选环境变量：
    AI_BASE_URL      默认 https://token-plan-cn.xiaomimimo.com/v1
    AI_MODEL         默认 mimo-v2.5-pro   （纯文本：分析/闲聊）
    AI_VISION_MODEL  默认 mimo-v2.5       （支持图像输入：看画布截图）
    PORT             默认 9091
"""
import base64
import hashlib
import hmac
import json
import os
import re
import socketserver
import shutil
import ssl
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import http.server

PORT = int(os.environ.get('PORT', '9091'))
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(DIRECTORY, 'ai_config.json')
RULES_PATH = os.path.join(DIRECTORY, 'web_rules.json')

DEFAULTS = {
    'base_url': 'https://api.deepseek.com/v1',
    'model': 'deepseek-chat',
    'vision_model': '',          # 留空 = 不支持图像（不发截图）；填模型名则启用视觉通道
    'vision_base_url': '',       # 视觉通道可独立用另一家（留空 = 与 base_url 相同）
    'vision_api_key': '',
    # 语音通道（mimo 的 asr/tts 走 chat/completions，与视觉同一家即可）
    'asr_model': '',                 # 语音转文字；留空 = 关闭语音输入（要语音就填你自己的）
    'tts_model': '',                 # 文字转语音；留空 = 朗读改用浏览器本地合成
    'speech_base_url': '',           # 留空 = 跟随 vision_base_url / base_url
    'speech_api_key': '',
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            for k, v in raw.items():
                if k.startswith('_'):
                    continue
                # vision_*/speech_* 允许显式置空（= 关闭该通道）；其余字段忽略空值
                if k in ('vision_model', 'vision_base_url', 'vision_api_key',
                         'asr_model', 'tts_model', 'speech_base_url', 'speech_api_key') or v:
                    cfg[k] = v
    except Exception as e:
        print(f'[WARN] 读取 ai_config.json 失败: {e}', file=sys.stderr)
    if os.environ.get('AI_BASE_URL'):
        cfg['base_url'] = os.environ['AI_BASE_URL']
    if os.environ.get('AI_MODEL'):
        cfg['model'] = os.environ['AI_MODEL']
    if os.environ.get('AI_VISION_MODEL') is not None:
        cfg['vision_model'] = os.environ['AI_VISION_MODEL']
    if os.environ.get('AI_VISION_BASE_URL') is not None:
        cfg['vision_base_url'] = os.environ['AI_VISION_BASE_URL']
    if os.environ.get('AI_VISION_API_KEY') is not None:
        cfg['vision_api_key'] = os.environ['AI_VISION_API_KEY']
    cfg['api_key'] = (os.environ.get('AI_API_KEY') or os.environ.get('MIMO_API_KEY')
                      or cfg.get('api_key') or '')
    return cfg


CONFIG = load_config()
VISION_CACHE = {}   # {图片hash: (时间戳, 描述)} —— 避免多轮对话重复看图

# ---------------------------------------------------------------
#  注册用户：谁开的板，agent 就驻扎在谁的板上
#  设计取舍：一块板有一个"主人"（开板的人），主人登录后拿到自己那块固定板号
#  （u-<名字哈希>），他的 agent 报到/干活都带这个板号 —— 多人同时用时各在各的板上。
#  密码用 pbkdf2 存，不存明文；登录给一个 token，前端放 localStorage。
#  局域网默认仍是"打开就能用"（WB_TRUST_LAN），不强制登录；登录是为了"我的板 + 我的 agent"。
# ---------------------------------------------------------------
USERS_PATH = os.path.join(DIRECTORY, 'users.json')
USER_LOCK = threading.Lock()
USERS = {'users': {}, 'boards': {}, 'sessions': {}}
SESSION_TTL = 30 * 24 * 3600      # 登录有效期 30 天


def pw_hash(pw, salt=None):
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac('sha256', str(pw).encode('utf-8'), salt.encode(), 120000).hex()
    return salt + '$' + h


def pw_ok(pw, stored):
    try:
        salt, _h = str(stored).split('$', 1)
    except ValueError:
        return False
    return hmac.compare_digest(pw_hash(pw, salt), str(stored))


def user_save():
    """落盘（原子替换）；只在持锁时调用"""
    try:
        tmp = USERS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(USERS, f, ensure_ascii=False, indent=1)
        os.replace(tmp, USERS_PATH)
    except Exception as e:
        print(f'[USER] 落盘失败：{e}', file=sys.stderr, flush=True)


def user_load():
    try:
        with open(USERS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in ('users', 'boards', 'sessions'):
                if isinstance(data.get(k), dict):
                    USERS[k] = data[k]
            print(f'[USER] 载入 {len(USERS["users"])} 个用户 / {len(USERS["boards"])} 块有主的板',
                  file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[USER] 用户文件损坏，忽略：{e}', file=sys.stderr, flush=True)


def board_of_user(name):
    """用户的固定板号：用名字哈希，任何名字（含中文）都能得到 URL 安全的板号"""
    return 'u-' + hashlib.sha1(str(name).encode('utf-8')).hexdigest()[:10]


def _clean_name(name):
    nm = re.sub(r'\s+', ' ', str(name or '')).strip()
    if not (1 <= len(nm) <= 24):
        return ''
    return nm


def user_register(name, pw):
    nm = _clean_name(name)
    if not nm:
        return {'ok': False, 'error': '名字请填 1-24 个字'}
    if len(str(pw or '')) < 4:
        return {'ok': False, 'error': '密码至少 4 位'}
    with USER_LOCK:
        if nm in USERS['users']:
            return {'ok': False, 'error': '这个名字已经被注册了，换一个或直接登录'}
        bd = board_of_user(nm)
        USERS['users'][nm] = {'pw': pw_hash(pw), 'ts': time.time(), 'board': bd}
        USERS['boards'][bd] = {'owner': nm, 'members': [nm], 'ts': time.time()}
        tok = secrets.token_hex(16)
        USERS['sessions'][tok] = {'user': nm, 'ts': time.time()}
        user_save()
    return {'ok': True, 'token': tok, 'user': nm, 'board': bd}


def user_login(name, pw):
    nm = _clean_name(name)
    with USER_LOCK:
        u = USERS['users'].get(nm)
        if not u or not pw_ok(pw, u.get('pw')):
            return {'ok': False, 'error': '名字或密码不对'}
        tok = secrets.token_hex(16)
        USERS['sessions'][tok] = {'user': nm, 'ts': time.time()}
        for k in [k for k, v in USERS['sessions'].items() if time.time() - v.get('ts', 0) > SESSION_TTL]:
            USERS['sessions'].pop(k, None)
        user_save()
        return {'ok': True, 'token': tok, 'user': nm, 'board': u.get('board') or board_of_user(nm)}


def user_by_token(tok):
    t = str(tok or '').strip()
    if not t:
        return ''
    with USER_LOCK:
        s = USERS['sessions'].get(t)
        if not s or time.time() - s.get('ts', 0) > SESSION_TTL:
            return ''
        return s.get('user', '')


def user_logout(tok):
    with USER_LOCK:
        USERS['sessions'].pop(str(tok or '').strip(), None)
        user_save()
    return {'ok': True}


def board_owner(board):
    bd = str(board or '')
    with USER_LOCK:
        b = USERS['boards'].get(bd)
        return b.get('owner', '') if b else ''


def board_claim(board, user):
    """登录用户第一次在这块板上画 => 记成他的板（谁开的板就是谁的）"""
    bd, nm = str(board or ''), str(user or '')
    if not bd or not nm:
        return ''
    with USER_LOCK:
        b = USERS['boards'].get(bd)
        if b:
            if nm not in b['members']:
                b['members'].append(nm)
                user_save()
            return b.get('owner', '')
        USERS['boards'][bd] = {'owner': nm, 'members': [nm], 'ts': time.time()}
        user_save()
        return nm


def board_join(user, board):
    nm, bd = _clean_name(user), board_key(board)
    if not nm or not bd:
        return {'ok': False, 'error': '参数不全'}
    with USER_LOCK:
        b = USERS['boards'].get(bd)
        if not b:
            USERS['boards'][bd] = {'owner': nm, 'members': [nm], 'ts': time.time()}
        else:
            if nm not in b['members']:
                b['members'].append(nm)
        user_save()
        return {'ok': True, 'board': bd, 'owner': USERS['boards'][bd].get('owner', '')}


user_load()

# ---------------------------------------------------------------
#  多端实时同步（平板 / 电脑共享同一块画布）
#  设计：整块画布状态 + 单调递增 rev，客户端长轮询拉取；写冲突以服务端为准。
# ---------------------------------------------------------------
SYNC_PATH = os.path.join(DIRECTORY, 'sync_state.json')
SYNC_LOCK = threading.Lock()
SYNC_BOARDS = {}    # {board: {'rev': int, 'state': {...}, 'by': 客户端id, 'ts': 时间}}
SYNC_EVENTS = {}    # {board: threading.Event}  用于长轮询唤醒
SYNC_PEERS = {}     # {board: {客户端id: 最后出现时间}}
DISC_LOG = {}       # {board: [{id, seq, t, text, who, marks, spk}]}  多端讨论转写
DISC_SEQ = {}       # {board: 递增序号}
DISC_MAX = 3000
TODOS = {'items': [], 'ts': 0}   # 白板上的待办（前端推送，供外部系统/脚本读取）
# ---------- 外部 agent（MCP）驱动画板：任务队列 ----------
# 工具真正执行的地方是浏览器里的 execTool，所以服务端只做"排队 + 等结果"：
#   MCP 进程 → /api/agent/call（排队并等）→ 页面 /api/agent/poll 取走并执行 → /api/agent/result 回传
# ---------- 共享对话：白板界面 ↔ 外部 agent ----------
# 放在服务端的原因：远程 agent 不该依赖"浏览器开着"才能说话；
# 白板页面只负责显示与推送，双方看到的是同一条对话。
# 接入的 agent（谁在用这块板子）：MCP 进程启动时报到，之后每次调用顺带心跳
AGENT_LOCK2 = threading.Lock()
AGENTS = {}          # name -> {name, ts, ver}


def agent_hello(name, ver='', board=''):
    nm = (str(name or '').strip() or 'agent')[:32]
    bd = board_key(board)
    with AGENT_LOCK2:
        AGENTS[nm] = {'name': nm, 'ts': time.time(), 'ver': str(ver or '')[:24], 'board': bd}
        # 只留最近 10 个
        for k in sorted(AGENTS, key=lambda x: AGENTS[x]['ts'])[:-10]:
            AGENTS.pop(k, None)
    return nm


def agent_live(ttl=45, board=None):
    now = time.time()
    bd = board_key(board)
    with AGENT_LOCK2:
        out = []
        for v in AGENTS.values():
            if now - v['ts'] > ttl:
                continue
            if v.get('board') != bd:
                continue
            out.append(dict(v))
        return out


CHAT_LOCK = threading.Lock()
CHAT = []           # [{seq, who, text, ts, src}]
CHAT_SEQ = [0]


def chat_push(who, text, src='', board=''):
    bd = board_key(board)
    with CHAT_LOCK:
        CHAT_SEQ[0] += 1
        item = {'seq': CHAT_SEQ[0], 'who': str(who or 'agent')[:24], 'text': str(text or '')[:4000],
                'ts': int(time.time() * 1000), 'src': str(src or '')[:24],
                'board': bd}
        CHAT.append(item)
        del CHAT[:-300]                     # 只留最近 300 条
    chat_save()                             # 落盘：重启后对话还在，换台设备也能看到历史
    return item


def chat_since(since, board=None):
    bd = board_key(board)
    with CHAT_LOCK:
        return [m for m in CHAT
                if m['seq'] > since and m.get('board') == bd]


def chat_clear(board=None):
    """清空某块板的对话（含 agent 那份）。
    以前"清空对话"只清浏览器本地，agent 用 board_chat_read 还能读到旧消息 ——
    用户以为清掉了、其实没有。现在两边一起清。"""
    bd = board_key(board)
    with CHAT_LOCK:
        keep = [m for m in CHAT if m.get('board') != bd]
        n = len(CHAT) - len(keep)
        del CHAT[:]
        CHAT.extend(keep)
    chat_save()
    return n


AGENT_LOCK = threading.Lock()
AGENT_QUEUE = []
AGENT_SEQ = [0]


def agent_enqueue(tool, args, board=''):
    bd = board_key(board)
    with AGENT_LOCK:
        AGENT_SEQ[0] += 1
        item = {'id': 'a%d' % AGENT_SEQ[0], 'tool': tool, 'args': args or {},
                'board': bd,
                'ev': threading.Event(), 'result': None, 'taken': False, 'ts': time.time()}
        AGENT_QUEUE.append(item)
        del AGENT_QUEUE[:-30]          # 只留最近 30 个，避免无人消费时无限堆积
    return item


def agent_take(board=None):
    """取一个待执行的任务。只取本板的任务（空号按 default 板算，绝不当通配）——
    多人多板同时用时，工具调用绝不能落到别人的板上。"""
    bd = board_key(board)
    with AGENT_LOCK:
        for it in AGENT_QUEUE:
            if it.get('taken'):
                continue
            if it.get('board') != bd:
                continue
            it['taken'] = True
            return it
    return None


TODOS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'todos.json')

# ---------- 持久化：对话 / 讨论转写 / 附件 ----------
# 以前这三样只存内存：服务一重启，对话和讨论记录就没了，换台设备也看不到历史。
# 现在按板落盘，条数有上限，写盘用"临时文件 + 原子替换"，避免写一半留坏 JSON。
CHAT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chat.json')
DISC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'disc.json')
DOCS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs.json')
CHAT_KEEP = 300          # 每块板保留的对话条数（和内存里一致）
DISC_KEEP = 3000         # 每块板保留的转写条数

# 附件：用户上传的"方案"之类文本，按板存放，agent / 内置 AI 按需读取
DOC_LOCK = threading.Lock()
DOCS = {}                # {board: [{id, name, chars, ts, text}]}
DOC_MAX = 20             # 每块板最多留 20 份
DOC_MAX_CHARS = 400000   # 单份上限（约 40 万字，够放一份长方案）
DOC_TTL = 24 * 3600      # 24 小时后自动过期
DOC_INTO_AI = 30000      # 每次喂给内置 AI 的字符上限（再长就只给开头，并说明被截断）


def _json_save(path, obj):
    """原子写：先写 .tmp 再替换，避免写一半断电留下坏文件。

    另外顺手把权限收到 600：这些文件里有账号哈希、对话、上传的方案，
    默认 umask 会给 644 —— 同一台机器上的其它用户/进程就能直接读走。
    """
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, path)
    except Exception as e:
        print(f'[SAVE] 写 {os.path.basename(path)} 失败：{e}', file=sys.stderr, flush=True)


def chat_load():
    try:
        with open(CHAT_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        boards = d.get('boards') or {}
        seq = int(d.get('seq') or 0)
        with CHAT_LOCK:
            del CHAT[:]
            for bd, items in boards.items():
                for it in (items or [])[-CHAT_KEEP:]:
                    if isinstance(it, dict) and it.get('text'):
                        CHAT.append(it)
                        seq = max(seq, int(it.get('seq') or 0))
            CHAT_SEQ[0] = seq
        print(f'[CHAT] 载入 {len(CHAT)} 条对话（{len(boards)} 块板）', file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[CHAT] 对话文件损坏，忽略：{e}', file=sys.stderr, flush=True)


def chat_save():
    with CHAT_LOCK:
        boards = {}
        for m in CHAT:
            boards.setdefault(m.get('board') or 'default', []).append(m)
        for k in boards:
            boards[k] = boards[k][-CHAT_KEEP:]
        snap = {'seq': CHAT_SEQ[0], 'boards': boards}
    _json_save(CHAT_PATH, snap)


def disc_load():
    try:
        with open(DISC_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        with SYNC_LOCK:
            for bd, items in (d.get('boards') or {}).items():
                DISC_LOG[bd] = (items or [])[-DISC_KEEP:]
            for bd, n in (d.get('seq') or {}).items():
                DISC_SEQ[bd] = int(n)
        print(f'[DISC] 载入 {len(DISC_LOG)} 块板的转写', file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[DISC] 转写文件损坏，忽略：{e}', file=sys.stderr, flush=True)


def disc_save():
    with SYNC_LOCK:
        snap = {'boards': {k: v[-DISC_KEEP:] for k, v in DISC_LOG.items()}, 'seq': dict(DISC_SEQ)}
    _json_save(DISC_PATH, snap)


def docs_load():
    try:
        with open(DOCS_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            with DOC_LOCK:
                for bd, items in d.items():
                    DOCS[bd] = [x for x in (items or []) if isinstance(x, dict)][-DOC_MAX:]
        print(f'[DOC] 载入 {sum(len(v) for v in DOCS.values())} 份附件', file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[DOC] 附件文件损坏，忽略：{e}', file=sys.stderr, flush=True)


def docs_save():
    # 注意：这里不要用 dict 的 | 合并 —— 部署机（Ubuntu 20.04）自带 Python 3.8，
    # 而 dict | dict 是 3.9 才有的写法，在 3.8 上会直接 TypeError（接口 500）。
    with DOC_LOCK:
        snap = {k: [dict(x) for x in v] for k, v in DOCS.items()}
    _json_save(DOCS_PATH, snap)


def doc_clean(board=None):
    """清掉过期的附件（默认清所有板）"""
    now = time.time()
    with DOC_LOCK:
        boards = [board] if board else list(DOCS.keys())
        removed = 0
        for bd in boards:
            items = DOCS.get(bd) or []
            keep = [x for x in items if now - float(x.get('ts') or 0) <= DOC_TTL]
            removed += len(items) - len(keep)
            DOCS[bd] = keep
    if removed:
        docs_save()
    return removed


def doc_push(board, name, text, src=''):
    bd = board_key(board)
    tx = str(text or '')
    if not tx.strip():
        return {'ok': False, 'error': '文件是空的'}
    if len(tx) > DOC_MAX_CHARS:
        tx = tx[:DOC_MAX_CHARS]
    doc_clean(bd)
    item = {'id': 'd%d' % int(time.time() * 1000), 'name': str(name or '文档')[:80],
            'chars': len(tx), 'ts': time.time(), 'src': str(src or '')[:24], 'text': tx}
    with DOC_LOCK:
        lst = DOCS.setdefault(bd, [])
        lst.append(item)
        del lst[:-DOC_MAX]
    docs_save()
    print(f'[DOC] /{bd} 收到附件 {item["name"]}（{item["chars"]} 字）', file=sys.stderr, flush=True)
    return {'ok': True, 'id': item['id'], 'name': item['name'], 'chars': item['chars']}


def doc_list(board):
    bd = board_key(board)
    doc_clean(bd)
    with DOC_LOCK:
        return [{'id': x['id'], 'name': x.get('name'), 'chars': x.get('chars'),
                 'ts': int(x.get('ts') or 0)} for x in (DOCS.get(bd) or [])]


def doc_get(board, doc_id):
    bd = board_key(board)
    with DOC_LOCK:
        for x in (DOCS.get(bd) or []):
            if x.get('id') == doc_id:
                return x
    return None


def doc_read(board, doc_id='', start=0, end=0):
    """读附件内容；可按行区间读（长方案分段读，别一次塞爆上下文）"""
    bd = board_key(board)
    doc_clean(bd)
    with DOC_LOCK:
        items = DOCS.get(bd) or []
        if not items:
            return {'ok': False, 'error': '这块板还没有附件'}
        d = None
        for x in items:
            if not doc_id or x.get('id') == doc_id:
                d = x
        if not d:
            return {'ok': False, 'error': '没有这份附件（id=%s）' % doc_id}
        text = d.get('text') or ''
    lines = text.replace('\r\n', '\n').split('\n')
    a = max(1, int(start or 1))
    b = int(end or 0) or len(lines)
    b = max(a, min(b, len(lines)))
    seg = '\n'.join(lines[a - 1:b])
    return {'ok': True, 'id': d['id'], 'name': d.get('name'), 'chars': len(text),
            'lines': len(lines), 'from': a, 'to': b, 'text': seg,
            'truncated': len(seg) < len(text)}


def doc_del(board, doc_id):
    bd = board_key(board)
    with DOC_LOCK:
        lst = DOCS.get(bd) or []
        n0 = len(lst)
        DOCS[bd] = [x for x in lst if x.get('id') != doc_id]
        n = n0 - len(DOCS[bd])
    if n:
        docs_save()
    return {'ok': True, 'removed': n}


TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wb_token.txt')


MCP_PROTOCOL = '2024-11-05'

JOIN_MD = """# 加入这块白板（agent 自助入驻）

你正在访问的是一块 **AI 白板**。它是一块画布 + 一个共享对话，
**你和用户共用它**：用户在画板上说话，你能听到；你画的、改的，用户直接看到。

> **这块板的板号是 `%(bid)s`。下面每个请求都要带上它。**
> 不带板号会被当成 default 板 —— 用户看不到你，你也看不到用户（这是"谁开的板就是谁的 agent 驻扎"）。

## 最快的方式：直接用 HTTP（不需要任何客户端）

1. **报到**（用户会在画板顶部看到"🤖 <你的名字> 已接入"）

   ```
   POST %(base)s/api/agent/hello
   {"name": "你的名字", "board": "%(bid)s"}
   ```

2. **听用户说话**（阻塞，最多等 timeout 秒，有人说话立刻返回）

   ```
   POST %(base)s/api/chat/wait
   {"since": 上次的最后序号(默认0), "timeout": 50, "board": "%(bid)s"}
   ```

3. **回话**（用户会在画板对话面板里看到）

   ```
   POST %(base)s/api/chat/push
   {"who": "你的名字", "text": "要说的话", "board": "%(bid)s"}
   ```

4. **画东西**：工具清单在 `GET %(base)s/api/tools`（46 个：画流程图/架构图、增删改元素、连线、
   自动布局、圈注、导出、多画板、演示、回放…）。调用方式：

   ```
   POST %(base)s/api/agent/call
   {"tool": "add_flow", "args": {...}, "board": "%(bid)s"}
   ```

   注意：画板类工具的执行发生在**浏览器里**，所以需要有页面开着 %(board)s 。
   没人开着页面时它会明确回"白板页面没有响应"，而不是卡住。

## 如果你支持 MCP

直接用 **HTTP 传输**连这个地址即可，不需要在本地放任何文件（板号写在 URL 里）：

```
%(base)s/mcp?board=%(bid)s
```

（同一套 46 个画板工具 + 3 个对话工具：board_chat_say / board_chat_read / board_chat_wait；
另有 2 个读附件的工具：`board_doc_list`（看用户上传了什么方案）、`board_doc_read`（读正文，长文按行区间分段读））

## 鉴权

- 内网部署：通常**不需要令牌**，直接调。
- 需要令牌的部署：请求头带 `X-WB-Token: <令牌>`（或访问 `%(board)s&token=<令牌>` 一次即可记住）。

## 机器可读的清单

`GET %(base)s/.well-known/agent.json`
"""


def build_join_md(base, board, bid=''):
    return JOIN_MD % {'base': base, 'board': board, 'bid': bid or 'default'}


# agent 生态是英文优先的：同一个地址加 ?lang=en（或请求头 Accept-Language 是英文）就给英文版。
# 中文版仍然是默认 —— 这块板的主要使用者说中文。
JOIN_MD_EN = """# Join this whiteboard (self-onboarding for agents)

You are looking at an **AI whiteboard**: a shared canvas plus a shared conversation.
**You and the human share it.** What the human says on the board, you can hear; what you draw or edit, the human sees immediately.

> **This board's id is `%(bid)s`. Every request below must carry it.**
> Without it you land on the `default` board — the human will not see you and you will not see the human.
> (That is how "whoever opened the board owns the agent" is enforced.)

## Fastest path: plain HTTP (no client needed)

1. **Say hello** (the board shows "🤖 <your name> has checked in")

   ```
   POST %(base)s/api/agent/hello
   {"name": "your-name", "board": "%(bid)s"}
   ```

2. **Listen to the human** (blocks up to `timeout` seconds; returns immediately when somebody speaks)

   ```
   POST %(base)s/api/chat/wait
   {"since": last_seq (0 to start), "timeout": 50, "board": "%(bid)s"}
   ```

3. **Reply** (the human sees it in the board's conversation panel)

   ```
   POST %(base)s/api/chat/push
   {"who": "your-name", "text": "what you want to say", "board": "%(bid)s"}
   ```

4. **Draw**: the tool list is at `GET %(base)s/api/tools` (46 board tools: flowcharts, architecture diagrams,
   adding/editing/deleting elements, connectors, auto layout, annotation, export, multi-page, presentation, replay …).
   Call one with:

   ```
   POST %(base)s/api/agent/call
   {"tool": "add_flow", "args": {...}, "board": "%(bid)s"}
   ```

   Note: board tools execute **inside the browser** (they act on the real canvas), so a page must have
   %(board)s open. When no page is open the call fails with an explicit "the board page did not respond"
   instead of hanging.

## If you speak MCP

Connect over **HTTP transport** to this URL — nothing to install locally (the board id is in the URL):

```
%(base)s/mcp?board=%(bid)s
```

(Same 46 board tools + 3 chat tools: board_chat_say / board_chat_read / board_chat_wait; plus 2 document tools:
`board_doc_list` to see what the user uploaded and `board_doc_read` to read it — long documents can be read in line ranges.)

## Auth

- LAN deployments normally need **no token** — just call.
- Deployments that require one: send the header `X-WB-Token: <token>`
  (or open `%(board)s&token=<token>` once and it will be remembered).

## Machine-readable manifest

`GET %(base)s/.well-known/agent.json`
"""


def wants_english(headers=None, query=None):
    """要不要英文？?lang=en 优先，其次看 Accept-Language（默认中文）"""
    try:
        q = query or {}
        lang = str((q.get('lang') or [''])[0]).lower()
        if lang:
            return lang.startswith('en')
        al = str((headers or {}).get('Accept-Language') or '').lower()
        if al:
            # 简单判断：第一顺位是英文就给英文
            first = al.split(',')[0].strip()
            return first.startswith('en')
    except Exception:
        pass
    return False


def build_join_md_lang(base, board, bid='', en=False):
    tpl = JOIN_MD_EN if en else JOIN_MD
    return tpl % {'base': base, 'board': board, 'bid': bid or 'default'}


def load_todos():
    """待办原来只存在内存里：服务端一重启就全没了。落一份盘。"""
    try:
        with open(TODOS_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get('items'), list):
            TODOS['items'] = d['items'][:400]
            TODOS['ts'] = int(d.get('ts') or 0)
            print('[TODO] 从磁盘载入 %d 条待办' % len(TODOS['items']), file=sys.stderr, flush=True)
    except Exception:
        pass


def save_todos():
    """写盘：先写临时文件再原子替换，避免写一半断电留下坏 JSON。"""
    try:
        tmp = TODOS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'items': TODOS['items'][:400], 'ts': TODOS['ts']}, f, ensure_ascii=False)
        os.replace(tmp, TODOS_PATH)
    except Exception as e:
        print('[WARN] 待办落盘失败: %s' % e, file=sys.stderr, flush=True)


def load_token():
    """访问令牌：非本机来源要带它，否则同网段任何设备都能改画布、花用户的 API key。
    本机（127.0.0.1）直接放行，保证主要用法零摩擦。"""
    try:
        with open(TOKEN_PATH, 'r', encoding='utf-8') as f:
            t = f.read().strip()
        if len(t) >= 16:
            return t
    except Exception:
        pass
    t = secrets.token_hex(16)
    try:
        with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
            f.write(t)
        try:
            os.chmod(TOKEN_PATH, 0o600)     # 令牌等于钥匙，别让同机其它用户读到
        except Exception:
            pass
    except Exception:
        pass
    return t


ACCESS_TOKEN = load_token()

# 46 个画板工具的英文说明（给英文 agent 看的那一面）。
# 中文仍是默认；agent 那侧可以用 ?lang=en 或 WB_LANG=en 拿英文说明 ——
# 工具说明是 agent「挑哪个工具」的依据，英文模型读英文说明会明显更准。
try:
    from 工具英文说明 import TOOL_DESC_EN
except Exception:
    TOOL_DESC_EN = {}


def tools_i18n(en=False, shape='mcp'):
    """按语言组装工具清单。名字和参数永远不变，只换 description。

    shape='mcp'    → [{'name','description','inputSchema'}]，给 MCP tools/list 用
    shape='openai' → [{'type':'function','function':{...}}]，**/api/tools 的原样结构**
                     （mcp_server.py 与工具同步自检都按这个形状读，改了会破坏它们的契约）
    """
    out = []
    for t in WB_TOOLS:
        fn = t.get('function') or {}
        nm = fn.get('name') or ''
        cn = fn.get('description') or ''
        desc = (TOOL_DESC_EN.get(nm) or cn) if en else cn
        params = fn.get('parameters') or {'type': 'object', 'properties': {}}
        if shape == 'openai':
            out.append({'type': 'function', 'function': {'name': nm, 'description': desc,
                                                         'parameters': params}})
        else:
            out.append({'name': nm, 'description': desc, 'inputSchema': params})
    chat_en = [
        {'name': 'board_doc_list',
         'description': 'List the documents the user uploaded to this board (plans, specs). Check here first, then '
                        'read the content with board_doc_read.',
         'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'board_doc_read',
         'description': 'Read an uploaded document. Long documents can be read in line ranges: from / to are 1-based '
                        'line numbers; omit from to read from the beginning.',
         'inputSchema': {'type': 'object', 'properties': {
             'id': {'type': 'string', 'description': 'document id (newest if omitted)'},
             'from': {'type': 'integer', 'description': 'first line (1-based)'},
             'to': {'type': 'integer', 'description': 'last line (inclusive)'}}}},
        {'name': 'board_chat_say',
         'description': 'Say something on the board; the user sees it immediately.',
         'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}},
        {'name': 'board_chat_read',
         'description': 'Read the board conversation (what the user and other agents said).',
         'inputSchema': {'type': 'object', 'properties': {'limit': {'type': 'integer'}}}},
        {'name': 'board_chat_wait',
         'description': 'Wait for the user to speak (blocks up to timeout seconds, returns at once on a message).',
         'inputSchema': {'type': 'object', 'properties': {'since': {'type': 'integer'},
                                                          'timeout': {'type': 'integer'}}}},
    ]
    chat_cn = [
        {'name': 'board_doc_list',
         'description': '列出用户上传到这块板的附件（方案 / 文档）。上传后先看这里，再用 board_doc_read 读正文。',
         'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'board_doc_read',
         'description': '读附件正文。长文档按行区间分段读：from / to 是行号（从 1 开始），不传 from 从头读。',
         'inputSchema': {'type': 'object', 'properties': {
             'id': {'type': 'string', 'description': '附件 id（不传取最新那份）'},
             'from': {'type': 'integer', 'description': '起始行号'},
             'to': {'type': 'integer', 'description': '结束行号（含）'}}}},
        {'name': 'board_chat_say', 'description': '对白板说一句话，用户直接看到。',
         'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}},
        {'name': 'board_chat_read', 'description': '读白板上的对话（用户说的 + 其它 agent 说的）。',
         'inputSchema': {'type': 'object', 'properties': {'limit': {'type': 'integer'}}}},
        {'name': 'board_chat_wait', 'description': '等用户说话（阻塞最多 timeout 秒，有消息立刻返回）。',
         'inputSchema': {'type': 'object', 'properties': {'since': {'type': 'integer'},
                                                          'timeout': {'type': 'integer'}}}},
    ]
    # 对话工具只在 MCP 形状里附加；/api/tools 原本就是 46 个画板工具（OpenAI 形状），
    # 加进去会破坏 mcp_server.py 与工具同步自检的既有契约
    if shape == 'openai':
        return out
    return out + (chat_en if en else chat_cn)


TLS_PORT = int(os.environ.get('WB_TLS_PORT') or 0)

# 令牌策略（部署时用环境变量决定）
#   WB_NO_TOKEN=1  —— 完全不要令牌（只适合完全可信的网络）
#   WB_TRUST_LAN=1 —— 内网/私有地址免令牌，只有外网来源才要（内网团队共用推荐这个）
#   都不设        —— 非本机一律要令牌（公网部署的安全默认）
NO_TOKEN = os.environ.get('WB_NO_TOKEN') == '1'
TRUST_LAN = os.environ.get('WB_TRUST_LAN') == '1'


def _is_private_ip(ip):
    """RFC1918 私有地址 + 回环。内网部署靠这个免掉"每台设备都要输令牌"。"""
    try:
        if ip in ('127.0.0.1', '::1', 'localhost'):
            return True
        if ip.startswith('10.') or ip.startswith('192.168.') or ip.startswith('169.254.'):
            return True
        if ip.startswith('172.'):
            second = int(ip.split('.')[1])
            return 16 <= second <= 31
        if ip.startswith('fc') or ip.startswith('fd') or ip.startswith('fe80'):
            return True     # IPv6 私网
    except Exception:
        pass
    return False
SYNC_MAX_BOARDS = 300       # 上限（以前 40，太容易把"好久没打开"的真板挤掉，见 sync_evict）


def board_key(b):
    """板号规范化：只需要"短标识"，不是标识的一律当 default。

    为什么要有这个：板号来自 URL/JSON，是**外部输入**。曾经有测试脚本把整个画布状态
    当成板号发上来，服务端 `[:64]` 一截，就在状态文件里留下了一张
    键为 `{'page': 'deep-test-…', 'els': [{'id': 'r0'` 的怪板 —— 既没人打开得了，
    又会占掉 SYNC_MAX_BOARDS 的名额（真板可能因此被挤掉）、把状态文件越写越大。

    规则故意收得很窄，不误伤正常板号：
      · 正常板号（`bmuabc123`、中文名、"我的板 1"）原样保留；
      · 空 -> `default`（**不做通配**，空板号绝不能等于"所有板"）；
      · 含 `{}[]'"\\` 或换行，或超过 64 字符 -> 当 `default`。
    """
    s = str(b or '').strip()
    if not s:
        return 'default'
    if len(s) > 64 or re.search(r'[{}()\[\]"\'\\\r\n]', s):
        return 'default'
    return s


def sync_event(board):
    ev = SYNC_EVENTS.get(board)
    if ev is None:
        ev = threading.Event()
        SYNC_EVENTS[board] = ev
    return ev


def sync_notify(board):
    ev = SYNC_EVENTS.get(board)
    if ev is not None:
        ev.set()


def sync_load():
    try:
        with open(SYNC_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            SYNC_BOARDS.update({k: v for k, v in data.items() if isinstance(v, dict)})
            print(f'[SYNC] 载入 {len(SYNC_BOARDS)} 块协作画布', file=sys.stderr, flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'[SYNC] 状态文件损坏，忽略：{e}', file=sys.stderr, flush=True)


def sync_evict():
    """块数超上限时挤掉最旧的板 —— **绝不静默丢内容**。

    踩过的坑：以前是 `sorted(..., key=ts)[:超出数]` 直接 pop，谁最久没动就丢谁。
    本地一堆测试脚本各建一块板，很快把 40 块上限撑满，于是**用户自己那块"好久没打开"的板
    被静默挤掉**，而画布内容当时只存服务端 —— 直接没了。
    现在的规则：
      1. 上限抬到 300（每块板几十 KB，300 块也就几 MB）；
      2. 先挤"一个元素都没有"的板，再考虑有内容的；
      3. 真被挤掉的有内容的板，写进 `sync_state.dropped.json` 留底，并打日志（可人工捞回来）。
    """
    if len(SYNC_BOARDS) <= SYNC_MAX_BOARDS:
        return
    need = len(SYNC_BOARDS) - SYNC_MAX_BOARDS

    def n_els(v):
        st = (v or {}).get('state') or {}
        return len(st.get('els') or []) if isinstance(st, dict) else 0

    order = sorted(SYNC_BOARDS.items(),
                   key=lambda kv: (n_els(kv[1]) > 0, float((kv[1] or {}).get('ts') or 0)))
    dropped = []
    for k, v in order[:need]:
        SYNC_BOARDS.pop(k, None)
        dropped.append((k, v, n_els(v)))
    if not dropped:
        return
    keep = [(k, v) for k, v, n in dropped if n > 0]
    print('[SYNC] 超出 %d 块上限，挤掉 %d 块板（有内容的 %d 块已留底到 sync_state.dropped.json）：%s'
          % (SYNC_MAX_BOARDS, len(dropped), len(keep),
             '、'.join(k for k, _v, _n in dropped[:5])), file=sys.stderr, flush=True)
    if not keep:
        return
    path = SYNC_PATH.replace('.json', '.dropped.json')
    try:
        old = {}
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                old = json.load(f) or {}
        for k, v in keep:
            old[k] = v
        if len(old) > 200:      # 留底也设个上限，按 ts 留最近的
            for k, _v in sorted(old.items(), key=lambda kv: float((kv[1] or {}).get('ts') or 0))[:-200]:
                old.pop(k, None)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(old, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f'[SYNC] 留底失败：{e}', file=sys.stderr, flush=True)


def sync_save():
    """落盘（原子替换）；只在持锁时调用"""
    try:
        sync_evict()
        tmp = SYNC_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(SYNC_BOARDS, f, ensure_ascii=False)
        os.replace(tmp, SYNC_PATH)
    except Exception as e:
        print(f'[SYNC] 落盘失败：{e}', file=sys.stderr, flush=True)


def sync_touch_peer(board, client):
    """记录在线端并清理超时端，返回在线端数"""
    now = time.time()
    peers = SYNC_PEERS.setdefault(board, {})
    if client:
        peers[client] = now
    for k in [k for k, v in peers.items() if now - v > 15]:
        peers.pop(k, None)
    return len(peers)


def sync_state_pages(state):
    """把一份 state 归一成 pages 列表（兼容只有 els 的单页老格式）"""
    if not isinstance(state, dict):
        return []
    pages = state.get('pages')
    if isinstance(pages, list) and pages:
        return [p for p in pages if isinstance(p, dict)]
    return [{'id': 'p0', 'name': '画板 1', 'els': state.get('els') or []}]


def sync_build_idx(state, base=None):
    """从整块 state 建元素索引：key = 页号::元素号 -> {'el','mt','by','seq'}"""
    idx = dict(base or {})
    seq = max([v.get('seq', 0) for v in idx.values()] or [0])
    for p in sync_state_pages(state):
        pid = str(p.get('id') or 'p0')[:64]
        for el in (p.get('els') or []):
            if not isinstance(el, dict) or not el.get('id'):
                continue
            k = pid + '::' + str(el['id'])[:64]
            if k in idx:
                continue
            seq += 1
            idx[k] = {'el': el, 'mt': 0.0, 'by': '', 'seq': seq}
    return idx


def sync_merge_els(board, client, cur, data, now):
    """元素级合并：多人同时改一块板，各改各的元素互不覆盖。

    规则：每个元素按「最后一次改动时刻 mt」比大小（谁新谁赢，同一时刻后来者赢），
    删除用墓碑记录，避免"删掉又被别人推回来"。整块 state 只在页面结构上取推送方的
    顺序，元素一律从合并后的索引重建 —— 这样两边同时画，两边的笔画都在。
    返回 (合并后的 state, 统计)。
    """
    state = data.get('state') if isinstance(data.get('state'), dict) else {}
    idx = cur.get('idx')
    if not isinstance(idx, dict):
        idx = sync_build_idx(cur.get('state'))
    tombs = cur.get('tombs') if isinstance(cur.get('tombs'), dict) else {}
    for k in idx:
        if 'seq' not in idx[k]:
            idx[k]['seq'] = 0
    seq = [max([v.get('seq', 0) for v in idx.values()] or [0])]
    won = lost = dead = 0

    # 同一块板的"第一块画板"必须认同一个号。
    # 两个人都是在默认页上画，可各自的页面号是本地随机生成的 —— 不做这一步，
    # 两边画的东西会各自落到一个画板上，谁都看不到对方（实测抓到的就是这个）。
    # 规则很保守：只有"服务端一页 + 对方也只报一页"且号不同才合并，正常新建页不受影响。
    page_map = {}
    _srv_pages = sync_state_pages(cur.get('state'))
    _in_pages = sync_state_pages(state)
    _srv_ids = [str(p.get('id') or 'p0')[:64] for p in _srv_pages]
    _in_ids = [str(p.get('id') or 'p0')[:64] for p in _in_pages]
    _single = (len(_srv_pages) == 1 and len(_in_pages) == 1)
    if _single and _in_ids[0] != _srv_ids[0]:
        page_map[_in_ids[0]] = _srv_ids[0]
        print(f'[SYNC] /{board} 第一块画板认同服务端页号：{_in_ids[0]} -> {_srv_ids[0]}',
              file=sys.stderr, flush=True)

    def _pid(v):
        p = str(v or 'p0')[:64]
        if p in page_map:
            return page_map[p]
        # 还有一种更隐蔽的情况：客户端"认同页号"的前后各推了一次，
        # 元素还挂在旧的页号上（双方都不认识这个页号）——双方都只有一页时并到那一页，
        # 否则这些元素会各占一个新页，两个人就看不到对方画的东西（实测抓到过）。
        if _single and p not in _srv_ids and p not in _in_ids:
            page_map[p] = _srv_ids[0]
            print(f'[SYNC] /{board} 元素挂在陌生页号 {p} 上，并到 {_srv_ids[0]}', file=sys.stderr, flush=True)
            return _srv_ids[0]
        return p

    for d in (data.get('deltas') or [])[:5000]:
        if not isinstance(d, dict):
            continue
        el = d.get('el')
        if not isinstance(el, dict) or not el.get('id'):
            continue
        pid = _pid(d.get('p'))
        k = pid + '::' + str(el['id'])[:64]
        try:
            mt = float(d.get('mt') or 0)
        except (TypeError, ValueError):
            mt = 0.0
        old = idx.get(k)
        if old and float(old.get('mt') or 0) > mt:
            lost += 1                      # 别人更新的改动已经在服务器上了，不覆盖
            continue
        if k in tombs and float(tombs[k].get('mt') or 0) > mt:
            lost += 1
            continue
        tombs.pop(k, None)
        seq[0] += 1
        idx[k] = {'el': el, 'mt': mt, 'by': str(d.get('by') or client or '')[:40], 'seq': seq[0]}
        won += 1

    for t in (data.get('tombs') or [])[:5000]:
        if not isinstance(t, dict):
            continue
        k = str(t.get('k') or '')[:140]
        if not k:
            continue
        if page_map and '::' in k:
            _pre, _eid = k.split('::', 1)
            k = _pid(_pre) + '::' + _eid      # 删掉的元素也要跟着换页号，否则墓碑对不上
        try:
            mt = float(t.get('mt') or 0)
        except (TypeError, ValueError):
            mt = 0.0
        old = idx.get(k)
        if old and float(old.get('mt') or 0) > mt:
            continue                       # 别人在我删之后又改了它，保留别人的版本
        if idx.pop(k, None) is not None:
            dead += 1
        tombs[k] = {'mt': mt, 'by': str(t.get('by') or client or '')[:40]}

    # 重建页面：以推送方的页面顺序为主，服务器独有的页补在后面（绝不吞掉别人的页）
    names = {}
    order = []
    for p in sync_state_pages(cur.get('state')):
        pid = str(p.get('id') or 'p0')[:64]
        names[pid] = p.get('name') or '画板'
        if pid not in order:
            order.append(pid)
    for p in sync_state_pages(state):
        pid = _pid(p.get('id'))
        if p.get('name'):
            names[pid] = p.get('name')
        if pid not in order:
            order.append(pid)
    for k in idx:
        pid = k.split('::', 1)[0]
        if pid not in order:
            order.append(pid)

    pages = []
    _in_ids = [_pid(p.get('id')) for p in sync_state_pages(state)]
    for pid in order:
        els = [v for k, v in idx.items() if k.split('::', 1)[0] == pid]
        els.sort(key=lambda v: v.get('seq', 0))
        if not els and pid not in _in_ids:
            continue                       # 服务器独有的空页（对方已删）不再保留
        pages.append({'id': pid, 'name': names.get(pid) or '画板', 'els': [v['el'] for v in els]})
    if not pages:
        pages = [{'id': 'p0', 'name': '画板 1', 'els': []}]

    out = dict(state) if state else {}
    out['pages'] = pages
    # 注意：cur 可能压根没带（脚本只推元素时），这时按第一页算；
    # 不能写成 if cur_id not in ids（缺 cur 时 _pid 会把它翻成 'p0'，翻完看着"有效"就不会赋值 -> KeyError）
    cur_id = _pid(out.get('cur'))
    _ids = [p['id'] for p in pages]
    out['cur'] = cur_id if cur_id in _ids else pages[0]['id']
    cp = [p for p in pages if p['id'] == out['cur']] or pages[:1]
    out['els'] = cp[0]['els']              # 老格式兼容：els = 当前页
    for k in ('panX', 'panY', 'zoom'):
        if k not in out:
            old_v = (cur.get('state') or {}).get(k)
            if old_v is not None:
                out[k] = old_v

    # 墓碑只留最近 3000 条，别让状态文件无限长
    if len(tombs) > 3000:
        for k, _v in sorted(tombs.items(), key=lambda kv: kv[1].get('mt', 0))[:len(tombs) - 3000]:
            tombs.pop(k, None)
    return out, {'won': won, 'lost': lost, 'dead': dead, 'els': len(idx), 'idx': idx, 'tombs': tombs}


sync_load()
chat_load()
disc_load()
docs_load()

ELEMENT_TYPES = ('rect', 'round', 'stadium', 'ellipse', 'diamond', 'triangle', 'star',
                 'parallelogram', 'hexagon', 'cylinder', 'cloud', 'document', 'note',
                 'actor', 'package', 'frame', 'text', 'line', 'arrow', 'pen')
TYPE_CN = {'rect': '矩形', 'round': '圆角矩形', 'stadium': '开始/结束', 'ellipse': '椭圆',
           'diamond': '判断', 'triangle': '三角形', 'star': '星形', 'parallelogram': '输入输出',
           'hexagon': '准备', 'cylinder': '数据库', 'cloud': '云', 'document': '文档',
           'note': '便签', 'actor': '角色', 'package': '模块', 'frame': '容器',
           'text': '文字', 'line': '直线', 'arrow': '箭头', 'pen': '手绘', 'edge': '连线',
           'image': '图片'}

# 只有出现这些"动作词"才允许 AI 动笔画布；纯聊天一律不动手（安全网，防止把闲聊画成图）
ACTION_WORDS = ('画', '绘制', '作图', '生成图', '加', '添加', '补充', '补上', '插入', '再来一个',
                '删', '去掉', '移除', '删除', '移', '挪', '改', '换', '重命名', '改名',
                '对齐', '排', '整理', '连', '连接', '连起来', '圈', '标注', '标出',
                '清空', '新建', '撤销', '重做', '导出', '放大', '缩小', '适应')


def looks_like_action(message):
    return any(w in (message or '') for w in ACTION_WORDS)

SYSTEM_ACT = """你是「AI白板」的伙伴，和用户在同一块画布上一起画图、像两个人面对面讨论那样交流。画布元素都带编号（[E1] [E2] …），你可以直接引用它们。

你必须只输出一个 JSON 对象，不要 markdown 代码块、不要多余解释：
{
  "reply": "对用户说的中文，像同事聊天，简短自然",
  "ops": [ ...操作列表，按顺序执行... ]
}

可用操作（可任意组合）：
1) 新增结构（自动排版，不要自己算坐标）：
   {"op":"layout","layout":"flow-right|flow-down","nodes":[{"id":"n1","label":"登录","shape":"rect|ellipse|diamond"}],"edges":[{"from":"n1","to":"n2","label":"是"}],"notes":[{"text":"备注"}]}
2) 自由摆放新增元素：{"op":"add","elements":[{"type":"rect|ellipse|diamond|text|arrow|pen","x":100,"y":100,"width":160,"height":70,"text":"内容"}]}
3) 手绘涂鸦：{"op":"draw","points":[[x,y],[x,y]]}
4) 修改已有元素：{"op":"update","target":"E3","set":{"text":"新文字","strokeColor":"#e03131","strokeWidth":3,"fontSize":20,"width":200,"height":80}}
5) 移动：{"op":"move","target":"E3","dx":0,"dy":140}
6) 删除：{"op":"delete","target":"E3"} 或 {"op":"delete","targets":["E3","E5"]}
7) 复制：{"op":"duplicate","target":"E3","dx":40,"dy":40}
8) 连线：{"op":"connect","from":"E1","to":"E5","label":"是"}（from/to 可以是已有编号，也可以是本次 layout 里的节点 id）
9) 对齐：{"op":"align","targets":["E1","E2","E3"],"mode":"left|center|right|top|middle|bottom"}
10) 选中：{"op":"select","targets":["E1"]}
11) 视口：{"op":"zoom","fit":true} / {"op":"zoom","in":true} / {"op":"zoom","out":true}
12) 圈注与指向（你想"指着图说话"时使用）：
    {"op":"annotate","shape":"circle","target":"E3","color":"#e03131"}
    {"op":"annotate","shape":"arrow","target":"E3","text":"这里少一步校验"}
13) 清空：{"op":"clear"}　撤销/重做：{"op":"undo"} / {"op":"redo"}

行为准则（第 0 条最重要，必须优先遵守）：
0) 【先判断这是不是指令】用户的话里如果【没有】动作词（画 / 加 / 补 / 改 / 删 / 去掉 / 移 / 挪 / 对齐 / 连 / 圈 / 换 / 标 / 导出…），
   那它就是聊天、提问、评价或陈述想法 → **ops 必须为空数组**，只把回话写在 reply 里。
   即使你从这句话里读出了"可以画的结构"，也【绝对不要画】；想说就回一句"要我把这个画出来吗？"，等用户点头再动手。
   例：「我想做一个电商系统」→ ops 空，reply 问"要我把电商系统的架构画出来吗？"；
       「这个流程有点乱」→ ops 空，reply 指出问题并问是否要整理。
1) 要画/加/补结构 → 用 layout（结构清晰、自动排版）；要自由摆放 → 用 add。
2) 要改/删/移/改名/对齐/连线 → 用 update / delete / move / align / connect 引用已有编号【就地修改】，不要整体重画。
3) 你是"一起画图的伙伴"：可以主动圈出问题（annotate）、可以把歪的框对齐（align）、可以让视口跟到你在讲的位置（zoom fit）。
4) layout 里绝不输出坐标（排版自动算）；只有 add / update 才需要坐标。
5) 看不清用户画的是什么就直说并问清楚，不要杜撰。
6) 上限：12 个节点、20 条连线、12 个操作。
7) nodes 可选带配色：{"label":"支付","shape":"rect","color":"primary|success|warn|danger|purple|gray"}（不填按形状自动配色：起止=success、判断=warn、步骤=primary）。"""

SYSTEM_CHAT = """你是「AI白板」的助手。"""   # 旧接口已废弃，仅保留占位避免引用报错

LEGACY_NOTICE = ('⚠️ 检测到你打开的是【旧版页面】：新版 AI 引擎（agent-v1）可以直接画、改、删、连线、对齐、圈注，'
                 '但你当前这个页面走的还是旧接口，所以只能"看和建议"。\n'
                 '请按 Ctrl+Shift+R 强制刷新，或直接打开 http://127.0.0.1:9091/?v=2 再试一次。\n'
                 '（新版加载成功的标志：AI 侧栏会出现一行「✅ AI 引擎已就绪：agent-v1」）')


SYSTEM_REVIEW = """你是「AI白板」的搭档，正静静看着用户画画。你的任务是【判断有没有必要主动开口】。

只输出一个 JSON 对象：
{"reply": "要对用户说的话（没有必要时留空字符串）", "ops": [ ...仅允许圈注类操作... ], "context": "一句话记录这块画布在画什么（≤30 字）"}

允许的 ops（只允许这三种，别的不许用）：
- {"op":"annotate","shape":"circle","target":"E3","color":"#e03131"}            圈出有问题的元素
- {"op":"annotate","shape":"arrow","target":"E3","text":"这里缺一步校验"}         指向 + 一句话说明
- {"op":"select","targets":["E3"]}                                              选中要讨论的元素
- {"op":"zoom","fit":true}                                                      只在需要把视图拉回内容时用

判断标准（很重要，别打扰用户）：
1) 只有当画布存在【明显的结构性缺口或矛盾】时才开口，例如：流程断了、只有输入没有输出、判断框没有分支、两个框该连没连、命名前后不一致。
2) 用户刚画完 1 个元素、或画布还很空 → 不要开口（reply 留空、ops 空）。
3) 已经在被圈注过的地方不要重复圈。
4) 每次最多圈 1 处、说 1 句话（≤40 字），语气像同事随口提醒，可以用"要不要…？"。
5) 没有问题就返回 {"reply":"","ops":[],"context":"..."}。
6) context 永远要填：用一句话说明这块画布在画什么，供后续对话使用。"""


SYSTEM_INTENT = """判断用户这句话是不是在【要求你操作白板画布】。只输出一个词：command 或 chat。

command = 明确要求你动手：画、加、补、改、删、去掉、移、对齐、连线、圈注、清空、导出、缩放。
chat = 提问、评价、讨论、陈述想法、闲聊、让你分析或解释——**即使内容涉及系统/流程/架构等可以画的东西，也算 chat**。
示例：
"帮我画一个退款流程" → command
"加一个数据库" → command
"把这个框改成红色" → command
"圈出你觉得有问题的地方" → command
"我想做一个电商系统" → chat
"换个思路呢" → chat
"这个流程有点乱" → chat
"你觉得还缺什么" → chat
"改天再说吧" → chat
拿不准 → chat"""

SYSTEM_PARTNER = """你是「AI白板」的搭档，正和用户在同一块画布前一起看图、讨论。用户这一句只是聊天/提问/评价，**不是要你改画布**。

要求：
1) 直接说人话，像同事一样自然、简短（≤80 字），不要输出 JSON、不要输出操作。
2) 可以基于画布元素结构（和截图转述）理解他在讲什么，给出判断或建议。
3) 如果确实值得动手（例如发现了缺口、聊到了新结构），就在最后问一句"要我把这个画出来吗？"，等用户点头。
4) 你看不清他画的是什么就直接问，不要杜撰。"""


SYSTEM_DISCUSSION = """你是一位既懂技术又能写会议纪要的「方案整理者」。下面是一场多人讨论的**全程语音转写**和**白板图形的每一步变化**。
请把它们整理成一份可直接发给团队看的中文方案。

讨论画板：{page}
已有背景（之前确认过的）：{context}

===== 语音转写（按时间）=====
{transcript}

===== 图形变化时间线 =====
{timeline}

===== 讨论结束时画布上的结构 =====
{canvas}

要求：
1. 严格按下面的小标题输出（用 Markdown 的 ##），没有内容的标题可以不写：
   ## 一、讨论背景（1~3 句，说明为什么讨论这件事）
   ## 二、关键结论（3~6 条，每条一句话，写明"决定了什么"）
   ## 三、方案要点（按模块/步骤写，能落到图上的对应白板上的结构）
   ## 四、争议与未决（讨论中没谈拢或悬而未决的点；没有就写"无"）
   ## 五、待办与分工（能用"谁 / 做什么 / 什么时候"就写清；没有明确负责人就写"待认领"）
   ## 六、图上的关键变化（按时间线总结图形是怎么一步步演化的，3~6 条）
2. **只依据给到的材料**，不要脑补不存在的人和事；转写里听不清或明显是语气词的内容不要写进去。
3. 语言要像人写的会议纪要：短句、具体、可执行；不要空话套话（如"加强沟通""高度重视"）。\n   转写里带说话人名字时，**关键结论和待办要写清是谁提出/谁负责**（名字不确定就写"说话人 N"）。
4. 如果转写内容很少或明显是闲聊，就在「讨论背景」里如实说明材料有限，并只给出能确定的结论。
5. 正文写完后，**另起一个 ```json 代码块**，只放待办清单，格式：
```json
{{"todos": [{{"who": "张三", "what": "定库存服务接口", "when": "下周一"}}]}}
```
   who/when 材料里没说就留空字符串；**没有待办就输出 {{"todos": []}}**。这个代码块必须是回复的最后一段。
"""


SYSTEM_PAGE_REVIEW = """你是一位资深的前端 / 视觉设计评审专家。下面是一个网页的**结构分析**（标题层级、地标、交互元素、配色）和**静态检查问题**。
请给出可直接落地的中文评审意见，结构如下（Markdown）：

## 一、总体印象（2~3 句，先说这个页面是干什么的、现在的整体观感）
## 二、信息层级（标题/分区是否清晰，哪里该加强、哪里该合并，2~5 条）
## 三、视觉与配色（配色数量、对比度、间距节奏、字体层级，2~5 条；只依据给到的数据，不要凭空描述颜色）
## 四、交互与可用性（按钮/表单/链接的问题，2~5 条）
## 五、无障碍与规范（按检查清单里实际存在的问题讲，逐条说"怎么改"）
## 六、优先级建议（把这些意见分成 P0 必须改 / P1 建议改 / P2 可选，用 `- [ ] P0 …` 的复选框格式）

要求：
1. **只依据给到的结构和检查结果**（如果附了"白板截图内容"，那部分也可以引用，但要说明依据）；不要编造没给到的视觉细节。
2. 每条意见都要能落到具体元素或具体数值上（例如"有 7 张图片缺 alt"）。
3. 不要空话（"提升用户体验"），要说"把 X 改成 Y"。
"""


SYSTEM_REPORT = """你是一个技术团队的负责人，要基于白板上的内容写一份**中文周报**，发给团队和管理层看。

时间范围：{range}

===== 白板素材（各画板的结构、备注、待办）=====
{material}

严格按下面的结构输出（Markdown，不要输出其它解释）：
## 本周进展（按主题/模块分 2~5 条，每条一句话讲清"做了什么、到什么程度"）
## 关键结论（讨论里定下来的事，3~6 条）
## 待办与分工（用 `- [ ] 谁 · 做什么 · 何时` 的复选框格式；材料里没有负责人就写"待认领"）
## 风险与阻塞（材料里出现的风险/依赖/未决；没有就写"无"）
## 下周计划（2~4 条，能落地）
要求：
1. **只依据素材**，不要编造项目、人名或数据；素材里没有的信息不要出现。
2. 已完成和未完成的待办要分开体现（已完成的在"本周进展"里体现，未完成的进"待办与分工"）。
3. 语言像人写的：短句、具体、可核对；不要"加强协同""高度重视"这类空话。
"""


SYSTEM_DISCUSSION_DIAGRAM = """你是把会议结论"画成图"的专家。下面是一场讨论的语音转写、图形变化时间线和最终画布结构。
请把讨论的结论与行动**画成一张流程图**，只输出一个 JSON 对象（不要任何解释文字、不要 Markdown 代码围栏）。

讨论画板：{page}
已有背景：{context}

===== 语音转写 =====
{transcript}

===== 图形变化时间线 =====
{timeline}

===== 讨论结束时画布上的结构 =====
{canvas}

JSON 结构：
{{"title": "这张图的标题（≤16 字）",
  "direction": "down",
  "nodes": [{{"id": "n1", "label": "节点文字（≤14 字，动宾短语）", "shape": "stadium|rect|round|diamond|cylinder|cloud|note|hexagon", "color": "primary|success|warn|danger|purple|gray"}}],
  "edges": [{{"from": "n1", "to": "n2", "label": "可选：是/否/依赖"}}],
  "notes": []}}

画法要求：
1. 第一个节点是**讨论要解决的问题/目标**（shape=stadium, color=success）；最后一个节点是**交付结果或验收标准**（shape=stadium）。
2. 中间节点是**讨论得出的关键结论与要做的动作**，一步一件事，按先后或依赖排序。
3. 需要拍板/有分歧的地方用 shape=diamond（color=warn），从它出发的边要带 label（如"通过/不通过"）。
4. 涉及的系统/数据/存储用 cylinder，外部依赖用 cloud，待办/提醒用 note。
5. 节点总数 4~10 个；**只依据材料**，材料里没提到的东西不要编。
"""


# ============ 白板工具（agent 的"手"）============
WB_TOOLS = [
    {"type": "function", "function": {
        "name": "get_canvas",
        "description": "读取当前画布上的全部元素（id / 类型 / 坐标 / 文字）。改动画布后想确认结果时调用。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "add_flow",
        "description": "新增一段结构化流程图，自动分层排版（不要传坐标）。流程节点顺序、分支（是/否）、回环都用它。",
        "parameters": {"type": "object", "properties": {
            "layout": {"type": "string", "enum": ["flow-down", "flow-right"], "description": "线性流程用 flow-down，横向布局用 flow-right"},
            "direction": {"type": "string", "enum": ["down", "right"], "description": "同 layout，优先用这个"},
            "nodes": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string", "description": "短 id，供 edges 引用"},
                "label": {"type": "string"},
                "shape": {"type": "string", "enum": ["rect", "round", "stadium", "diamond", "parallelogram",
                                                     "hexagon", "ellipse", "cylinder", "cloud", "document",
                                                     "note", "actor", "package", "triangle", "star"],
                          "description": "开始/结束用 stadium；处理用 rect；判断用 diamond；输入输出用 parallelogram"},
                "color": {"type": "string", "enum": ["primary", "success", "warn", "danger", "purple", "gray"]}},
                "required": ["label"]}},
            "edges": {"type": "array", "items": {"type": "object", "properties": {
                "from": {"type": "string"}, "to": {"type": "string"},
                "label": {"type": "string", "description": "分支标签，如 是/否/重试"},
                "mode": {"type": "string", "enum": ["ortho", "straight", "curve"]}},
                "required": ["from", "to"]}},
            "notes": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}}}}
        }, "required": ["nodes"]}}},
    {"type": "function", "function": {
        "name": "add_architecture",
        "description": "画分层架构图：每层自动生成一个容器框，层内节点横向排布。云/数据库/存储等基础设施图优先用它。",
        "parameters": {"type": "object", "properties": {
            "tiers": {"type": "array", "description": "从上到下排列的层", "items": {"type": "object", "properties": {
                "title": {"type": "string", "description": "层的名字，如 客户端层 / 接入层 / 服务层 / 数据层"},
                "color": {"type": "string"},
                "nodes": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "label": {"type": "string"},
                    "shape": {"type": "string", "enum": ["rect", "round", "stadium", "diamond", "parallelogram",
                                                         "hexagon", "ellipse", "cylinder", "cloud", "document",
                                                         "note", "actor", "package"]},
                    "color": {"type": "string", "enum": ["primary", "success", "warn", "danger", "purple", "gray"]},
                    "stroke": {"type": "string"}, "fill": {"type": "string"}},
                    "required": ["label"]}}},
                "required": ["title"]}},
            "edges": {"type": "array", "items": {"type": "object", "properties": {
                "from": {"type": "string", "description": "节点 label（或 id）"},
                "to": {"type": "string"},
                "label": {"type": "string"},
                "dash": {"type": "string", "enum": ["solid", "dashed"], "description": "异步/可选链路用 dashed"},
                "both": {"type": "boolean", "description": "true=双向箭头"}},
                "required": ["from", "to"]}}
        }, "required": ["tiers"]}}},
    {"type": "function", "function": {
        "name": "add_sequence",
        "description": "画时序图（参与者 + 生命线 + 消息）。用于接口调用顺序、交互过程、请求链路。",
        "parameters": {"type": "object", "properties": {
            "participants": {"type": "array", "description": "从左到右的参与者", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "label": {"type": "string"},
                "shape": {"type": "string", "enum": ["rect", "round", "actor", "cylinder", "hexagon", "cloud", "package"]},
                "color": {"type": "string", "enum": ["primary", "success", "warn", "danger", "purple", "gray"]}},
                "required": ["label"]}},
            "messages": {"type": "array", "description": "按时间顺序从上到下", "items": {"type": "object", "properties": {
                "from": {"type": "string", "description": "参与者 label 或 id"},
                "to": {"type": "string", "description": "同 from 表示自调用（自消息）"},
                "label": {"type": "string"},
                "dashed": {"type": "boolean", "description": "true=返回/异步消息（虚线）"}},
                "required": ["from", "to"]}}
        }, "required": ["participants", "messages"]}}},
    {"type": "function", "function": {
        "name": "add_swimlane",
        "description": "画泳道图（跨角色/部门的流程）：每条泳道是一个容器，泳道内步骤自动依次连线。用于审批流、故障处理、跨部门协作。",
        "parameters": {"type": "object", "properties": {
            "lanes": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string", "description": "泳道名（角色/部门）"},
                "color": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "label": {"type": "string"},
                    "shape": {"type": "string", "enum": ["rect", "round", "stadium", "diamond", "document", "note", "parallelogram"]},
                    "color": {"type": "string", "enum": ["primary", "success", "warn", "danger", "purple", "gray"]}},
                    "required": ["label"]}}},
                "required": ["title"]}},
            "edges": {"type": "array", "description": "跨泳道的额外连线（泳道内已自动连好，不用重复写）", "items": {"type": "object", "properties": {
                "from": {"type": "string"}, "to": {"type": "string"}, "label": {"type": "string"},
                "dash": {"type": "string", "enum": ["solid", "dashed"]}},
                "required": ["from", "to"]}}
        }, "required": ["lanes"]}}},
    {"type": "function", "function": {
        "name": "add_elements",
        "description": "在画布上自由摆放元素（需要精确坐标时用）",
        "parameters": {
            "type": "object",
            "properties": {
                "elements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["rect", "round", "stadium", "diamond", "ellipse",
                                                                "parallelogram", "hexagon", "triangle", "star",
                                                                "cylinder", "cloud", "document", "note", "actor",
                                                                "package", "frame", "text", "arrow"]},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                            "text": {"type": "string"},
                            "strokeColor": {"type": "string"},
                            "fillColor": {"type": "string"}
                        },
                        "required": ["type"]
                    }
                }
            },
            "required": ["elements"]
        }
    }},
    {"type": "function", "function": {
        "name": "update_element",
        "description": "修改已有元素的属性（文字/颜色/粗细/字号/位置/大小）",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "元素 id（画布清单里的编号）"},
            "set": {"type": "object", "properties": {
                "text": {"type": "string"}, "strokeColor": {"type": "string"}, "fillColor": {"type": "string"},
                "strokeWidth": {"type": "number"}, "fontSize": {"type": "number"},
                "x": {"type": "number"}, "y": {"type": "number"},
                "width": {"type": "number"}, "height": {"type": "number"}}}
        }, "required": ["id", "set"]}}},
    {"type": "function", "function": {
        "name": "move_element",
        "description": "相对移动某个元素（像素增量）",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "dx": {"type": "number"}, "dy": {"type": "number"}},
            "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "delete_elements",
        "description": "删除一个或多个元素（相关的箭头会一起清理）",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]}}},
    {"type": "function", "function": {
        "name": "connect_elements",
        "description": "在两个元素之间连一条带箭头的连线（自动贴边、自动绕线，元素移动后连线跟着走）",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "string", "description": "起点元素的 id 或文字标签"},
            "to": {"type": "string", "description": "终点元素的 id 或文字标签"},
            "label": {"type": "string", "description": "线上的标签，如 是/否/异步"},
            "mode": {"type": "string", "enum": ["ortho", "straight", "curve"], "description": "默认 ortho 直角折线"},
            "from_side": {"type": "string", "enum": ["auto", "t", "b", "l", "r"]},
            "to_side": {"type": "string", "enum": ["auto", "t", "b", "l", "r"]},
            "arrow_start": {"type": "boolean", "description": "true=双向箭头"},
            "color": {"type": "string"}},
            "required": ["from", "to"]}}},
    {"type": "function", "function": {
        "name": "frame_elements",
        "description": "把一组元素（或当前选中的元素）用一个带标题的容器框起来，表示模块/层/边界",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}, "description": "留空 = 用当前选中的元素"},
            "title": {"type": "string"},
            "color": {"type": "string"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "auto_layout",
        "description": "一键整理画布：按连线关系分层重新排列元素（有连线时用分层算法，没连线时网格排布）",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["down", "right", "grid"], "description": "down=从上到下，right=从左到右，grid=网格"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "align_elements",
        "description": "把多个元素按某个方向对齐",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["left", "center", "right", "top", "middle", "bottom"]}},
            "required": ["ids", "mode"]}}},
    {"type": "function", "function": {
        "name": "annotate",
        "description": "圈出或指向某个元素并配一句话（用来'指着图说话'）",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"},
            "shape": {"type": "string", "enum": ["circle", "arrow"]},
            "text": {"type": "string"},
            "color": {"type": "string"}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "export_code",
        "description": "把当前画布导出成 Mermaid 或 PlantUML 代码（用户要贴进文档/代码仓库/PR 时用）。返回的 code 你要原样贴给用户。",
        "parameters": {"type": "object", "properties": {
            "format": {"type": "string", "enum": ["mermaid", "plantuml"], "description": "默认 mermaid"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "check_diagram",
        "description": "给当前画布做体检：连线是否穿过了元素、元素是否重叠、判断分支是否缺标签、有没有孤立节点、是否缺开始/结束、交叉是否过多。用户问'这图有没有问题/帮我看看'时用它。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "add_legend",
        "description": "给整张图自动补标题和图例（按画布上实际用到的配色/形状/线型生成说明）。架构图画完后建议调用一次。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "标题文字；不传则用会话背景或'系统架构图'"},
            "shapes": {"type": "boolean", "description": "是否包含形状说明（默认形状种类 >= 3 才加）"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "import_code",
        "description": "把一段图代码画到画布上：Mermaid（flowchart/graph/sequenceDiagram）或 PlantUML（@startuml / actor / rectangle / database / package…），自动识别格式。用户粘贴代码让你画时用它。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "完整的 Mermaid 或 PlantUML 文本"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "import_mermaid",
        "description": "把 Mermaid 流程图代码画到画布上（import_code 的别名，只收 Mermaid 时可用）。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "完整的 Mermaid 流程图文本"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "fix_diagram",
        "description": "一键采纳体检建议：给判断分支补「是/否」标签、给流程补开始/结束节点、按连线重新分层整理。用户说'帮我修一下/按你说的改'时用它。",
        "parameters": {"type": "object", "properties": {
            "only": {"type": "array", "items": {"type": "string", "enum": ["labels", "terminals", "layout"]},
                     "description": "只修这几类；留空=全修"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "page_add",
        "description": "新建一个画板（多画板：每页元素与视口独立）。用户说'再开一张/新起一页画'时用它。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "画板名称"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "page_switch",
        "description": "切换到另一个画板（用户说'回到第一张/切到架构图那页'时用）",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "number", "description": "第几个画板，从 1 开始"},
            "name": {"type": "string", "description": "按名字模糊匹配"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "copy_from_page",
        "description": "把另一个画板的内容复制到当前画板（跨画板复用）。用户说'把第 1 页的架构图复制到这页/复用上一页的图'时用它。",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "number", "description": "源画板序号，从 1 开始"},
            "name": {"type": "string", "description": "或按画板名模糊匹配"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "add_page_ref",
        "description": "在当前画板插入一个「画板引用块」：框里实时显示另一个画板的内容，源画板一改它就跟着变。用户说「这页引用一下第 1 页 / 把架构图挂在这页」时用它。",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "number", "description": "源画板序号，从 1 开始"},
            "name": {"type": "string"},
            "width": {"type": "number"}, "height": {"type": "number"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "compare_pages",
        "description": "把当前画板和另一个画板做结构对比（节点、关系、形状差异），用于检查两页架构/流程描述是否一致。用户问「这两页一致吗 / 对比一下」时用它。",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "number", "description": "要对比的画板序号，从 1 开始"},
            "name": {"type": "string", "description": "或按名称模糊匹配"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "get_pages_outline",
        "description": "读取所有画板的演示大纲（每页的名称、节点清单、关系清单、已有备注）。用户要「演示脚本 / 讲稿 / 汇报要点」时先调用它，再按画板顺序写讲稿。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "set_page_note",
        "description": "把讲稿/备注写到某个画板上（演示时按 N 会显示这页的备注）。写完讲稿后用它对每一页写入 2~4 句。",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "number", "description": "画板序号（1 开始）；不传=当前画板"},
            "name": {"type": "string"},
            "note": {"type": "string", "description": "这一页要讲的话，2~4 句"}},
            "required": ["note"]}}},
    {"type": "function", "function": {
        "name": "clear_canvas",
        "description": "清空画布上的元素（用于'重新画/重画'前先清掉旧内容）。默认连 AI 未采纳的建议一起清。",
        "parameters": {"type": "object", "properties": {
            "keep_suggestions": {"type": "boolean", "description": "true=只清用户画的，保留 AI 建议"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "set_view",
        "description": "控制视口：适应全部内容 / 放大 / 缩小 / 重置",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["fit", "in", "out", "reset"]}}, "required": ["mode"]}}},
    {"type": "function", "function": {
        "name": "undo",
        "description": "撤销上一步操作（你自己刚改错了、或用户说「撤回/撤销/上一步/算了」时用）。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "redo",
        "description": "重做刚才撤销掉的操作（用户说「还是改回来/恢复刚刚那个」时用）。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "page_delete",
        "description": "删除一个画板（用户说「这页不要了/删掉那页/合并成一页后把多余的删了」时用）。至少要留一个画板；删当前页会自动切到相邻一页。",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "number", "description": "画板序号（1 开始）"},
            "name": {"type": "string", "description": "画板名字（可只写一部分）"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "group_elements",
        "description": "把一组元素编成一组：以后拖动/复制它们会一起动（**不画任何框**，外观完全不变）。用户说「把这几块绑在一起/当成一组/组合起来」时用。注意区分：要画一个框把元素圈起来用 frame_elements，不是这个。",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}, "description": "要编组的元素编号，至少 2 个"}},
            "required": ["ids"]}}},
    {"type": "function", "function": {
        "name": "ungroup_elements",
        "description": "解散编组（元素本身保留，只是不再一起动）。用户说「解开/拆开这组/不要绑在一起」时用。",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}, "description": "组内任意元素编号；给 1 个即可解散整组"}},
            "required": ["ids"]}}},
    {"type": "function", "function": {
        "name": "present",
        "description": "进入/退出演示模式（全屏逐页展示）。用户说「开始演示/全屏讲一下/进入汇报模式」用 start，「退出演示」用 exit。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "exit"]},
            "index": {"type": "number", "description": "从第几页开始（1 开始），不传=当前页"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "replay",
        "description": "图形变化时间线的回放：看这块画布是怎么一步步画出来的。用户说「回放一下/看看刚才怎么改的/把过程放一遍」用 start（然后可用 step 跳到某一步），「退出回放」用 stop。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "step", "stop"]},
            "step": {"type": "number", "description": "action=step 时跳到第几步（从 0 开始）"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "discussion",
        "description": "讨论模式（持续录音→转写→整理）：用户说「开始记录讨论 / 记一下我们要说什么 / 开个讨论」用 start；「暂停一下」用 pause；「继续」再 start 不行就 pause 切换；「讨论结束了 / 把它整理成方案」用 summarize（会生成六段式方案）；「把讨论导出成文档」用 export；「看看有哪些待办」用 todos；「把讨论里提到的风险圈到图上」用 risks；「找找说了但图上没有的节点」用 suggest；「别录了」用 stop；想确认现在到底录没录、转写了多少段，用 status。**注意麦克风是异步授权的：调完 start 若想确认，隔一会儿用 status 查一下**。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "pause", "stop", "summarize", "export", "todos", "risks", "suggest", "status"]}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "export_file",
        "description": "把画板导出成文件：png 图片 / svg 矢量图 / json 数据（可再打开）/ pdf 文档。用户说「导出图片/给我一张图」用 png，「导成 PDF」用 pdf。",
        "parameters": {"type": "object", "properties": {
            "format": {"type": "string", "enum": ["png", "svg", "json", "pdf"]}},
            "required": ["format"]}}},
    {"type": "function", "function": {
        "name": "element_order",
        "description": "调整元素层级或锁定：front 置顶 / back 置底 / up 上移一层 / down 下移一层（被别的元素挡住时用）；lock 锁定（锁定后不能被拖动）/ unlock 解锁。",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}, "description": "元素编号"},
            "action": {"type": "string", "enum": ["front", "back", "up", "down", "lock", "unlock"]}},
            "required": ["ids", "action"]}}},
    {"type": "function", "function": {
        "name": "appearance",
        "description": "画布外观：切换深色/浅色主题、网格显示、拖动磁吸。用户说「换个深色」「把网格关掉」「不要磁吸」时用。",
        "parameters": {"type": "object", "properties": {
            "theme": {"type": "string", "enum": ["dark", "light"]},
            "grid": {"type": "boolean"},
            "snap": {"type": "boolean"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "voice",
        "description": "语音朗读与连续对话：read_on/read_off 开关「朗读我的回复」，say 直接念一段话，continuous_on/continuous_off 开关连续语音对话（说完自动回到聆听）。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["read_on", "read_off", "say", "continuous_on", "continuous_off"]},
            "text": {"type": "string", "description": "action=say 时要念的内容"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "duplicate_elements",
        "description": "把已有元素再制一份（副本往右下偏移一点）。要复制几个图形时用它，比重新画一遍快。",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"},
                    "description": "要再制的元素编号（如 E1、E3；先用 get_canvas 看编号）"}},
            "required": ["ids"]}}},
    {"type": "function", "function": {
        "name": "insert_template",
        "description": "插入内置模板（现成的流程图 / 架构图 / 时序图 / 泳道图）。用户说“用模板”“来个架构图模板”时用它。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "模板名关键词，如 基础、审批、微服务"},
            "kind": {"type": "string", "enum": ["flow", "arch", "sequence", "swimlane"],
                     "description": "按类型找模板"},
            "index": {"type": "integer", "description": "也可以直接给模板序号（不知道名字时先用 0 试）"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "todo",
        "description": "管理画板待办：list 列出全部待办（带编号）、add 新增一条、done/undone 标记完成状态、to_page 把待办画到新画板。用户问“有哪些待办”“这条做完了”“记一条待办”时用它。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "add", "done", "undone", "to_page"]},
            "what": {"type": "string", "description": "action=add 时的待办内容"},
            "who": {"type": "string", "description": "负责人（可空）"},
            "when": {"type": "string", "description": "什么时候要，如“下周一”（可空）"},
            "id": {"type": "string", "description": "action=done/undone 的待办编号（先 list 拿）"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "screenshot_page",
        "description": "把某个网页截成整页图贴到画板上（之后可以在图上圈注、让我评审）。用户给一个网址说“截到画板”“看一眼这个页面”时用它。外网站点约 10~30 秒。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "http(s) 网址，或本机文件路径（如 D:\\proj\\index.html）"},
            "height": {"type": "integer",
                       "description": "截图高度像素，默认 2600（首屏 900 / 两屏 1800 / 长图 2600 / 超长 4200）"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "review_page",
        "description": "抓取一个网页并做设计评审，返回评审意见。用户说“帮我评审这个页面”“这个网页有什么问题”时用它。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "http(s) 网址"},
            "html": {"type": "string", "description": "也可以直接给一段 HTML（与 url 二选一）"}},
            "required": []}}},
]

# 附图成功时用它包住视觉转述。为什么要"覆盖旧结论"：
# 用户在修好之前连着问过好几轮，模型自己回答过"我读不到图片内容""图片是黑盒"，
# 这些话留在对话历史里会形成惯性 —— 它看到自己以前的结论，就继续照旧回答，
# 哪怕这一次的视觉转述其实已经把画面内容写全了（实测：转述 696 字、图也发了，
# 模型仍然回"我读不出来"）。所以这里明确告诉它旧结论已作废。
VISION_FRAME = (
    '【你这次真实看到的画面（视觉通道已开启，以下是你已经看到的内容，不是猜测）】\n%s\n'
    '（注意：如果历史对话里出现过"我读不到图片""看不到图片内容""图片对我是黑盒"'
    '之类的话，那是旧版本的限制，现在已经能看到了 —— 不要再声称自己看不到，'
    '直接基于上面的内容回答，也不要要求用户改用文字描述或把图贴到聊天框。）')

SYSTEM_AGENT = """你是「AI白板」的搭档，和用户在【同一块画布】前一起画图、讨论。你不是"画图机器人"，而是一个会用工具的同事。

当前画布元素清单（编号可被工具引用）：
{canvas}

协作上下文（之前确认过的背景）：{context}
画布截图内容（视觉模型转述）：{vision}
注意：上面这段里的【用户标注】（圈、框、箭头、下划线、手写批注）是最重要的信息 —— 用户圈住/指向的地方就是他要你关注的地方，回答时必须针对它，不能视而不见。
整块画板概览（整板缩略图的转述，用来了解画板全貌）：{overview}
{marks}

工作方式：
0) **始终用中文回复**：即使用户用英文提问也要用中文回答；**不要输出英文过渡句**（如 "I'll check the diagram for you." 这类一律不许出现），一句话直接说你要做什么（"我先看一下画布"）。
1) 用户跟你聊天、提问、评价时，就正常回话（像同事，简短自然，≤80 字），不要调用任何工具。
2) 用户要你画、加、补、改、删、移、对齐、连线、圈注、调整视图时，调用相应工具完成；可以一次调多个。
3) 只有用户真的要求动手、或你确实需要看图现状时才调用工具；不确定就先问一句"要我把这个画出来吗？"。
4) 调用工具后，用一句话告诉用户你做了什么（例："已把'下单'和'支付'连起来，并把库存校验插在中间"）。
5) 新增结构化流程图用 add_flow（自动分层排版，不要传坐标）；画分层架构/部署/云服务拓扑用 add_architecture（每层自动生成容器框）；**接口调用顺序、请求链路、交互过程用时序图 add_sequence**；**跨角色/跨部门的流程用时序图之外的泳道图 add_swimlane**；改已有元素用 update_element / move_element / delete_elements（引用清单里的编号）；连线用 connect_elements（可带 label / mode / from_side / to_side）；要框住一组元素用 frame_elements；要重排整张图用 auto_layout。
6) 说话要像搭档：发现问题可以主动圈出来（annotate）并问一句要不要补。
7) 看不清用户画的是什么就直接问，不要杜撰。
8) 用户要"导出代码 / 贴到文档里 / 给我 Mermaid"时用 export_code，把返回的 code 原样贴出来（用代码块）；用户问"这张图有没有问题 / 帮我检查一下"时用 check_diagram，再把问题用大白话总结一遍并问要不要修。
9) 架构图画完可以调 add_legend 补上标题与图例；用户粘贴 Mermaid **或 PlantUML** 代码让你"画出来"时用 import_code（自动识别格式）。
10) 用户说"整理一下 / 太乱了 / 排一排 / 排版"这类话时，**直接调用 auto_layout**（默认 down），不要反问他按什么关系整理；只有画布为空时才问。
11) 这块白板支持**多画板**（页面）与**画板引用块**：用户说"再开一张/新起一页"用 page_add，"切回第 N 张/切到 XX 那页"用 page_switch，"把第 1 页的图复制过来/复用上一页"用 copy_from_page，用户提到的是**另一个画板**里的东西（如"架构图那页的网关"）时，**先 page_switch 切过去再操作**，"这页挂一个第 1 页的引用块（要跟着变）"用 add_page_ref，"这两页一致吗/对比一下"用 compare_pages。画布清单里会给出画板列表（序号/名称/元素数/当前是哪个）。你的所有工具都只作用于**当前画板**。用户说"帮我修一下/就按你说的改"时用 fix_diagram。\n12) 用户要「演示脚本 / 讲稿 / 汇报要点」时：先 get_pages_outline 拿到各页大纲，然后**按画板顺序逐页写 2~4 句口语化讲稿**，并用 set_page_note 写回每一页（演示时按 N 即可看到）；最后在聊天里给出完整讲稿。
13) 你还能操作「画板本身」和「展示」：撤掉上一步用 undo、恢复用 redo；删掉多余的画板用 page_delete（至少要留一页）；把几块元素**绑成一组一起动**（不画框）用 group_elements、拆开用 ungroup_elements，要**画个框圈起来**才用 frame_elements；要全屏讲用 present；用户想看「这块图是怎么一步步长出来的」就用 replay。用户说「撤回 / 这页不要了 / 把这几块绑一起 / 开始演示 / 回放一下」时**直接调工具**，不要反问。
14) **别把「开始演示」当成「写讲稿」**：用户说「开始演示 / 全屏讲一遍 / 进入汇报模式」= 立刻调用 present(start)；只有说「写演示讲稿 / 演示脚本 / 汇报要点」时才走上面第 12 条去写备注。两件事经常被一起提，先做用户当轮明确要的那件。
15) 讨论模式、导出文件、层级锁定、外观、朗读也都能由你操作：`discussion`（开始/暂停/整理成方案/导出/待办/圈风险/建议节点）、`export_file`（png/svg/json/pdf）、`element_order`（置顶置底/锁定）、`appearance`（深色/网格/磁吸）、`voice`（朗读开关/念一段/连续对话）。用户提到这些就直接调工具，不要让人自己去点面板。

【图形词表（按语义选形状，这是画好看的关键）】- 开始 / 结束 / 终止：stadium；普通步骤 / 处理 / 服务 / 模块：rect 或 round
- 判断 / 条件 / 是否：diamond；输入 / 输出 / 数据流：parallelogram
- 准备 / 预处理：hexagon；文档 / 报表 / 日志：document；备注 / 待办：note
- 数据库 / 缓存 / 队列 / 消息中间件：cylinder；云 / 外部系统 / 第三方：cloud
- 角色 / 用户 / 参与者：actor；包 / 分组模块 / 命名空间：package
- 强调或起点主题：ellipse；重点标记：star；容器 / 层 / 边界：frame
- 配色语义：起止 success（绿）、判断 warn（黄）、处理 primary（蓝）、异常 danger（红）、数据/中间件 purple、次要 gray。

【流程图的规矩】
- 必须有明确的入口（开始）与出口（结束）；主干从上到下（direction=down）。
- 分支从 diamond 出发，边必须带 label："是/否"、"成功/失败"；失败或重试的回流边用 mode=ortho。
- 一步只做一件事，节点文字用动宾短语（"校验库存"而不是"库存校验逻辑处理"）。

【架构图的规矩】
- 用 add_architecture 分层：客户端层 → 接入层（网关/CDN）→ 服务层 → 中间件层 → 数据层，层的名字写在 title 里。
- 基础设施用 cylinder（DB/缓存/队列）、cloud（外部服务）、hexagon（网关/负载均衡）、document（日志/报表）、actor（用户）。
- 跨层连线带方向；异步 / 消息 / 可选链路用 dash=dashed 并写 label（如"异步"、"订阅"）。
- 不要把所有东西排成一条直线：同层内是并列关系，跨层才是依赖关系。

【时序图的规矩】
- 参与者横排在顶部（actor=人、rect=系统/服务、cylinder=存储、hexagon=网关），消息按时间从上到下。
- 返回 / 异步 / 回调消息用 dashed=true；自调用（重试、内部处理）把 to 写成与 from 相同。
- 消息文字用"动词+宾语"（"提交订单"、"查询会话"），不要写成长句。

【泳道图的规矩】
- 每条泳道是一个角色 / 部门 / 系统；泳道内的步骤按从左到右的时间顺序写，系统会自动连线。
- 只有跨泳道的跳转才写进 edges（如"驳回并说明"→"收到结果"），并给 edge 加 label（是/否）。
- 泳道一般 2~5 条，每条 2~5 步；步骤文字用动宾短语。

【最重要的两条纪律】
A) 画布内容的唯一来源是【画布上已有的图形】或【用户这一句明确说出的结构】。
   **绝对不要把你和用户的聊天记录、对话文字、你的回复内容画到画布上。**
   只有用户明确说"把这段话写在画布上 / 加一段文字说明"时，才可以用 add_elements 添加 text 元素。
B) 用户说"重新画 / 重画 / 整理一下 / 规范一下 / 弄好看点"这类**没指明对象**的话时：
   1. 先调用 get_canvas 看清画布上现在有什么；
   2. 以【画布上的现有内容】为唯一依据，用 clear_canvas 清掉旧内容，再用 add_flow 重画一版；
   3. 如果画布是空的、或你无法从画布判断他想重画什么 → **不要画**，回一句"你想重画哪一块？"并说清你看到的现状。"""


# ============ 网页审阅：HTML 结构分析与静态检查（只用标准库）============
def strip_tags(t):
    # 先丢掉"没有闭合 >"的半截标签，再丢掉完整标签。
    # 只删完整标签是不够的：标题写成 `<img src=x onerror=...`（结尾没有 >）就能原样留下来，
    # 之后被前端拼进 innerHTML 就成了脚本。
    t = re.sub(r'<[^>]*$', ' ', t or '')
    t = re.sub(r'<[^>]+>', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def load_rules(profile=None):
    """网页审阅的可配置规则（团队规范）
    支持分环境：{"active":"dev","profiles":{"dev":{...},"prod":{...}}}
    也兼容旧的扁平格式（整份当默认 profile）。profile 参数可临时指定环境。
    """
    try:
        with open(RULES_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get('profiles'), dict):
        name = profile or data.get('active') or 'dev'
        prof = data['profiles'].get(name)
        if not isinstance(prof, dict) and data['profiles']:
            prof = list(data['profiles'].values())[0]     # 名字对不上就用第一个
        return prof or {}
    return data


def save_rules(rules, profile=None):
    """保存某个环境的规则；没有 profiles 结构就整体覆盖"""
    try:
        existing = {}
        try:
            with open(RULES_PATH, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except Exception:
            existing = {}
        if not profile and isinstance(existing, dict) and isinstance(existing.get('profiles'), dict):
            profile = existing.get('active') or 'dev'
        if profile:
            profiles = dict((existing.get('profiles') or {})) if isinstance(existing, dict) else {}
            profiles[profile] = rules
            data = {'active': profile, 'profiles': profiles}
        else:
            data = rules
        with open(RULES_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f'[RULES] 保存失败: {e}', file=sys.stderr, flush=True)
        return False


def rules_meta():
    """返回 {active, profiles:[名字...], flat:bool}"""
    try:
        with open(RULES_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {'active': '', 'profiles': [], 'flat': True}
    if isinstance(data, dict) and isinstance(data.get('profiles'), dict):
        return {'active': data.get('active') or 'dev', 'profiles': list(data['profiles'].keys()), 'flat': False}
    return {'active': '', 'profiles': [], 'flat': True}


def analyze_html(html, source='', rules=None):
    """把一个页面的 HTML 拆成结构大纲 + 静态检查问题 + 配色统计"""
    info = {'source': source, 'title': '', 'lang': '', 'viewport': False, 'description': '',
            'outline': [], 'landmarks': {}, 'counts': {}, 'palette': [], 'checks': []}
    m = re.search(r'<title[^>]*>([\s\S]*?)</title>', html, re.I)
    if m:
        info['title'] = strip_tags(m.group(1))[:120]
    m = re.search(r'<html[^>]*\blang\s*=\s*["\']?([^"\'>\s]+)', html, re.I)
    if m:
        info['lang'] = m.group(1)
    info['viewport'] = bool(re.search(r'<meta[^>]+name\s*=\s*["\']viewport', html, re.I))
    # 结构/属性/配色只在“去掉脚本与样式”的正文里找，否则会把 JS 代码当成 HTML
    body = re.sub(r'<(script|style)\b[\s\S]*?</\1\s*>', ' ', html, flags=re.I)
    body = re.sub(r'<!--[\s\S]*?-->', ' ', body)
    m = re.search(r'<meta[^>]+name\s*=\s*["\']description["\'][^>]*content\s*=\s*["\']([^"\']*)', html, re.I)
    if m:
        info['description'] = m.group(1)[:200]

    # 标题层级（按出现顺序）
    for hm in re.finditer(r'<(h[1-6])\b([^>]*)>([\s\S]*?)</\1>', body, re.I):
        text = strip_tags(hm.group(3))[:80]
        if not text:
            continue
        attrs = hm.group(2) or ''
        hid = re.search(r"\bid\s*=\s*[\"']?([^\"'>\s]+)", attrs, re.I)
        hcls = re.search(r"\bclass\s*=\s*[\"']([^\"']+)", attrs, re.I)
        info['outline'].append({
            'level': int(hm.group(1)[1]), 'text': text,
            'id': (hid.group(1) if hid else '')[:40],
            'cls': (hcls.group(1) if hcls else '')[:60]
        })

    # 地标与元素计数
    for tag in ('header', 'nav', 'main', 'aside', 'footer', 'section', 'article', 'form', 'table'):
        info['landmarks'][tag] = len(re.findall(r'<%s\b' % tag, body, re.I))
    imgs = re.findall(r'<img\b[^>]*>', body, re.I)
    links = re.findall(r'<a\b[^>]*>([\s\S]*?)</a>', body, re.I)
    buttons = re.findall(r'<button\b[^>]*>([\s\S]*?)</button>', body, re.I)
    inputs = re.findall(r'<(?:input|select|textarea)\b[^>]*>', body, re.I)
    info['counts'] = {
        'headings': len(info['outline']), 'images': len(imgs), 'links': len(links),
        'buttons': len(buttons), 'inputs': len(inputs),
        'inline_styles': len(re.findall(r'style\s*=\s*["\']', body, re.I)),
        'scripts': len(re.findall(r'<script\b', html, re.I)),
        'css_blocks': len(re.findall(r'<style\b', html, re.I)),
    }

    # 配色（十六进制色值出现频次）
    colors = {}
    for cm in re.finditer(r'#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b', body):
        c = '#' + cm.group(1).lower()
        if c in ('#000', '#000000', '#fff', '#ffffff'):
            continue
        colors[c] = colors.get(c, 0) + 1
    info['palette'] = [{'color': k, 'count': v} for k, v in
                       sorted(colors.items(), key=lambda kv: -kv[1])[:10]]

    def add(level, kind, msg, fix=''):
        info['checks'].append({'level': level, 'kind': kind, 'msg': msg, 'fix': fix})

    # ---- 静态检查 ----
    if not info['lang']:
        add('warn', 'lang', '根元素没有 lang 属性（屏幕阅读器与翻译会受影响）',
            '把 <html> 改成 <html lang="zh-CN">')
    if not info['viewport']:
        add('warn', 'viewport', '缺少 viewport meta（手机上会按桌面宽度缩放）',
            '在 <head> 里加 <meta name="viewport" content="width=device-width, initial-scale=1">')
    if not info['description']:
        add('info', 'seo', '缺少 meta description（分享/搜索时没有摘要）',
            '在 <head> 里加 <meta name="description" content="一句话说明这个页面">')
    no_alt = [i for i in imgs if not re.search(r'\balt\s*=', i, re.I)]
    if no_alt:
        add('error', 'a11y', '%d 张图片没有 alt（共 %d 张）' % (len(no_alt), len(imgs)),
            '给每个 <img> 补 alt="图片说明"；纯装饰图写 alt=""（空 alt 才是正确的装饰写法）')
    h1 = [o for o in info['outline'] if o['level'] == 1]
    if not info['outline']:
        add('warn', 'structure', '页面没有任何 h1~h6 标题（结构无法被理解）',
            '给每个内容区块加语义标题：主标题 <h1>、分区 <h2>、子项 <h3>')
    elif not h1:
        add('warn', 'structure', '没有 h1，页面缺少主标题',
            '把最重要的一行标题改成 <h1>（每页只保留一个）')
    elif len(h1) > 1:
        add('info', 'structure', '有 %d 个 h1（一般建议只保留一个）' % len(h1),
            '除主标题外的 h1 降级为 <h2>')
    prev = 0
    for o in info['outline']:
        if prev and o['level'] > prev + 1:
            add('warn', 'structure', '标题层级跳跃：h%d 后面直接是 h%d（「%s」）' % (prev, o['level'], o['text'][:20]),
                '把「%s」改成 h%d，或在它前面补一个 h%d' % (o['text'][:20], prev + 1, prev + 1))
            break
        prev = o['level']
    empty_links = [t for t in links if not strip_tags(t) and 'aria-label' not in t]
    if empty_links:
        add('warn', 'a11y', '有 %d 个链接没有文字（也看不出用途）' % len(empty_links),
            '给纯图标链接加 aria-label="用途"，或补上可见文字')
    empty_btns = [t for t in buttons if not strip_tags(t)]
    if empty_btns:
        add('warn', 'a11y', '有 %d 个按钮没有文字（图标按钮建议加 aria-label）' % len(empty_btns),
            '给按钮加 aria-label="动作名"，或放一个 <span class="sr-only">文字</span>')
    unlabeled = 0
    for i in inputs:
        if re.search(r'type\s*=\s*["\'](hidden|submit|button|reset)["\']', i, re.I):
            continue
        if not re.search(r'(aria-label|aria-labelledby|placeholder|title)\s*=', i, re.I):
            unlabeled += 1
    if unlabeled:
        add('warn', 'a11y', '有 %d 个表单控件没有标签或占位说明' % unlabeled,
            '每个控件配 <label for="id">，或用 aria-label；placeholder 不能替代标签')
    ids = re.findall(r"\bid\s*=\s*[\"']?([^\"'>\s]+)", body, re.I)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        add('error', 'html', '有重复 id：' + '、'.join(dup[:5]),
            'id 必须唯一：重命名其中一组，或用 class 代替')
    if re.search(r'target\s*=\s*["\']_blank["\']', body, re.I) and not re.search(r'rel\s*=\s*["\'][^"\']*noopener', body, re.I):
        add('info', 'security', '有 target="_blank" 的链接但没加 rel="noopener"',
            '给这些链接加 rel="noopener noreferrer"')
    if not info['landmarks'].get('main') and info['counts']['headings'] >= 3:
        add('info', 'structure', '没有 <main>，主体内容缺少语义标记',
            '用 <main> 包住主体内容（每页只有一个）')
    if not info['landmarks'].get('nav') and info['counts']['links'] >= 6:
        add('info', 'structure', '链接较多但没有 <nav> 包裹导航',
            '把导航链接包进 <nav>，并加 aria-label 说明是哪类导航')
    if info['counts']['inline_styles'] >= 15:
        add('info', 'style', '内联 style 有 %d 处（建议抽到样式表里）' % info['counts']['inline_styles'],
            '把这些 style 抽成 class，统一放到样式表')
    grays = [c for c in info['palette'] if c['color'].endswith('f') and len(c['color']) == 4]
    if len(info['palette']) >= 12:
        add('info', 'style', '页面里出现了 %d 种颜色（配色偏杂，建议收敛成一套）' % len(info['palette']),
            '把相近颜色合并成 3~5 个设计变量（CSS 变量）')
    # ---- 团队规范（可配置规则）----
    if rules:
        allowed = [str(c).lower() for c in (rules.get('palette') or [])]
        if allowed:
            bad = [p['color'] for p in info['palette'] if p['color'] not in allowed]
            if bad:
                add('error', 'rule', '配色超出规范色板：%s（允许：%s）' % ('、'.join(bad[:6]), '、'.join(allowed[:8])),
                    '把超出色板的颜色换成规范里的颜色，或把新色登记进规则')
        mc = rules.get('max_colors')
        if isinstance(mc, int) and mc > 0 and len(info['palette']) > mc:
            add('warn', 'rule', '页面用了 %d 种颜色，超过规范上限 %d' % (len(info['palette']), mc),
                '把配色收敛到 %d 种以内（同类颜色合并）' % mc)
        mis = rules.get('max_inline_styles')
        if isinstance(mis, int) and info['counts'].get('inline_styles', 0) > mis:
            add('warn', 'rule', '内联 style %d 处，超过规范上限 %d' % (info['counts']['inline_styles'], mis),
                '把内联样式抽到样式表，或改用 class')
        for tag in (rules.get('banned_tags') or []):
            tag = str(tag).strip().lower()
            if not tag:
                continue
            n = len(re.findall(r'<%s\b' % re.escape(tag), body, re.I))
            if n:
                add('error', 'rule', '使用了禁用标签 <%s>（%d 处）' % (tag, n),
                    '用语义化的替代标签重写（如 <center> → CSS text-align）')
        if rules.get('require_viewport') and not info['viewport']:
            add('error', 'rule', '规范要求必须有 viewport meta', '加 <meta name="viewport" content="width=device-width, initial-scale=1">')
        if rules.get('require_main') and not info['landmarks'].get('main'):
            add('error', 'rule', '规范要求必须有 <main>', '用 <main> 包住主体内容')
        if rules.get('require_description') and not info['description']:
            add('error', 'rule', '规范要求必须有 meta description', '在 <head> 加 <meta name="description" content="...">')
        must = [str(t) for t in (rules.get('must_have_text') or []) if str(t).strip()]
        for t in must:
            if t not in body:
                add('warn', 'rule', '规范要求页面上必须有「%s」，没找到' % t[:20], '补上这段文字或调整规则')
        info['rules_applied'] = True
    return info


def format_page_info(info):
    """把结构分析转成给模型看的紧凑文本"""
    L = []
    L.append('标题：' + (info.get('title') or '（无）'))
    L.append('lang：' + (info.get('lang') or '（无）') + '　viewport：' + ('有' if info.get('viewport') else '无'))
    c = info.get('counts') or {}
    L.append('计数：标题 %s / 图片 %s / 链接 %s / 按钮 %s / 表单控件 %s / 内联样式 %s' % (
        c.get('headings', 0), c.get('images', 0), c.get('links', 0),
        c.get('buttons', 0), c.get('inputs', 0), c.get('inline_styles', 0)))
    lm = info.get('landmarks') or {}
    L.append('地标：' + '、'.join('%s×%d' % (k, v) for k, v in lm.items() if v) or '（无地标）')
    pal = info.get('palette') or []
    if pal:
        L.append('配色（出现次数）：' + '、'.join('%s×%d' % (p['color'], p['count']) for p in pal))
    L.append('')
    L.append('标题层级：')
    for o in (info.get('outline') or [])[:40]:
        sel = ('#' + o['id']) if o.get('id') else (('.' + (o.get('cls') or '').split()[0]) if o.get('cls') else '')
        L.append('  ' + '  ' * (o['level'] - 1) + 'h%d %s%s' % (o['level'], o['text'], ('　<' + sel + '>') if sel else ''))
    L.append('')
    checks = info.get('checks') or []
    L.append('静态检查问题（%d 条）：' % len(checks))
    for ch in checks:
        L.append('  [%s] %s' % (ch['level'], ch['msg']))
    if not checks:
        L.append('  （没有发现明显问题）')
    return '\n'.join(L)


def describe_marks(canvas):
    """底图信息 + 用户标注（相对底图的位置）。

    两个作用：
      1) 标注本来就是画布上的一等元素，位置确定已知 —— 直接算给模型，
         比让它看图猜稳得多（视觉模型经常漏掉细圈，或把圈画一笔带过）。
      2) 告诉模型"底图是哪个网页"：浏览器截图要按网页评审的角度分析
         （信息层级、布局对齐、按钮状态、交互可用性），而不是当普通插图。
    """
    els = (canvas or {}).get('elements') or []
    imgs = [e for e in els if e.get('type') == 'image']
    if not imgs:
        return ''
    focus_id = str((canvas or {}).get('focusImageId') or '')
    img = None
    if focus_id:
        for e in imgs:
            if str(e.get('id')) == focus_id:
                img = e
                break
    if img is None:
        img = imgs[0]
    try:
        ix, iy = float(img.get('x') or 0), float(img.get('y') or 0)
        iw, ih = float(img.get('width') or 0), float(img.get('height') or 0)
    except Exception:
        return ''
    if iw <= 0 or ih <= 0:
        return ''

    lines = ['【底图与用户标注（坐标算出来的，必定准确）】']
    url = str(img.get('pageUrl') or '').strip()
    if url:
        lines.append('底图 %s 是【浏览器网页截图】，来源：%s' % (img.get('id') or '图片', url[:160]))
        lines.append('用户在这张网页截图上的圈注 = 他对这个网页的评审意见。请按网页的角度分析：'
                     '信息层级、布局对齐、按钮与状态、交互可用性、文案，不要当成普通插图。')
    else:
        lines.append('底图 %s（图片），画面范围 x %.0f~%.0f、y %.0f~%.0f' % (
            img.get('id') or '图片', ix, ix + iw, iy, iy + ih))
    if len(imgs) > 1:
        lines.append('画布上共有 %d 张图片，本次针对的是 %s。' % (len(imgs), img.get('id') or '其中一张'))

    MARK = ('pen', 'ellipse', 'rect', 'arrow', 'line', 'annotate', 'brush', 'highlight', 'text')
    marks = []
    for e in els:
        if e.get('type') not in MARK or e.get('ai'):
            continue
        if e.get('type') == 'text' and not str(e.get('text') or '').strip():
            continue
        marks.append(e)
    for k, m in enumerate(marks[:12], 1):
        try:
            mx, my = float(m.get('x') or 0), float(m.get('y') or 0)
            mw, mh = float(m.get('width') or 0), float(m.get('height') or 0)
        except Exception:
            continue
        cxr = (mx + mw / 2 - ix) / iw * 100
        cyr = (my + mh / 2 - iy) / ih * 100
        inside = (-10 <= cxr <= 110) and (-10 <= cyr <= 110)
        desc = '标注%d：%s，中心在底图横向 %.0f%%、纵向 %.0f%% 处' % (k, m.get('type'), cxr, cyr)
        if m.get('text'):
            desc += '，内容「%s」' % str(m.get('text'))[:30]
        desc += '，%s' % ('落在截图内' if inside else '落在截图外（不在截图上）')
        lines.append('- ' + desc)
    if marks:
        lines.append('回答时必须先针对这些标注所在位置的内容作答，一个都不要忽略。')
    else:
        lines.append('（这张底图上目前没有用户画的标注）')
    return '\n'.join(lines)


def describe_canvas(canvas):
    """把画布元素转成给模型看的紧凑文本（含多画板清单）"""
    canvas = canvas or {}
    elements = canvas.get('elements', []) or []
    pages = canvas.get('pages') or []
    page_line = ''
    if len(pages) > 1:
        parts = []
        for i, p in enumerate(pages):
            sample = [str(x) for x in (p.get('sample') or [])][:5]
            parts.append('%d.%s(%d个元素%s%s)' % (
                p.get('index', i + 1), p.get('name', '?'), p.get('count', 0),
                '，当前' if p.get('current') else '',
                '：' + '/'.join(sample) if sample else ''))
        page_line = '本白板共 %d 个画板：' % len(pages) + '、'.join(parts) + '\n'
    if not elements:
        return page_line + '（当前画板为空）', {}
    counts = {}
    for el in elements:              # 统计覆盖全部元素，不再只看前 80 个
        t = el.get('type', '?')
        counts[t] = counts.get(t, 0) + 1
    head = f"共 {len(elements)} 个元素：" + "、".join(
        f"{TYPE_CN.get(t, t)}×{c}" for t, c in counts.items())
    lines = []
    token_map = {}   # 形如 {"E1": "真实元素id"} —— 模型用 E 编号引用已有元素
    # 图片（底图）和用户标注绝不能被数量上限挤掉：AI 明确反馈过
    # "清单里图片那条被截断在最后，我拿不到它的坐标" —— 就是被 [:40] 切掉的。
    # 元素多的时候，先列底图与标注，其余按原顺序补位。
    # 底图排最前（第 1 行一定是它），其次是"真的像标注"的那几类，最后才是普通图形。
    # 只把 image 和 rect 放同一档是不够的：矩形一多，图片照样被挤到 60 名之外。
    def _rank(el):
        t = el.get('type')
        if t == 'image':
            return 0
        if t in ('pen', 'ellipse', 'arrow', 'annotate', 'brush', 'highlight'):
            return 1
        return 2
    order = sorted(range(len(elements)), key=lambda i: (_rank(elements[i]), i))
    LIMIT = 80
    for i in order[:LIMIT]:
        el = elements[i]
        tok = 'E%d' % (i + 1)
        token_map[tok] = str(el.get('id') or tok)
        t = TYPE_CN.get(el.get('type', '?'), el.get('type', '?'))
        x, y = _num(el.get('x'), 0), _num(el.get('y'), 0)
        w, h = _num(el.get('width'), 0), _num(el.get('height'), 0)
        txt = (el.get('text') or '').strip()
        seg = f"[{tok}] {t} @({x:.0f},{y:.0f}) {w:.0f}×{h:.0f}"
        if txt:
            seg += f" 文字“{txt[:40]}”"
        if el.get('_aiSuggestion'):
            seg += "（AI建议，未采纳）"
        lines.append(seg)
    if len(elements) > LIMIT:
        rest = {}
        shown = set(order[:LIMIT])
        for ii, el in enumerate(elements):
            if ii in shown:
                continue
            t = el.get('type', '?')
            rest[t] = rest.get(t, 0) + 1
        lines.append('（还有 %d 个元素没逐条列出，按类型是：%s。'
                     '需要看某个具体元素时，按类型或文字描述它）' % (
                         len(elements) - LIMIT,
                         '、'.join('%s×%d' % (TYPE_CN.get(t, t), c) for t, c in rest.items())))
    return page_line + head + "\n" + "\n".join(lines), token_map


def sanitize_messages(messages):
    """清洗历史，保证符合 OpenAI/DeepSeek 的消息序列约束：
       - tool 消息必须紧跟在带 tool_calls 的 assistant 之后（否则丢弃孤儿）
       - 带 tool_calls 的 assistant 若没有任何 tool 响应，则整条丢弃
    """
    cleaned = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'user':
            cleaned.append({'role': 'user', 'content': str(m.get('content') or '')[:4000]})
        elif role == 'assistant':
            item = {'role': 'assistant', 'content': m.get('content') or ''}
            if m.get('tool_calls'):
                item['tool_calls'] = m['tool_calls']
            cleaned.append(item)
        elif role == 'tool':
            # 只接受紧跟 assistant(tool_calls) 或其它 tool 之后的
            if cleaned and cleaned[-1].get('role') in ('assistant', 'tool') and (
                    cleaned[-1].get('role') == 'tool' or cleaned[-1].get('tool_calls')):
                # 这里原来截到 2000 字符：画布元素一多，get_canvas 的 JSON 立刻超限，
                # 而图片是最后加入的、排在数组末尾 —— 正好被切掉，AI 于是"拿不到图片的坐标"
                # （用户原话："清单里图片那条被截断在最后"）。放宽到 12000。
                cleaned.append({'role': 'tool',
                                'tool_call_id': str(m.get('tool_call_id') or ''),
                                'content': str(m.get('content') or '')[:12000]})
            else:
                print('[AGENT] 丢弃孤儿 tool 消息', file=sys.stderr, flush=True)
    # 丢掉"有 tool_calls 但没有任何 tool 响应"的 assistant
    out = []
    for i, m in enumerate(cleaned):
        if m.get('role') == 'assistant' and m.get('tool_calls'):
            has = (i + 1 < len(cleaned) and cleaned[i + 1].get('role') == 'tool')
            if not has:
                print('[AGENT] 丢弃没有工具响应的 assistant(tool_calls)', file=sys.stderr, flush=True)
                continue
        out.append(m)
    # 保证第一条是 user（系统消息另加）
    while out and out[0].get('role') != 'user':
        out.pop(0)
    return out


def call_llm(messages, model, timeout=45, base_url=None, api_key=None, tools=None, raw=False):
    """调用 OpenAI 兼容 /chat/completions（base_url/api_key 可覆盖；tools=函数调用；raw=True 返回完整 message）"""
    base_url = (base_url or CONFIG.get('base_url') or '').rstrip('/')
    api_key = api_key or CONFIG.get('api_key') or ''
    if not api_key:
        raise RuntimeError('未配置 API Key：请设置环境变量 AI_API_KEY，或写入 ai_config.json')
    url = base_url + '/chat/completions'
    payload_obj = {
        'model': model,
        'messages': messages,
        'temperature': 0.4,
        # 2000 太小：tool_calls 的 arguments 是长 JSON，被截断后 json.loads 失败、
        # 会静默降级成空参数执行（用户只看到 xxx✗0）。放宽到 4000。
        'max_tokens': 4000,
    }
    if tools:
        payload_obj['tools'] = tools
        payload_obj['tool_choice'] = 'auto'
    payload = json.dumps(payload_obj).encode('utf-8')
    req = urllib.request.Request(url, data=payload, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + api_key)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as he:
        detail = ''
        try:
            detail = he.read().decode('utf-8')[:400]
        except Exception:
            pass
        raise RuntimeError(f'HTTP {he.code}：{detail}')
    msg = data['choices'][0]['message']
    if raw:
        return msg
    return (msg.get('content') or '').strip()


def extract_json(text):
    """从模型输出里稳健地取出 JSON 对象"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    try:
        return json.loads(s)
    except Exception:
        pass
    start = s.find('{')
    end = s.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None
    return None


def _num(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def normalize_elements(raw):
    """把模型给的元素清洗成前端能吃的结构"""
    out = []
    if not isinstance(raw, list):
        return out
    for el in raw[:40]:
        if not isinstance(el, dict):
            continue
        t = str(el.get('type', 'rect')).lower()
        if t not in ELEMENT_TYPES:
            t = 'rect'
        item = {
            'type': t,
            'x': _num(el.get('x'), 220),
            'y': _num(el.get('y'), 180),
            'width': _num(el.get('width', el.get('w')), 160),
            'height': _num(el.get('height', el.get('h')), 80),
            'text': str(el.get('text') or ''),
            'strokeColor': str(el.get('strokeColor') or '#1971c2'),
            'fillColor': str(el.get('fillColor') or '#dbe4ff'),
        }
        if t == 'text':
            item['width'] = _num(el.get('width'), max(60, 14 * max(1, len(item['text']))))
            item['height'] = _num(el.get('height'), 40)
            item['fillColor'] = 'transparent'
        if t in ('line', 'arrow', 'pen'):
            pts = el.get('points')
            if isinstance(pts, list) and pts:
                item['points'] = pts
        out.append(item)
    return out


def template_draw(description):
    """原模板引擎（保留作为兜底）"""
    desc = (description or '').lower()
    elements = []

    def make_flow(steps, start_x=220, start_y=200, box_w=160, box_h=80, gap=60):
        colors = [('#1971c2', '#dbe4ff'), ('#2f9e44', '#d3f9d8'),
                  ('#e03131', '#ffc9c9'), ('#f08c00', '#ffec99'),
                  ('#9c36b5', '#eebefa')]
        els, x = [], start_x
        for i, step in enumerate(steps):
            sc, fc = colors[i % len(colors)]
            els.append({'type': 'rect', 'x': x, 'y': start_y, 'width': box_w,
                        'height': box_h, 'strokeColor': sc, 'fillColor': fc, 'text': step})
            if i < len(steps) - 1:
                els.append({'type': 'arrow', 'x': x + box_w, 'y': start_y + box_h // 2,
                            'width': gap, 'height': 0})
            x += box_w + gap
        return els

    if '登录' in desc:
        elements = make_flow(['用户输入', '密码验证', '登录成功'])
    elif '注册' in desc:
        elements = make_flow(['填写信息', '邮箱验证', '注册成功'])
    elif '订单' in desc:
        elements = make_flow(['创建订单', '支付', '确认', '完成'])
    elif '流程' in desc or '步骤' in desc:
        parts = re.split(r'[，,、\s]+', description)
        steps = [p.strip() for p in parts if 1 < len(p.strip()) < 10][:5]
        elements = make_flow(steps if len(steps) >= 2 else ['开始', '处理', '结束'])
    elif '架构' in desc or '系统' in desc:
        elements = make_flow(['用户端', '服务A', '服务B', '数据库'])
    elif any(w in desc for w in ['矩形', '方形']):
        nums = re.findall(r'(\d+)', desc)
        count = min(int(nums[0]), 8) if nums else 2
        elements = make_flow([f'矩形{i+1}' for i in range(count)], box_w=120, gap=40)
    elif any(w in desc for w in ['圆', '椭圆']):
        nums = re.findall(r'(\d+)', desc)
        count = min(int(nums[0]), 8) if nums else 2
        for i in range(count):
            elements.append({'type': 'ellipse', 'x': 220 + i * 180, 'y': 200,
                             'width': 120, 'height': 80, 'strokeColor': '#2f9e44',
                             'fillColor': '#d3f9d8', 'text': f'椭圆{i+1}'})
    else:
        elements = make_flow(['开始', (description or '步骤')[:8], '结束'])

    for el in elements:
        el.setdefault('strokeColor', '#1971c2')
        el.setdefault('fillColor', '#dbe4ff')
    return elements


class Handler(http.server.SimpleHTTPRequestHandler):
    # ---------------------------------------------------------------
    #  静态文件**白名单**：只发下面这几个路径，其它一律 404。
    #
    #  为什么必须这么严：这个 Handler 继承自 SimpleHTTPRequestHandler，
    #  它默认会把**服务目录下的任何文件**原样发出去。实测（2026-09-18，部署机上）：
    #      GET /ai_config.json  -> 200，拿到 API key 明文
    #      GET /users.json      -> 200，拿到全部账号与口令哈希
    #      GET /wb_token.txt    -> 200，拿到访问令牌
    #      GET /wb.key          -> 200，拿到 HTTPS 私钥
    #      GET /                -> 200，一份目录清单（连"有哪些敏感文件"都告诉对方了）
    #  也就是说：一个"自托管的画板工具"，程序自己就是泄露源。内网里任何一台设备
    #  （或端口被映射到公网后的任何人）都能把密钥和账号拖走。
    #
    #  所以：不列目录、不发白名单外的任何文件。页面只有 board.html 一个入口，
    #  静态资源只有 docs/board.png；其余敏感文件即使躺在同一个目录里也拿不到。
    # ---------------------------------------------------------------
    STATIC_OK = {
        '/': ('board.html', 'text/html; charset=utf-8'),
        '/board.html': ('board.html', 'text/html; charset=utf-8'),
        '/index.html': ('board.html', 'text/html; charset=utf-8'),
        '/docs/board.png': ('docs/board.png', 'image/png'),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def list_directory(self, path):
        """绝不列目录（父类默认会生成一份文件清单）"""
        self.send_error(404, 'File not found')
        return None

    def send_head(self):
        """静态文件只走白名单；其余（.json/.key/.txt/.log/备份）一律 404"""
        try:
            path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        except Exception:
            path = self.path
        item = self.STATIC_OK.get(path)
        if not item:
            self.send_error(404, 'File not found')
            return None
        rel, ctype = item
        full = os.path.join(DIRECTORY, rel)
        if not os.path.isfile(full):
            self.send_error(404, 'File not found')
            return None
        try:
            with open(full, 'rb') as f:
                data = f.read()
        except Exception:
            self.send_error(404, 'File not found')
            return None
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache, must-revalidate')
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass
        return None

    # ---------- 基础 ----------
    def _reply(self, data, status=200):
        try:
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self._cors()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _reply_text(self, body, ctype='text/plain; charset=utf-8', status=200):
        try:
            raw = body.encode('utf-8') if isinstance(body, str) else body
            self.send_response(status)
            self._cors()
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except Exception:
            pass

    def _is_local(self):
        try:
            return (self.client_address[0] or '') in ('127.0.0.1', '::1', 'localhost')
        except Exception:
            return False

    _mcp_board = ''

    def mcp_handle(self, msg):
        """MCP JSON-RPC（HTTP 传输）。语义与 mcp_server.py 完全一致 ——
        这样 agent 只要知道网址就能接，本地一个文件都不用放。"""
        method = msg.get('method')
        mid = msg.get('id')

        def R(result=None, error=None):
            m = {'jsonrpc': '2.0', 'id': mid}
            if error is not None:
                m['error'] = error
            else:
                m['result'] = result
            return m

        if method == 'initialize':
            return R({'protocolVersion': (msg.get('params') or {}).get('protocolVersion') or MCP_PROTOCOL,
                      'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'ai-whiteboard', 'version': '1.0.0'}})
        if method in ('notifications/initialized', 'initialized', 'notifications/cancelled'):
            return None
        if method == 'ping':
            return R({})
        if method == 'tools/list':
            tools = tools_i18n(en=getattr(self, '_mcp_lang_en', False))
            return R({'tools': tools})
        if method == 'tools/call':
            p = msg.get('params') or {}
            name = p.get('name') or ''
            args = p.get('arguments') or {}
            if name == 'board_doc_list':
                bd = args.get('board') or self._mcp_board
                lst = doc_list(bd)
                if not lst:
                    return R({'content': [{'type': 'text', 'text': '这块板还没有上传任何附件。'}], 'isError': False})
                txt = '\n'.join('%s  %s（%d 字）' % (d['id'], d['name'], d['chars']) for d in lst)
                return R({'content': [{'type': 'text', 'text': txt}], 'isError': False})
            if name == 'board_doc_read':
                bd = args.get('board') or self._mcp_board
                res = doc_read(bd, str(args.get('id') or ''), args.get('from') or 1, args.get('to') or 0)
                if not res.get('ok'):
                    return R({'content': [{'type': 'text', 'text': res.get('error') or '读不到'}], 'isError': True})
                head = '《%s》共 %d 字 / %d 行，下面是第 %d-%d 行%s' % (
                    res['name'], res['chars'], res['lines'], res['from'], res['to'],
                    '（还有后文，继续用 from/to 读）' if res['truncated'] else '')
                return R({'content': [{'type': 'text', 'text': head + '\n\n' + res['text']}], 'isError': False})
            if name == 'board_chat_say':
                text = str(args.get('text') or '').strip()
                if not text:
                    return R({'content': [{'type': 'text', 'text': '缺少 text'}], 'isError': True})
                it = chat_push('agent', text, 'mcp', args.get('board') or self._mcp_board)
                return R({'content': [{'type': 'text', 'text': '已发到白板对话：%s' % text[:60]}], 'isError': False})
            if name == 'board_chat_read':
                try:
                    lim = int(args.get('limit') or 40)
                except Exception:
                    lim = 40
                msgs = chat_since(0, args.get('board') or self._mcp_board)[-max(1, min(200, lim)):]
                txt = '\n'.join('[%s] %s' % (m['who'], m['text']) for m in msgs) or '（还没有对话）'
                return R({'content': [{'type': 'text', 'text': txt}], 'isError': False})
            if name == 'board_chat_wait':
                try:
                    since = int(args.get('since') or 0)
                    tmo = min(120, max(1, int(args.get('timeout') or 50)))
                except Exception:
                    since, tmo = 0, 50
                t0 = time.time()
                while time.time() - t0 < tmo:
                    msgs = chat_since(since, args.get('board') or self._mcp_board)
                    if msgs:
                        txt = '\n'.join('[%s] %s' % (m['who'], m['text']) for m in msgs)
                        return R({'content': [{'type': 'text', 'text': txt}], 'isError': False})
                    time.sleep(0.3)
                return R({'content': [{'type': 'text', 'text': '（这段时间没人说话）'}], 'isError': False})
            res = self.agent_call(name, args, board=self._mcp_board)
            if not res.get('ok'):
                return R({'content': [{'type': 'text', 'text': '调用失败：%s' % res.get('error')}], 'isError': True})
            out = res.get('result')
            return R({'content': [{'type': 'text',
                                   'text': out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)}],
                      'isError': False})
        if mid is not None:
            return R(error={'code': -32601, 'message': 'method not found: %s' % method})
        return None

    def agent_call(self, tool, args, timeout=170, board=''):
        """把一个工具调用交给浏览器执行并等结果（MCP 与 /api/agent/call 共用同一条路）。
        board 决定这次调用交给哪块板的页面执行。"""
        names = set(t['function']['name'] for t in WB_TOOLS)
        if tool not in names:
            return {'ok': False, 'error': '没有这个工具：%s' % tool}
        it = agent_enqueue(tool, args or {}, board)
        print('[AGENT-BRIDGE] 排队 %s（等浏览器执行）' % tool, file=sys.stderr, flush=True)
        if not it['ev'].wait(timeout=timeout):
            return {'ok': False, 'error': '白板页面没有响应（board.html 没打开？或那台设备没联网）'}
        return {'ok': True, 'result': it['result']}

    def _deny_remote(self):
        """非本机来源是否拒绝。返回 True 表示已拒绝（调用方直接 return）。

        策略由环境变量决定：WB_NO_TOKEN=1 全放行；WB_TRUST_LAN=1 时私有地址放行；
        默认只有本机放行。内网团队共用建议 WB_TRUST_LAN=1 —— 大家打开就能用，不用输令牌。
        """
        if NO_TOKEN:
            return False
        if self._is_local():
            return False
        if TRUST_LAN:
            try:
                if _is_private_ip(self.client_address[0] or ''):
                    return False
            except Exception:
                pass
        tok = (self.headers.get('X-WB-Token') or '').strip()
        if not tok:
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                tok = (q.get('token', ['']) or [''])[0].strip()
            except Exception:
                tok = ''
        if tok and secrets.compare_digest(tok, ACCESS_TOKEN):
            return False
        print('[AUTH] 拒绝非本机请求 %s（缺少/错误的访问令牌）' % self.path[:60], file=sys.stderr, flush=True)
        self._reply({'ok': False, 'auth': 'need-token',
                     'error': '需要访问令牌：非本机访问请在页面提示里输入 wb_token.txt 中的令牌'})
        return True

    def _cors(self):
        # 原来是无条件 ACAO:* —— 任何网站/局域网设备都能从浏览器直接驱动本机接口
        # （花用户的 API key、改画布、清空待办、起浏览器子进程）。
        # 页面本来就是本服务自己提供的，同源请求根本不需要 CORS 头；这里只在
        # Origin 与请求 Host 一致时才回，其它来源一律不回，跨站就带不上凭证也无法读响应。
        origin = (self.headers.get('Origin') or '').strip()
        host = (self.headers.get('Host') or '').strip()
        if origin and host and origin.split('://')[-1] == host:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def end_headers(self):
        # 关键：禁止浏览器缓存页面/JS，避免你一直在跑旧版本
        try:
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
        except Exception:
            pass
        super().end_headers()

    def _read_json(self):
        """读请求体。解析失败时**不是**返回空对象就算完 —— 会置 self._json_bad，
        让 /api/todos 这类"空对象会被当成清空"的接口能够拒绝执行（实测会静默清空待办总表）。"""
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length) if length else b''
        self._json_bad = False
        if not raw.strip():
            self._json_bad = True
            return {}
        for enc in ('utf-8', 'gbk'):
            try:
                return json.loads(raw.decode(enc))
            except Exception:
                continue
        self._json_bad = True
        print('[WARN] 请求体不是合法 JSON，已标记（破坏性接口会拒绝执行）', file=sys.stderr, flush=True)
        return {}

    def log_message(self, fmt, *args):
        pass

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path.startswith('/api/') and self._deny_remote():
                return
            if path == '/api/version':
                # 页面用来自查"我是不是旧版"：返回磁盘上 board.html 里的构建号与体积。
                # 用户改了代码但页面没刷新时，前端就能主动提示刷新，而不是让人对着旧页面点半天。
                info = {'build': '', 'size': 0, 'mtime': 0}
                try:
                    fp = os.path.join(DIRECTORY, 'board.html')
                    with open(fp, encoding='utf-8') as fh:
                        html = fh.read()
                    mm = re.search(r"WB_BUILD = '([^']+)'", html)
                    info = {'build': (mm.group(1) if mm else ''), 'size': len(html),
                            'mtime': int(os.path.getmtime(fp))}
                except Exception as e:
                    info['error'] = str(e)[:80]
                self._reply(info)
                return
            if path in ('/.well-known/agent.json', '/agent.json'):
                base = self._base_url()
                _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                _bd = board_key((_q.get('board', ['']) or [''])[0])
                _bq = ('?board=' + urllib.parse.quote(_bd)) if _bd else ''
                _en_aj = wants_english(dict(self.headers), _q)
                self._reply({
                    'name': 'ai-whiteboard',
                    'description': ('A whiteboard you and your agent share: shared canvas + shared conversation'
                                    if _en_aj else '一块你和你的 agent 共用的白板：共享画布 + 共享对话'),
                    'version': '1.0.0',
                    # 归属哪块板：带上 ?board= 就是这个板；不带就是 default 板
                    'board': _bd or 'default',
                    'board_url': base + '/board.html' + _bq,
                    'onboarding': base + '/join.md' + _bq,
                    'mcp': {'transport': 'http', 'url': base + '/mcp' + _bq, 'protocolVersion': MCP_PROTOCOL},
                    'http_api': {
                        'hello': base + '/api/agent/hello',
                        'chat_wait': base + '/api/chat/wait',
                        'chat_pull': base + '/api/chat/pull',
                        'chat_push': base + '/api/chat/push',
                        'tools': base + '/api/tools',
                        'call': base + '/api/agent/call',
                        'status': base + '/api/agent/status',
                    },
                    'note': ('every request except tools must carry "board": "%s" (or ?board= in the URL), '
                             'otherwise it lands on the default board' % (_bd or 'default')) if _en_aj else
                            ('除 tools 外的每个请求都要带 "board": "%s"（或 URL 上 ?board=），否则会落到 default 板'
                             % (_bd or 'default')),
                    'auth': {'header': 'X-WB-Token',
                             'note': ('LAN deployments normally need no token; see onboarding if yours does'
                                      if _en_aj else '内网部署通常不需要令牌；需要时见 onboarding')},
                    'lang': 'en' if _en_aj else 'zh',
                })
                return
            if path in ('/join.md', '/llms.txt'):
                # agent 自助入驻：带上 ?board=<板号> 就照这块板入驻（谁开的板就是谁的 agent）
                _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                _bd = board_key((_q.get('board', ['']) or [''])[0])
                base = self._base_url()
                board_url = base + '/board.html' + ('?board=' + urllib.parse.quote(_bd) if _bd else '')
                _en = wants_english(dict(self.headers), _q)
                self._reply_text(build_join_md_lang(base, board_url, _bd, _en),
                                 'text/markdown; charset=utf-8')
                return
            if path == '/api/doc/list':
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._reply({'ok': True, 'docs': doc_list((q.get('board', ['']) or [''])[0])})
                return
            if path == '/api/doc/read':
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                def _gi(k, d=0):
                    try:
                        return int((q.get(k, [str(d)]) or [str(d)])[0])
                    except (TypeError, ValueError):
                        return d
                self._reply(doc_read((q.get('board', ['']) or [''])[0],
                                     (q.get('id', ['']) or [''])[0], _gi('from', 1), _gi('to', 0)))
                return
            if path == '/api/tools':
                # 给 mcp_server.py 用：agent 那台机器上不必再放一份 server_v2.py
                # ?lang=en（或 Accept-Language: en）拿英文说明；默认中文
                _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                _en = wants_english(dict(self.headers), _q)
                self._reply({'ok': True, 'lang': 'en' if _en else 'zh',
                             'tools': tools_i18n(en=_en, shape='openai')})
                return
            if path == '/api/health':
                self._reply({'ok': True, 'has_key': bool(CONFIG.get('api_key')),
                             'model': CONFIG['model'], 'vision_model': CONFIG['vision_model'],
                'ai': bool(CONFIG.get('api_key')),
                # 把安全地址告诉页面：麦克风只在 HTTPS 下可用，页面据此引导用户切过去
                'tls_port': TLS_PORT,
                             'vision': bool(CONFIG.get('vision_model') and
                                            (CONFIG.get('vision_api_key') or CONFIG.get('api_key'))),
                             'asr_model': CONFIG.get('asr_model', ''),
                             'tts_model': CONFIG.get('tts_model', ''),
                             'speech': bool(CONFIG.get('asr_model') and self._speech_conf()[1]),
                             'mode': 'builtin-ai' if CONFIG.get('api_key') else 'pure-agent'})
                return
            if path == '/api/sync':
                self._reply(self.do_sync_get())
                return
            if path == '/api/disc/pull':
                self._reply(self.do_disc_pull())
                return
            if path == '/api/review/rules':
                q2 = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                prof = (q2.get('profile', [''])[0] or None)
                meta = rules_meta()
                self._reply({'ok': True, 'rules': load_rules(prof), 'meta': meta})
                return
            if path == '/api/selfcheck':
                self._reply(self.do_selfcheck())
                return
            if path == '/api/agent/status':
                _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                bd = (_q.get('board', ['']) or [''])[0]
                live = agent_live(board=bd)
                self._reply({'ok': True, 'board': bd or '(全部)', 'agents': live, 'count': len(live)})
                return
            if path == '/api/chat/pull':
                try:
                    _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    _since = int((_q.get('since', ['0']) or ['0'])[0])
                except Exception:
                    _since = 0
                _qb = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                _bd = (_qb.get('board', ['']) or [''])[0]
                msgs = chat_since(_since, _bd)
                self._reply({'ok': True, 'messages': msgs,
                             'last': msgs[-1]['seq'] if msgs else _since})
                return
            if path == '/api/agent/poll':
                # 页面长轮询取任务（最多等 wait 秒，避免高频空转）
                try:
                    _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    _wait = min(30, max(1, int((_q.get('wait', ['25']) or ['25'])[0])))
                except Exception:
                    _wait = 25
                t0 = time.time()
                _qb = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                _bd = (_qb.get('board', ['']) or [''])[0]
                while time.time() - t0 < _wait:
                    it = agent_take(_bd)
                    if it:
                        self._reply({'ok': True, 'call': {'id': it['id'], 'tool': it['tool'], 'args': it['args']}})
                        return
                    time.sleep(0.2)
                self._reply({'ok': True, 'call': None})
                return
            if path == '/api/todos':
                with SYNC_LOCK:
                    items = [x for x in TODOS['items'] if not x.get('done')]
                self._reply({'ok': True, 'ts': TODOS['ts'], 'count': len(TODOS['items']),
                             'open': len(items), 'items': TODOS['items']})
                return
            if path == '/api/ai/chat':
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._reply(self.do_chat(q.get('message', [''])[0], {}))
                return
            if path == '/api/ai/act':
                # 便于浏览器直接调试：GET /api/ai/act?message=画一个登录流程
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._reply(self.do_act({'message': q.get('message', [''])[0]}))
                return
            super().do_GET()
        except Exception as e:
            self._reply({'error': str(e)}, 500)

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            # /mcp 同样要过令牌策略，否则它会绕过内网/外网判定
            if (path.startswith('/api/') or path.rstrip('/') == '/mcp') and self._deny_remote():
                return
            if path.startswith('/api/ai/'):
                clen = self.headers.get('Content-Length', '0')
                flag = ' [LEGACY!]' if path in ('/api/ai/draw', '/api/ai/chat', '/api/ai/act') else ''
                print(f'[API] {path}{flag}  body={clen}B', file=sys.stderr, flush=True)
            if path == '/api/sync':
                self._reply(self.do_sync_post(self._read_json()))
                return
            if path == '/api/disc/push':
                self._reply(self.do_disc_push(self._read_json()))
                return
            if path == '/api/agent/hello':
                data = self._read_json()
                nm = agent_hello(data.get('name'), data.get('ver'), data.get('board'))
                print('[AGENT] %s 已接入画板' % nm, file=sys.stderr, flush=True)
                self._reply({'ok': True, 'name': nm, 'live': len(agent_live(board=data.get('board')))})
                return
            if path == '/api/chat/wait':
                # 给 agent 用的"听"接口：阻塞等到用户/别人说了新的话再返回。
                # MCP 是拉模型，没有这个就只能靠轮询 —— 有它，agent 才能真正"跟用户对话"。
                data = self._read_json()
                try:
                    since = int(data.get('since') or 0)
                    wait = min(120, max(1, int(data.get('timeout') or 50)))
                except Exception:
                    since, wait = 0, 50
                t0 = time.time()
                _bd = str(data.get('board') or '')
                while time.time() - t0 < wait:
                    msgs = chat_since(since, _bd)
                    if msgs:
                        self._reply({'ok': True, 'messages': msgs, 'last': msgs[-1]['seq']})
                        return
                    time.sleep(0.3)
                self._reply({'ok': True, 'messages': [], 'last': since})
                return
            if path == '/api/chat/push':
                data = self._read_json()
                text = str(data.get('text') or '').strip()
                if not text:
                    self._reply({'ok': False, 'error': '缺少 text'})
                    return
                it = chat_push(data.get('who') or 'agent', text, data.get('src') or '', data.get('board'))
                self._reply({'ok': True, 'seq': it['seq']})
                return
            if path == '/api/chat/clear':
                # 白板上的「清空对话」要连 agent 那份一起清 —— 否则用户以为清干净了，
                # agent 用 board_chat_read 还能读到旧消息。
                data = self._read_json()
                n = chat_clear(data.get('board'))
                self._reply({'ok': True, 'board': board_key(data.get('board')),
                             'cleared': n})
                return
            if path == '/api/doc/push':
                # 附件（用户上传的方案）：按板存服务端，对话里只放一条引用
                data = self._read_json()
                self._reply(doc_push(data.get('board'), data.get('name'), data.get('text'), 'board'))
                return
            if path == '/api/doc/del':
                data = self._read_json()
                self._reply(doc_del(data.get('board'), data.get('id')))
                return
            if path == '/api/register':
                data = self._read_json()
                self._reply(user_register(data.get('name'), data.get('pass')))
                return
            if path == '/api/login':
                data = self._read_json()
                self._reply(user_login(data.get('name'), data.get('pass')))
                return
            if path == '/api/logout':
                data = self._read_json()
                self._reply(user_logout(data.get('token')))
                return
            if path == '/api/me':
                data = self._read_json()
                nm = user_by_token(data.get('token'))
                if not nm:
                    self._reply({'ok': False, 'error': '登录已过期，请重新登录'})
                    return
                with USER_LOCK:
                    boards = [{'board': b, 'owner': v.get('owner'),
                               'mine': v.get('owner') == nm, 'members': len(v.get('members') or [])}
                              for b, v in USERS['boards'].items() if nm in (v.get('members') or [])]
                    bd = (USERS['users'].get(nm) or {}).get('board') or board_of_user(nm)
                self._reply({'ok': True, 'user': nm, 'board': bd, 'myBoard': bd, 'boards': boards})
                return
            if path == '/api/invite':
                # 邀请别人来同一块板：给他一个带 ?board= 的地址，他打开就是协作端
                data = self._read_json()
                nm = user_by_token(data.get('token')) or str(data.get('user') or '')[:24]
                bd = board_key(data.get('board') or (USERS['users'].get(nm) or {}).get('board'))
                if not bd:
                    self._reply({'ok': False, 'error': '缺少 board（或先登录）'})
                    return
                other = _clean_name(data.get('as'))
                if other:
                    self._reply(board_join(other, bd))
                    return
                owner = board_claim(bd, nm) if nm else board_owner(bd)
                base = self._base_url()
                url = '%s/board.html?board=%s' % (base, urllib.parse.quote(bd))
                self._reply({'ok': True, 'board': bd, 'owner': owner, 'url': url,
                             'agent': '%s/join.md?board=%s' % (base, urllib.parse.quote(bd))})
                return
            if path == '/api/join':
                data = self._read_json()
                self._reply(board_join(data.get('user'), data.get('board')))
                return
            if path.rstrip('/') == '/mcp':
                # MCP over HTTP：agent 只要知道网址就能接，本地不用放任何文件。
                # 归属哪块板由 URL 决定：POST /mcp?board=<画板号>（也接受 X-WB-Board 头）。
                # 客户端的配置里把画板号写进 URL 即可 —— 这就是「谁开的板就是谁的 agent 驻扎」。
                _qm = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._mcp_board = board_key((_qm.get('board', ['']) or [''])[0]
                                            or self.headers.get('X-WB-Board'))
                self._mcp_lang_en = wants_english(dict(self.headers), _qm)
                msg = self._read_json()
                resp = self.mcp_handle(msg)
                self._reply(resp if resp is not None else {'ok': True})
                return
            if path == '/api/agent/call':
                data = self._read_json()
                tool = str(data.get('tool') or '').strip()
                if not tool:
                    self._reply({'ok': False, 'error': '缺少 tool'})
                    return
                names = {t['function']['name'] for t in WB_TOOLS}
                if tool not in names:
                    self._reply({'ok': False, 'error': '没有这个工具：%s' % tool})
                    return
                self._reply(self.agent_call(tool, data.get('args') or {}, board=data.get('board')))
                return
            if path == '/api/agent/result':
                data = self._read_json()
                rid = str(data.get('id') or '')
                with AGENT_LOCK:
                    hit = None
                    for x in AGENT_QUEUE:
                        if x['id'] == rid:
                            hit = x
                            break
                if not hit:
                    self._reply({'ok': False, 'error': '没有这个任务：%s' % rid})
                    return
                hit['result'] = data.get('result')
                hit['ev'].set()
                self._reply({'ok': True})
                return
            if path == '/api/todos':
                data = self._read_json()
                # 原来写的是 data.get('items') or [] —— 请求体畸形或缺字段会被当成"清空全部待办"。
                if getattr(self, '_json_bad', False):
                    return self._reply({'ok': False, 'error': '请求体不是合法 JSON，已忽略（待办没有被改动）'})
                if 'items' not in data or not isinstance(data.get('items'), list):
                    return self._reply({'ok': False, 'error': 'items 必须是数组（待办没有被改动）'})
                items = data['items']
                with SYNC_LOCK:
                    TODOS['items'] = items[:400]
                    TODOS['ts'] = int(time.time())
                    save_todos()               # 落盘：重启后待办不会丢
                    open_n = len([x for x in TODOS['items'] if not x.get('done')])
                print(f'[TODO] 收到 {len(items)} 条（未完成 {open_n}）', file=sys.stderr, flush=True)
                self._reply({'ok': True, 'count': len(TODOS['items']), 'open': open_n})
                return
            if path == '/api/ai/report':
                self._reply(self.do_report(self._read_json()))
                return
            if path == '/api/review/page':
                self._reply(self.do_page_review(self._read_json()))
                return
            if path == '/api/review/shot':
                self._reply(self.do_shot(self._read_json()))
                return
            if path == '/api/review/rules':
                self._reply(self.do_rules(self._read_json()))
                return
            if path == '/api/ai/act':
                self._reply(self.do_act(self._read_json()))
                return
            if path == '/api/ai/agent':
                self._reply(self.do_agent(self._read_json()))
                return
            if path == '/api/ai/review':
                self._reply(self.do_review(self._read_json()))
                return
            if path == '/api/ai/discussion':
                self._reply(self.do_discussion(self._read_json()))
                return
            if path == '/api/ai/asr':
                self._reply(self.do_asr(self._read_json()))
                return
            if path == '/api/ai/tts':
                self._reply(self.do_tts(self._read_json()))
                return
            if path == '/api/ai/chat':
                data = self._read_json()
                self._reply(self.do_chat(data.get('message', ''), data.get('canvas', {})))
                return
            if path == '/api/ai/draw':
                data = self._read_json()
                self._reply(self.do_draw(data.get('description', '')))
                return
            self._reply({'error': 'Not found'}, 404)
        except Exception as e:
            self._reply({'error': str(e)}, 500)

    # ---------- 讨论整理：把转写 + 图形变化时间线整理成方案 ----------
    def do_discussion(self, data):
        transcript = data.get('transcript') or []
        timeline = data.get('timeline') or []
        canvas = data.get('canvas') or {}
        page = data.get('page') or ''
        context = data.get('context') or ''
        if not transcript and not timeline:
            return {'ok': False, 'error': '没有讨论内容'}
        def _seg_line(x):
            who = str(x.get('speaker') or '').strip()
            return "[%s]%s %s" % (x.get('t', ''), (' ' + who + '：') if who else ' ', str(x.get('text', ''))[:200])
        tr = "\n".join(_seg_line(x) for x in transcript[:400] if str(x.get('text', '')).strip())
        tl = "\n".join("[%s][%s] %s" % (x.get('t', ''), x.get('page', ''), str(x.get('text', ''))[:160])
                       for x in timeline[:200])
        struct, _ = describe_canvas(canvas)
        mode = str(data.get('mode') or 'text')
        if mode == 'diagram':
            prompt = SYSTEM_DISCUSSION_DIAGRAM.format(transcript=tr or '（没有语音转写）',
                                                      timeline=tl or '（没有图形变化记录）',
                                                      canvas=struct[:2500],
                                                      page=page or '当前画板',
                                                      context=context or '（无）')
        else:
            prompt = SYSTEM_DISCUSSION.format(transcript=tr or '（没有语音转写）',
                                              timeline=tl or '（没有图形变化记录）',
                                              canvas=struct[:3000],
                                              page=page or '当前画板',
                                              context=context or '（无）')
        t0 = time.time()
        try:
            text = call_llm([{'role': 'user', 'content': prompt}], CONFIG['model'], timeout=120)
        except Exception as e:
            print(f'[DISC] 整理失败: {e}', file=sys.stderr, flush=True)
            return {'ok': False, 'error': str(e)}
        ms = int((time.time() - t0) * 1000)
        text = (text or '').strip()
        print(f'[DISC] {mode} 转写 {len(transcript)} 段 / 时间线 {len(timeline)} 条 -> {ms}ms -> {len(text)} 字',
              file=sys.stderr, flush=True)
        if mode == 'diagram':
            obj = extract_json(text)
            if not obj or not obj.get('nodes'):
                return {'ok': False, 'error': '模型没有返回可用的图形 JSON'}
            return {'ok': True, 'diagram': obj, 'ms': ms}
        # 正文末尾的 JSON 待办块：解析出来并从事正文里剥掉
        todos = []
        body = text
        m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```\s*$', text)
        if m:
            obj = extract_json(m.group(1))
            if isinstance(obj, dict) and isinstance(obj.get('todos'), list):
                todos = [t for t in obj['todos'] if isinstance(t, dict)][:20]
                body = text[:m.start()].strip()
        print(f'[DISC] 待办 {len(todos)} 条', file=sys.stderr, flush=True)
        return {'ok': True, 'content': body, 'todos': todos, 'ms': ms,
                'segments': len(transcript), 'events': len(timeline)}

    # ---------- 服务端自检（面板「自检」按钮用，毫秒级、不起子进程）----------
    def do_selfcheck(self):
        checks = []

        def ck(name, cond, note=''):
            checks.append({'name': name, 'ok': bool(cond), 'note': str(note)[:80]})

        t0 = time.time()
        # 0) 先分清形态：没配 key 就是「纯 agent 模式」——这是**允许的部署形态**，
        #    此时"对话模型没 key"不是故障，否则别人拿到干净副本一跑自检就以为装坏了。
        pure = not CONFIG.get('api_key')
        ck('部署形态', True, '纯 agent 模式（未配模型 key，等你的 agent 接入）' if pure else '内置 AI 模式')
        # 1) 配置
        ck('模型配置已加载', bool(CONFIG.get('model')), CONFIG.get('model'))
        ck('对话模型有 key', bool(CONFIG.get('api_key')) or pure,
           '纯 agent 模式：不需要（思考在 agent 那边）' if pure else '')
        ck('视觉通道已配置', bool(CONFIG.get('vision_model')) or pure,
           '纯 agent 模式：不需要（看图由 agent 负责）' if pure else CONFIG.get('vision_model'))
        _asr_ok = bool(CONFIG.get('asr_model') and self._speech_conf()[1])
        ck('语音输入可用（asr_model + speech key）', _asr_ok or pure,
           (CONFIG.get('asr_model') + '（已配）') if _asr_ok else
           '未配置：想要语音输入就填 asr_model + speech_base_url / speech_api_key')
        ck('配置里没有遗留的旧网关地址',
           'xiaomimimo.com' not in str(CONFIG.get('base_url') or ''), CONFIG.get('base_url'))

        # 2) 规范
        meta = rules_meta()
        ck('审阅规范可读', isinstance(meta, dict))
        ck('规范分环境可用', bool(meta.get('profiles')) or meta.get('flat'),
           '环境：' + ('、'.join(meta.get('profiles') or []) or '扁平'))

        # 3) 分析器
        demo = ('<html><body><h1>标题</h1><h3>跳级</h3><img src=a.png>'
                '<input type=text><div id=x></div><div id=x></div><center>c</center></body></html>')
        info = analyze_html(demo, 'selfcheck', {})
        kinds = {c['kind'] for c in info.get('checks') or []}
        ck('HTML 分析器能跑', info.get('title') == '' or True, '%d 条检查' % len(info.get('checks') or []))
        ck('能查出无障碍问题', 'a11y' in kinds)
        ck('能查出结构问题', 'structure' in kinds)
        ck('能查出重复 id', 'html' in kinds)
        ck('每条检查都带修法', all(c.get('fix') for c in (info.get('checks') or [])))
        ck('分析器隔离脚本内容', '重复 id：' not in ' '.join(
            c['msg'] for c in analyze_html('<script>el.id = uid();</script><div id=a></div>', 'x', {})['checks']))

        # 4) JSON 提取
        ck('extract_json 能取代码块里的 JSON',
           (extract_json('前言\n```json\n{"todos":[{"what":"x"}]}\n```') or {}).get('todos') is not None)
        ck('extract_json 对垃圾输入返回 None', extract_json('这不是 json') is None)

        # 5) 存储自测（用完就清，不污染真实数据）
        probe = '__selfcheck__'
        with SYNC_LOCK:
            SYNC_BOARDS[probe] = {'rev': 1, 'state': {'els': []}, 'by': 'selfcheck', 'ts': time.time()}
            got = SYNC_BOARDS.get(probe, {}).get('rev')
        ck('同步存储可读写', got == 1)
        with SYNC_LOCK:
            SYNC_BOARDS.pop(probe, None)
            DISC_SEQ[probe] = 1
            DISC_LOG[probe] = [{'seq': 1, 'text': 'x'}]
            ok_disc = DISC_LOG[probe][0]['text'] == 'x' and DISC_SEQ[probe] == 1
            DISC_LOG.pop(probe, None)
            DISC_SEQ.pop(probe, None)
        ck('多端转写存储可读写', ok_disc)
        ck('待办存储结构正常', isinstance(TODOS, dict) and isinstance(TODOS.get('items'), list),
           '%d 条' % len(TODOS.get('items') or []))

        # 6) 关键文件与功能标记
        files = ['board.html', '几何自检.js', '全面验证.py', 'web审查.py', '定时审阅.py', 'server_v2.py']
        missing = [f for f in files if not os.path.exists(os.path.join(DIRECTORY, f))]
        ck('关键文件齐全', not missing, ('缺：' + '、'.join(missing)) if missing else '%d 个' % len(files))
        try:
            with open(os.path.join(DIRECTORY, 'board.html'), 'r', encoding='utf-8', errors='replace') as f:
                html = f.read(3_000_000)
            markers = ['function parsePlantUML', 'function toMermaid', 'function drawRef', 'openTodos',
                       'function markRisksOnCanvas', 'function reviewFetch', 'presentSetMode', 'function layoutGraph']
            hit = [m for m in markers if m in html]
            ck('页面功能标记齐全', len(hit) == len(markers), '%d/%d' % (len(hit), len(markers)))
            ck('页面体量正常', len(html) > 200_000, '%.0f KB' % (len(html) / 1024))
        except Exception as e:
            ck('读取 board.html', False, str(e))

        ms = int((time.time() - t0) * 1000)
        passed = len([c for c in checks if c['ok']])
        print(f'[SELFCHECK] {passed}/{len(checks)} 通过（{ms}ms）', file=sys.stderr, flush=True)
        return {'ok': passed == len(checks), 'pass': passed, 'total': len(checks), 'ms': ms, 'checks': checks}

    def _find_browser(self):
        for p in (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                  r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
                  r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                  r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
                  '/usr/bin/chromium', '/usr/bin/google-chrome'):
            if os.path.exists(p):
                return p
        return None

    def _shot_cdp(self, exe, url, width, height):
        """首选方案：走 CDP（截图.js）。

        比命令行 --screenshot 强的地方：
          - 等的是 load 事件，最多等 20 秒，慢站不会无限拖（实测外网快一倍以上）
          - captureBeyondViewport 一次截下整页高度，"看全貌"才成立
        返回 (原始png字节, 元信息dict)；失败时第一个元素是 None，第二个是原因字符串。
        """
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), '截图.js')
        if not os.path.exists(script):
            return None, '没有 截图.js'
        node = shutil.which('node') or shutil.which('node.exe')
        if not node:
            return None, '本机没装 node，无法走 CDP'
        fd, out = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        t0 = time.time()
        try:
            env = dict(os.environ)
            env['WB_EDGE'] = exe
            r = subprocess.run([node, script, url, out, str(width), str(height), '20'],
                               capture_output=True, timeout=100, env=env)
            raw = b''
            if os.path.exists(out):
                with open(out, 'rb') as f:
                    raw = f.read()
            if len(raw) >= 1200:
                meta = {}
                toks = (r.stdout or b'').decode('utf-8', 'ignore').strip().splitlines()
                if toks:
                    try:
                        meta = json.loads(toks[-1])
                    except Exception:
                        meta = {}
                meta['ms'] = int((time.time() - t0) * 1000)
                return raw, meta
            errs = (r.stderr or b'').decode('utf-8', 'ignore').strip().splitlines()
            return None, (errs[-1][:120] if errs else 'CDP 截图没出图')
        except subprocess.TimeoutExpired:
            return None, 'CDP 截图超时（100 秒）'
        except Exception as e:
            return None, str(e)[:120]
        finally:
            try:
                os.remove(out)
            except Exception:
                pass

    def _shot_cli(self, exe, url, width, height):
        """兜底方案：命令行 --screenshot（CDP 不可用时用）。

        返回 (原始png字节, 原因字符串)，两者只有一个非空。
        """
        fd, out = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        # 关键：给无头实例一个独立的 profile 目录。
        # 不指定时它会去碰用户正在使用的那份 Edge 数据（被转发 / 等锁），表现就是截图卡到超时。
        profile_dir = tempfile.mkdtemp(prefix='wbshot-')
        t0 = time.time()
        try:
            # 外网页面光是"等加载完"就要 10~30 秒，所以超时给 55 秒；
            # 用 Popen 自己管：Edge 会 fork 一堆子进程，subprocess.run 超时只杀主进程，
            # 子进程会一直累积（实测一次截图能剩十几个），越用越慢。所以无论成功失败都杀整棵树。
            proc2 = subprocess.Popen(
                [exe, '--headless=new', '--disable-gpu', '--no-first-run',
                 '--no-default-browser-check', '--disable-features=Translate',
                 '--user-data-dir=' + profile_dir,
                 '--hide-scrollbars', '--force-device-scale-factor=1',
                 '--disable-extensions', '--disable-background-networking',
                 '--disable-client-side-phishing-detection', '--disable-default-apps',
                 '--disable-sync', '--no-pings', '--metrics-recording-only',
                 '--virtual-time-budget=6000',
                 '--screenshot=' + out,
                 '--window-size=%d,%d' % (width, height), url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            timed_out = False
            try:
                proc2.wait(timeout=55)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(proc2.pid), '/T', '/F'],
                                   capture_output=True)
                else:
                    try:
                        proc2.kill()
                    except Exception:
                        pass
            if timed_out:
                return None, '截图超时（55 秒还没加载完；有些站点需要登录或特别重）'
            if not os.path.exists(out) or os.path.getsize(out) < 1200:
                return None, '截图失败（页面打不开 / 超时 / 空白页）'
            with open(out, 'rb') as f:
                return f.read(), ''
        except Exception as e:
            return None, str(e)[:120]
        finally:
            try:
                os.remove(out)
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)

    def do_shot(self, data):
        """把网页截图，返回 dataURL。

        用途：把网页"搬"到画板上当底图 —— 比嵌一个小 iframe 强得多：
        可以随意缩放看全貌、直接在图上圈画标注，AI 也能看到这张图参与讨论。
        先走 CDP（快、能截整页），不行再退回命令行 --screenshot。
        """
        url = (data.get('url') or '').strip()
        if not url:
            return {'ok': False, 'error': '缺少 url'}
        if url.startswith('file://'):
            # 前端有时直接把本机路径拼成 file:///D:/... 传过来，别当成非法网址拒掉。
            # 注意别乱 lstrip('/')：POSIX 绝对路径 file:///opt/x 的根斜杠是路径的一部分，
            # 去掉就变成相对路径 opt/x，在 Linux 上必然"文件不存在"（Windows 因盘符 D:/ 侥幸没暴露）。
            raw = urllib.parse.unquote(url[7:])
            local = raw[1:] if re.match(r'^/[a-zA-Z]:[\\/]', raw) else raw
            if not os.path.exists(local):
                return {'ok': False, 'error': '文件不存在：' + local}
            url = 'file:///' + local.replace('\\', '/')
        elif re.match(r'^[a-zA-Z]:[\\/]', url) or url.startswith('/'):
            if not os.path.exists(url):
                return {'ok': False, 'error': '文件不存在：' + url}
            url = 'file:///' + url.replace('\\', '/').lstrip('/')
        elif not re.match(r'^https?://', url):
            return {'ok': False, 'error': '请填 http(s) 网址，或本机存在的文件路径'}
        try:
            width = max(600, min(2400, int(data.get('width') or 1440)))
            height = max(600, min(8000, int(data.get('height') or 2600)))
        except Exception:
            width, height = 1440, 2600
        exe = self._find_browser()
        if not exe:
            return {'ok': False, 'error': '没找到 Edge / Chrome，无法截图'}

        t0 = time.time()
        raw, meta = self._shot_cdp(exe, url, width, height)
        how = 'cdp'
        if raw is None:
            cdp_err = meta if isinstance(meta, str) else 'CDP 截图失败'
            print('[SHOT] CDP 失败(%s)，退回命令行' % cdp_err, file=sys.stderr, flush=True)
            raw, cli_err = self._shot_cli(exe, url, width, height)
            how = 'cli'
            if raw is None:
                return {'ok': False, 'error': cli_err or cdp_err}
        real_w = int(meta.get('width') or width) if isinstance(meta, dict) else width
        real_h = int(meta.get('height') or height) if isinstance(meta, dict) else height
        ms = int(meta.get('ms')) if isinstance(meta, dict) and meta.get('ms') else int((time.time() - t0) * 1000)
        print('[SHOT/%s] %s -> %dx%d %dB %dms' % (how, url[:70], real_w, real_h, len(raw), ms),
              file=sys.stderr, flush=True)
        return {'ok': True, 'image': 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii'),
                'width': real_w, 'height': real_h, 'bytes': len(raw), 'ms': ms, 'via': how}


    def do_rules(self, data):
        if not data:
            return {'ok': True, 'rules': load_rules()}
        rules = data.get('rules')
        if not isinstance(rules, dict):
            return {'ok': False, 'error': 'rules 必须是对象'}
        clean = {}
        if isinstance(rules.get('palette'), list):
            clean['palette'] = [str(x)[:16] for x in rules['palette'][:24]]
        for k in ('max_colors', 'max_inline_styles'):
            v = rules.get(k)
            if isinstance(v, (int, float)) and v >= 0:
                clean[k] = int(v)
        if isinstance(rules.get('banned_tags'), list):
            clean['banned_tags'] = [str(x)[:20] for x in rules['banned_tags'][:20] if str(x).strip()]
        if isinstance(rules.get('must_have_text'), list):
            clean['must_have_text'] = [str(x)[:40] for x in rules['must_have_text'][:10] if str(x).strip()]
        for k in ('require_viewport', 'require_main', 'require_description'):
            if rules.get(k):
                clean[k] = True
        prof = str(data.get('profile') or '').strip()[:20] or None
        ok = save_rules(clean, prof)
        print(f'[RULES] 保存规范 profile={prof or "(flat)"}：{list(clean.keys())}', file=sys.stderr, flush=True)
        return {'ok': ok, 'rules': clean, 'meta': rules_meta()}

    # ---------- 网页审阅：抓取 + 结构分析 + 静态检查 ----------
    def do_page_review(self, data):
        src = str(data.get('url') or '').strip()
        html = str(data.get('html') or '')
        # 1) 拿到 HTML：内联 > 本地文件 > 远程抓取
        if not html and src:
            if re.match(r'^https?://', src, re.I):
                try:
                    req = urllib.request.Request(src, headers={
                        'User-Agent': 'Mozilla/5.0 (AI-Whiteboard review)',
                        'Accept-Language': 'zh-CN,zh;q=0.9'})
                    with urllib.request.urlopen(req, timeout=25,
                                                context=ssl.create_default_context()) as resp:
                        raw = resp.read(2_000_000)
                        charset = resp.headers.get_content_charset() or 'utf-8'
                    try:
                        html = raw.decode(charset, 'replace')
                    except Exception:
                        html = raw.decode('utf-8', 'replace')
                except urllib.error.HTTPError as he:
                    return {'ok': False, 'error': f'抓取失败 HTTP {he.code}（有些站点会拒绝抓取）'}
                except Exception as e:
                    return {'ok': False, 'error': f'抓取失败：{e}'}
            else:
                path = src.strip('"\'')
                if not os.path.isabs(path):
                    path = os.path.join(DIRECTORY, path)
                if not os.path.exists(path):
                    return {'ok': False, 'error': '本地文件不存在：' + path}
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        html = f.read(2_000_000)
                except Exception as e:
                    return {'ok': False, 'error': f'读取失败：{e}'}
        if not html.strip():
            return {'ok': False, 'error': '没有可分析的内容（给 url / 本地文件路径 / 直接贴 HTML）'}
        info = analyze_html(html, src, load_rules(str(data.get('profile') or '') or None))
        mode = str(data.get('mode') or 'analyze')
        if mode != 'ai':
            return {'ok': True, 'info': info}
        vision = ''
        img = str(data.get('image') or '')
        if img.startswith('data:image') and CONFIG.get('vision_model'):
            try:
                vision = self.vision_describe_cached(img, 600)
                print(f'[PAGE] 截图转述 {len(vision)} 字', file=sys.stderr, flush=True)
            except Exception as e:
                print(f'[PAGE] 截图转述失败: {e}', file=sys.stderr, flush=True)
        prompt = SYSTEM_PAGE_REVIEW + "\n\n===== 页面结构分析 =====\n" + format_page_info(info)
        if vision:
            prompt += "\n\n===== 白板截图内容（视觉模型转述，可作为视觉观感参考）=====\n" + vision
        t0 = time.time()
        try:
            text = call_llm([{'role': 'user', 'content': prompt}], CONFIG['model'], timeout=120)
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        ms = int((time.time() - t0) * 1000)
        print(f'[PAGE] {info.get("title", "")[:40]} -> {ms}ms -> {len(text or "")} 字', file=sys.stderr, flush=True)
        return {'ok': True, 'info': info, 'review': (text or '').strip(), 'ms': ms}

    # ---------- 周报：汇总多个画板的结论与待办 ----------
    def do_report(self, data):
        material = str(data.get('material') or '')[:12000]
        if not material.strip():
            return {'ok': False, 'error': '没有素材'}
        range_text = str(data.get('range') or '本周')
        prompt = SYSTEM_REPORT.format(range=range_text, material=material)
        t0 = time.time()
        try:
            text = call_llm([{'role': 'user', 'content': prompt}], CONFIG['model'], timeout=120)
        except Exception as e:
            print(f'[REPORT] 失败: {e}', file=sys.stderr, flush=True)
            return {'ok': False, 'error': str(e)}
        ms = int((time.time() - t0) * 1000)
        text = (text or '').strip()
        print(f'[REPORT] {len(material)} 字素材 -> {ms}ms -> {len(text)} 字', file=sys.stderr, flush=True)
        return {'ok': True, 'content': text, 'ms': ms}

    # ---------- 多端讨论转写（每台设备录自己那一路，合并成一份记录）----------
    def do_disc_pull(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        board = board_key(q.get('board', ['default'])[0])
        try:
            since = int(q.get('since', ['0'])[0])
        except ValueError:
            since = 0
        with SYNC_LOCK:
            items = [x for x in DISC_LOG.get(board, []) if int(x.get('seq', 0)) > since]
        return {'ok': True, 'items': items, 'seq': DISC_SEQ.get(board, 0)}

    def do_disc_push(self, data):
        board = board_key(data.get('board'))
        text = re.sub(r'\s+', ' ', str(data.get('text') or '')).strip()[:400]
        if not text:
            disc_save()
            return {'ok': False, 'error': '空内容'}
        who = str(data.get('who') or '')[:24]
        client = str(data.get('client') or '')[:40]
        with SYNC_LOCK:
            DISC_SEQ[board] = DISC_SEQ.get(board, 0) + 1
            item = {'seq': DISC_SEQ[board], 'id': str(data.get('id') or '')[:40], 't': int(data.get('t') or 0),
                    'text': text, 'who': who, 'client': client, 'marks': data.get('marks') or [],
                    'f0': int(data.get('f0') or 0), 'centroid': int(data.get('centroid') or 0)}
            lst = DISC_LOG.setdefault(board, [])
            lst.append(item)
            if len(lst) > DISC_MAX:
                del lst[:len(lst) - DISC_MAX]
            seq = DISC_SEQ[board]
        print(f'[DISC] /{board} #{seq} {who or client}: {text[:40]}', file=sys.stderr, flush=True)
        return {'ok': True, 'seq': seq}

    # ---------- 语音：听（ASR）/ 说（TTS）----------
    def _speech_conf(self):
        base = CONFIG.get('speech_base_url') or CONFIG.get('vision_base_url') or CONFIG.get('base_url')
        key = CONFIG.get('speech_api_key') or CONFIG.get('vision_api_key') or CONFIG.get('api_key')
        return base, key

    def do_asr(self, data):
        """语音转文字：只接受 wav / mp3（网关限制），body 传 base64 或 dataURL"""
        audio = (data.get('audio') or '').strip()
        if not audio:
            return {'ok': False, 'error': '缺少 audio（base64 或 dataURL）'}
        fmt = (data.get('format') or '').lower()
        if audio.startswith('data:'):
            head, _, audio = audio.partition(',')
            m = re.search(r'audio/([A-Za-z0-9.+-]+)', head)
            if m and not fmt:
                fmt = m.group(1).lower()
        alias = {'wave': 'wav', 'x-wav': 'wav', 'wav': 'wav', 'mpeg': 'mp3', 'mp3': 'mp3', 'mpga': 'mp3'}
        fmt = alias.get(fmt, fmt or 'wav')
        if fmt not in ('wav', 'mp3'):
            return {'ok': False, 'error': f'不支持的音频格式 {fmt}（网关只接受 wav / mp3，浏览器请先转成 wav）'}
        if len(audio) > 16_000_000:
            return {'ok': False, 'error': '录音太大（>12MB），请说短一点'}
        if not CONFIG.get('asr_model'):
            return {'ok': False, 'error': '服务端未配置 asr_model（语音输入已关闭）'}
        base, key = self._speech_conf()
        msgs = [{'role': 'user', 'content': [
            {'type': 'input_audio', 'input_audio': {'data': audio, 'format': fmt}}]}]
        t0 = time.time()
        try:
            text = call_llm(msgs, CONFIG['asr_model'], timeout=90, base_url=base, api_key=key)
        except Exception as e:
            print(f'[ASR] 失败: {e}', file=sys.stderr, flush=True)
            return {'ok': False, 'error': str(e)}
        ms = int((time.time() - t0) * 1000)
        text = (text or '').strip()
        print(f'[ASR] {len(audio)}B/{fmt} -> {ms}ms -> {text[:60]!r}', file=sys.stderr, flush=True)
        return {'ok': True, 'text': text, 'ms': ms, 'format': fmt}

    def do_tts(self, data):
        """文字转语音：返回 wav 的 dataURL，供前端 <audio> 直接播放"""
        text = re.sub(r'\s+', ' ', (data.get('text') or '')).strip()
        if not text:
            return {'ok': False, 'error': '缺少 text'}
        if not CONFIG.get('tts_model'):
            return {'ok': False, 'error': '服务端未配置 tts_model（朗读已关闭）'}
        text = text[:400]
        base, key = self._speech_conf()
        msgs = [{'role': 'user', 'content': '请朗读下面这段话，不要改动内容'},
                {'role': 'assistant', 'content': text}]
        t0 = time.time()
        try:
            msg = call_llm(msgs, CONFIG['tts_model'], timeout=90, base_url=base, api_key=key, raw=True)
        except Exception as e:
            print(f'[TTS] 失败: {e}', file=sys.stderr, flush=True)
            return {'ok': False, 'error': str(e)}
        au = (msg or {}).get('audio') or {}
        b64 = au.get('data')
        if not b64:
            return {'ok': False, 'error': '模型没有返回音频数据'}
        ms = int((time.time() - t0) * 1000)
        print(f'[TTS] {len(text)}字 -> {ms}ms -> {len(b64)}B', file=sys.stderr, flush=True)
        return {'ok': True, 'audio': 'data:audio/wav;base64,' + b64,
                'transcript': au.get('transcript') or text, 'ms': ms}

    # ---------- 多端同步 ----------
    def _base_url(self):
        """本站对外的地址。HTTPS 端口开着就用 https —— 麦克风只在安全上下文里能用，
        给用户/agent 的地址必须是他真能用的那个。"""
        host = self.headers.get('Host') or ('127.0.0.1:%d' % PORT)
        if TLS_PORT and (':' + str(TLS_PORT)) in host:
            return 'https://' + host
        return 'http://' + host

    def _sync_params(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        board = board_key(q.get('board', ['default'])[0])
        try:
            rev = int(q.get('rev', ['0'])[0])
        except ValueError:
            rev = 0
        wait = 15.0
        try:
            wait = float(q.get('wait', ['15'])[0])
        except ValueError:
            pass
        client = (q.get('client', [''])[0] or '')[:40]
        return board, rev, max(0.0, min(wait, 25.0)), client

    def do_sync_get(self):
        """长轮询：等到 rev 变化或超时才返回"""
        board, rev, wait, client = self._sync_params()
        deadline = time.time() + wait
        while True:
            with SYNC_LOCK:
                cur = SYNC_BOARDS.get(board)
                peers = sync_touch_peer(board, client)
            cur_rev = int(cur.get('rev', 0)) if cur else 0
            if cur and cur_rev > rev:
                return {'ok': True, 'changed': True, 'rev': cur_rev, 'by': cur.get('by', ''),
                        'owner': board_owner(board),
                        'state': cur.get('state'), 'peers': peers}
            left = deadline - time.time()
            if left <= 0:
                return {'ok': True, 'changed': False, 'rev': cur_rev, 'peers': peers, 'board': board,
                        'owner': board_owner(board)}
            ev = sync_event(board)
            ev.wait(min(0.5, left))
            ev.clear()

    def do_sync_post(self, data):
        """提交画布状态：rev 落后 -> 冲突，把服务端最新状态回给客户端"""
        board = board_key(data.get('board'))
        client = str(data.get('client') or '')[:40]
        state = data.get('state')
        # 登录用户在这块板上动过手 -> 这块板就是他的（谁开的板就是谁的，agent 跟着板走）
        _u = user_by_token(data.get('token'))
        if _u:
            board_claim(board, _u)
        try:
            rev = int(data.get('rev', 0))
        except (TypeError, ValueError):
            rev = 0
        with SYNC_LOCK:
            cur = SYNC_BOARDS.get(board) or {}
            cur_rev = int(cur.get('rev', 0)) if cur else 0
            if rev < cur_rev:
                peers = sync_touch_peer(board, client)
                print(f'[SYNC] /{board} 冲突：客户端 rev={rev} < 服务端 rev={cur_rev}', file=sys.stderr, flush=True)
                return {'ok': False, 'conflict': True, 'rev': cur_rev,
                        'state': cur.get('state'), 'by': cur.get('by', ''), 'peers': peers}
            new_rev = cur_rev + 1
            if ('deltas' in data) or ('tombs' in data):
                # 新协议：元素级合并。多人同时画，各画各的互不覆盖。
                merged, st = sync_merge_els(board, client, cur, data, time.time())
                SYNC_BOARDS[board] = {'rev': new_rev, 'state': merged, 'by': client,
                                      'ts': time.time(), 'idx': st['idx'], 'tombs': st['tombs']}
                peers = sync_touch_peer(board, client)
                sync_save()
                n = st['els']
                stat = {'won': st['won'], 'lost': st['lost'], 'dead': st['dead']}
            else:
                # 老协议：整块替换（老客户端/脚本仍在用，保留不动）
                SYNC_BOARDS[board] = {'rev': new_rev, 'state': state, 'by': client, 'ts': time.time()}
                peers = sync_touch_peer(board, client)
                sync_save()
                n = len((state or {}).get('els') or []) if isinstance(state, dict) else 0
                stat = None
        sync_notify(board)
        if stat is None:
            print(f'[SYNC] /{board} rev={new_rev} 元素={n} 来自 {client or "?"} 在线={peers}',
                  file=sys.stderr, flush=True)
            return {'ok': True, 'changed': True, 'rev': new_rev, 'peers': peers, 'board': board,
                    'owner': board_owner(board)}
        print(f'[SYNC] /{board} rev={new_rev} 合并 收{stat["won"]} 拒{stat["lost"]} 删{stat["dead"]} '
              f'共{n}元素 来自 {client or "?"} 在线={peers}', file=sys.stderr, flush=True)
        return {'ok': True, 'changed': True, 'rev': new_rev, 'peers': peers, 'board': board,
                'owner': board_owner(board), 'merged': stat, 'state': merged}

    # ---------- 业务 ----------
    def _classify_intent(self, message):
        """判断是"要动手"还是"纯聊天"。分类失败时降级到关键词判断。"""
        try:
            raw = call_llm([{'role': 'system', 'content': SYSTEM_INTENT},
                            {'role': 'user', 'content': (message or '')[:300]}],
                           CONFIG['model'], timeout=20)
        except Exception as e:
            print(f'[WARN] 意图分类失败，降级关键词判断: {e}', file=sys.stderr)
            return looks_like_action(message)
        t = (raw or '').strip().lower()
        if 'command' in t and 'chat' not in t:
            return True
        if 'chat' in t and 'command' not in t:
            return False
        return looks_like_action(message)

    def _vision_describe(self, data_url, max_chars=900, kind='shot'):
        """让视觉模型把截图转述成文字（只描述，不要求输出 JSON）

        对话模型看不到图，只能看到这段转述 —— 所以"用户圈了什么"必须在第 1 项就被逼问出来。
        原来的提示词只问"画了哪些图形"，视觉模型经常把圈画当成普通线条一笔带过甚至漏掉，
        用户体感就是"我做的标记 AI 看不到"。
        """
        base = CONFIG.get('vision_base_url') or CONFIG.get('base_url')
        key = CONFIG.get('vision_api_key') or CONFIG.get('api_key')
        if kind == 'overview':
            # 整板概览：要的是"整体有什么、在哪"，不是逐字念
            prompt = ('这是同一块白板的整块缩略图。请用中文概述：1) 画板分成哪几块区域、'
                      '各有什么类型的图形或文字；2) 各部分的相对位置关系；3) 整体想表达什么。'
                      '不要逐字念文字，200 字以内。')
        else:
            prompt = ('这是一张白板截图。请用中文客观描述，严格按下面顺序：'
                      '1)【最重要】用户在图上的标注：有没有圈、框、箭头、下划线、手写批注或高亮？'
                      '有几个、分别用什么颜色、圈住或指向了哪一块内容？逐个说明，一个都不要漏；'
                      '2) 画了哪些图形（矩形/圆/箭头/手绘线条/贴图等）及其相对位置与连线关系；'
                      '3) 图上出现的所有文字（包括网页里的标题、按钮、表格文字，以及手写批注）；'
                      '4) 你判断用户想表达什么。只描述看到的内容，不要给建议，450 字以内。')
        msgs = [{'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            {'type': 'image_url', 'image_url': {'url': data_url}},
        ]}]
        text = call_llm(msgs, CONFIG['vision_model'], timeout=60, base_url=base, api_key=key)
        return (text or '').strip()[:max_chars]

    def vision_describe_cached(self, data_url, max_chars=900, kind='shot'):
        """同一张截图 3 分钟内复用结果：避免多轮对话反复调用视觉模型导致整体超时"""
        try:
            key = hashlib.md5((kind + '|' + data_url).encode('utf-8')).hexdigest()
        except Exception:
            return self._vision_describe(data_url, max_chars)
        now = time.time()
        hit = VISION_CACHE.get(key)
        if hit and now - hit[0] < 180:
            print('[VISION] 命中缓存，跳过看图', file=sys.stderr, flush=True)
            return hit[1]
        txt = self._vision_describe(data_url, max_chars, kind)
        VISION_CACHE[key] = (now, txt)
        if len(VISION_CACHE) > 8:
            for k, _v in sorted(VISION_CACHE.items(), key=lambda kv: kv[1][0])[:3]:
                VISION_CACHE.pop(k, None)
        return txt

    def do_act(self, data):
        """统一入口：意图判断 + 元素生成 + 视觉理解 + 协作上下文"""
        message = (data.get('message') or '').strip()
        canvas = data.get('canvas') or {}
        image = data.get('image') or None
        history = data.get('history') or []
        context = (data.get('context') or '').strip()[:200]
        if not message:
            return {'reply': '（空消息）', 'action': 'none', 'elements': []}

        canvas_desc, token_map = describe_canvas(canvas)
        # 附件：前端发消息时会带上 doc id，这里把内容读进上下文。
        # 注意注入到 user_text / chat_text 里（拼成普通文本），不走 sanitize_messages 的 4000 字截断 ——
        # 那份截断是给聊天历史用的，方案动辄上万字，塞进去会被砍掉一半。
        doc_note = ''
        _doc_id = str(data.get('doc') or '')[:40]
        _doc_bd = board_key(data.get('board'))
        if _doc_id or _doc_bd:
            _d = doc_get(_doc_bd, _doc_id) if _doc_id else (doc_read(_doc_bd, '', 1, 0) or {})
            if _d and _d.get('text'):
                _tx = _d['text']
                _cut = len(_tx) > DOC_INTO_AI
                doc_note = '\n\n【用户上传的方案：%s】\n%s%s' % (
                    _d.get('name') or '文档', _tx[:DOC_INTO_AI],
                    '\n（方案较长，这里只给了前 %d 字；需要后文可以让用户分段发）' % DOC_INTO_AI if _cut else '')
                print('[DOC] 把附件《%s》读进 AI 上下文（%d 字%s）' % (
                    _d.get('name'), min(len(_tx), DOC_INTO_AI), '，已截断' if _cut else ''),
                    file=sys.stderr, flush=True)


        want_vision = bool(image) and image.startswith('data:image') and bool(CONFIG.get('vision_model'))

        # 第一段：视觉模型只负责「看懂图并描述」，不要求它输出 JSON（它这方面不稳定）
        vision_note = ''
        if want_vision:
            try:
                vision_note = self._vision_describe(image)
            except Exception as e:
                print(f'[WARN] 视觉描述失败（继续走文本模型）: {e}', file=sys.stderr)

        # 关键一步：先判断这是"要我动手"还是"只是聊天"——聊天绝不生成任何绘图指令
        if not self._classify_intent(message):
            chat_text = f"【画布元素结构】\n{canvas_desc}"
            if context:
                chat_text = f"【协作上下文】{context}\n\n" + chat_text
            if vision_note:
                chat_text += f"\n\n【画布截图内容（视觉模型转述）】\n{vision_note}"
            chat_text += doc_note
            chat_text += f"\n\n【用户说】{message}"
            try:
                reply = call_llm([{'role': 'system', 'content': SYSTEM_PARTNER},
                                  {'role': 'user', 'content': chat_text}], CONFIG['model'], timeout=45)
            except Exception as e:
                reply = f'（我这边连不上模型：{str(e)[:60]}）'
            return {'reply': (reply or '').strip()[:400], 'action': 'none', 'ops': [],
                    'elements': [], 'context': context, 'model': CONFIG['model'],
                    'used_image': bool(vision_note), 'intent': 'chat'}

        # 第二段：始终用文本模型做「意图判断 + 元素生成」——DeepSeek 的 JSON 遵循度更好
        user_text = f"【画布元素结构】\n{canvas_desc}"
        if context:
            user_text = f"【协作上下文（之前确认过）】{context}\n\n" + user_text
        if vision_note:
            user_text += f"\n\n【画布截图内容（由视觉模型转述）】\n{vision_note}"
        user_text += doc_note
        user_text += f"\n\n【用户说】{message}"

        messages = [{'role': 'system', 'content': SYSTEM_ACT}]
        for h in history[-8:]:
            role = h.get('role')
            content = h.get('content')
            if role in ('user', 'assistant') and content:
                messages.append({'role': role, 'content': str(content)[:4000]})
        messages.append({'role': 'user', 'content': user_text})

        model = CONFIG['model']
        try:
            raw = call_llm(messages, model, timeout=45)
        except Exception as e:
            # 不再用模板乱画：直接如实告知
            return {'reply': f'AI 服务调用失败：{str(e)[:80]}',
                    'action': 'none', 'ops': [], 'elements': [],
                    'model': model, 'used_image': False, 'degraded': True}

        parsed = extract_json(raw)
        if not parsed:
            # 解析失败：绝不用模板乱画，只说人话
            note = raw.strip()[:300] if raw and raw.strip() else '我没太理解，能再说具体点吗？'
            return {'reply': note, 'action': 'none', 'ops': [], 'elements': [],
                    'model': model, 'used_image': bool(vision_note), 'degraded': True}

        # 新版结构（推荐）：nodes / edges / notes —— 坐标交给前端排版，避免模型瞎猜
        nodes, edges, notes = [], [], []
        raw_nodes = parsed.get('nodes')
        if isinstance(raw_nodes, list):
            for i, n in enumerate(raw_nodes[:12]):
                if not isinstance(n, dict):
                    continue
                shape = str(n.get('shape') or 'rect').lower()
                if shape not in ('rect', 'ellipse', 'diamond'):
                    shape = 'rect'
                nodes.append({'id': str(n.get('id') or ('n%d' % (i + 1))),
                              'label': str(n.get('label') or '')[:24],
                              'shape': shape})
        def _real(tok):
            tok = str(tok or '').strip()
            return token_map.get(tok.upper(), tok)

        raw_edges = parsed.get('edges')
        if isinstance(raw_edges, list):
            node_ids = {n['id'] for n in nodes}
            known = node_ids | set(token_map.values())
            for e in raw_edges[:20]:
                if not isinstance(e, dict):
                    continue
                a, b = _real(e.get('from')), _real(e.get('to'))
                if a and b and a != b and a in known and b in known:
                    edges.append({'from': a, 'to': b, 'label': str(e.get('label') or '')[:12]})

        # 对【已有元素】的就地修改：改文字 / 移动 / 删除
        edits = []
        raw_edits = parsed.get('edits')
        if isinstance(raw_edits, list):
            for e in raw_edits[:12]:
                if not isinstance(e, dict):
                    continue
                tgt = _real(e.get('target'))
                if not tgt:
                    continue
                item = {'target': tgt}
                if e.get('delete') is True:
                    item['delete'] = True
                if isinstance(e.get('text'), str):
                    item['text'] = e['text'][:60]
                for k in ('strokeColor', 'fillColor'):
                    if isinstance(e.get(k), str):
                        item[k] = e[k][:20]
                for k in ('dx', 'dy', 'x', 'y', 'width', 'height', 'fontSize'):
                    v = e.get(k)
                    if isinstance(v, (int, float)):
                        item[k] = float(v)
                if len(item) > 1:
                    edits.append(item)
        raw_notes = parsed.get('notes')
        if isinstance(raw_notes, list):
            for n in raw_notes[:3]:
                txt = n.get('text') if isinstance(n, dict) else n
                if txt:
                    notes.append({'text': str(txt)[:80]})
        layout = str(parsed.get('layout') or 'flow-right')
        if layout not in ('flow-right', 'flow-down', 'arch'):
            layout = 'flow-right'

        # 通用操作集（新版）：把 E 编号换成真实 id 后原样透传，由前端执行
        def _fix_tokens(obj):
            if not isinstance(obj, dict):
                return obj
            out = {}
            for k, v in obj.items():
                if k in ('target', 'from', 'to') and isinstance(v, str):
                    out[k] = _real(v)
                elif k == 'targets' and isinstance(v, list):
                    out[k] = [_real(str(x)) for x in v]
                elif k in ('set',) and isinstance(v, dict):
                    out[k] = _fix_tokens(v)
                elif k == 'edges' and isinstance(v, list):
                    out[k] = [_fix_tokens(e) for e in v]
                else:
                    out[k] = v
            return out

        ops = []
        raw_ops = parsed.get('ops')
        if isinstance(raw_ops, list):
            for o in raw_ops[:12]:
                if isinstance(o, dict) and o.get('op'):
                    ops.append(_fix_tokens(o))

        elements = normalize_elements(parsed.get('elements'))   # 兼容旧格式 / 自由摆放
        action = str(parsed.get('action') or 'none').lower()
        if action not in ('none', 'add', 'replace_all'):
            action = 'add' if (nodes or elements) else 'none'
        if action == 'none' and (nodes or elements):
            action = 'add'
        count = len(nodes) if nodes else len(elements)

        # 安全网：用户没有明确动作词 → 一律不动手（避免把闲聊画成图）
        if not looks_like_action(message) and (ops or nodes or edges or edits or elements):
            print(f'[INFO] 未检测到动作词，忽略模型的绘图指令：{message[:40]}', file=sys.stderr)
            ops, nodes, edges, notes, edits, elements = [], [], [], [], [], []
            action = 'none'
            count = 0

        new_ctx = parsed.get('context')
        if not isinstance(new_ctx, str) or not new_ctx.strip():
            new_ctx = context
        return {
            'reply': str(parsed.get('reply') or '').strip() or ('已生成 %d 个元素' % count),
            'context': new_ctx[:200],
            'action': action,
            'layout': layout,
            'nodes': nodes,
            'edges': edges,
            'notes': notes,
            'edits': edits,
            'ops': ops,
            'elements': elements,
            'model': model,
            'used_image': bool(vision_note),
        }

    def do_agent(self, data):
        """标准 tool-calling agent：把白板能力当工具，由模型自己决定何时调用"""
        messages = data.get('messages') or []
        canvas = data.get('canvas') or {}
        context = (data.get('context') or '').strip()[:200]
        image = data.get('image') or None

        canvas_desc, token_map = describe_canvas(canvas)
        vision_note = ''
        if image and image.startswith('data:image') and CONFIG.get('vision_model'):
            try:
                vision_note = self.vision_describe_cached(image, max_chars=900)
            except Exception as e:
                print(f'[WARN] agent 视觉描述失败: {e}', file=sys.stderr)

        # 整板概览：只看聚焦图的话，AI 不知道画板其余部分长什么样
        overview_note = ''
        overview = data.get('overview') or None
        if (overview and isinstance(overview, str) and overview.startswith('data:image')
                and overview != image and CONFIG.get('vision_model')):
            try:
                overview_note = self.vision_describe_cached(overview, max_chars=700, kind='overview')
            except Exception as e:
                print(f'[WARN] 概览图描述失败: {e}', file=sys.stderr)
        marks_desc = describe_marks(canvas)
        # 附件：前端发消息时带上 doc id，这里读进上下文。
        # 放在系统提示里（sanitize_messages 只截 user 消息，系统消息不截），方案上万字也不会被砍。
        doc_note = ''
        _doc_id = str(data.get('doc') or '')[:40]
        _doc_bd = board_key(data.get('board'))
        if _doc_id:
            _d = doc_get(_doc_bd, _doc_id)
            if _d and _d.get('text'):
                _tx = _d['text']
                _cut = len(_tx) > DOC_INTO_AI
                doc_note = ('\n\n【用户上传的方案：%s】\n%s%s' % (
                    _d.get('name') or '文档', _tx[:DOC_INTO_AI],
                    '\n（方案较长，这里只给前 %d 字；需要后文可以让用户分批发）' % DOC_INTO_AI if _cut else ''))
                print('[DOC] agent 路径读入附件《%s》（%d 字%s）' % (
                    _d.get('name'), min(len(_tx), DOC_INTO_AI), '，已截断' if _cut else ''),
                    file=sys.stderr, flush=True)
        # 历史里模型自己以前说过的"我读不到图片"，会在上下文里形成惯性，让它照旧回答
        # （用户连续几轮都得到"我看不到"就是这个原因）。这里统计出来，日志里一眼可见。
        DENY = ('读不到图片', '看不到图片', '读不出来', '黑盒', '能力边界', '贴到聊天框')
        old_denials = 0
        for _m in (messages or []):
            if isinstance(_m, dict) and _m.get('role') == 'assistant':
                _c = str(_m.get('content') or '')
                if any(k in _c for k in DENY):
                    old_denials += 1
        _imgs = [e for e in ((canvas or {}).get('elements') or []) if e.get('type') == 'image']
        print('[AGENT] 附图=%s 视觉转述=%d字 概览=%d字 标注=%d条 底图=%s 画布=%d元素 历史旧结论=%d条' % (
            ('%dKB' % (len(image) // 1024)) if image else '无',
            len(vision_note or ''), len(overview_note or ''), marks_desc.count('- 标注'),
            ('%d张/聚焦%s' % (len(_imgs), (canvas or {}).get('focusImageId') or '?')) if _imgs else '无',
            len((canvas or {}).get('elements') or []), old_denials),
            file=sys.stderr, flush=True)
        sys_text = (SYSTEM_AGENT
                    .replace('{canvas}', canvas_desc)
                    .replace('{context}', context or '（暂无）')
                    .replace('{overview}', overview_note or '（无）')
                    .replace('{marks}', marks_desc)
                    .replace('{vision}', (VISION_FRAME % vision_note) if vision_note else
                             ('（本轮没有附图，你看不到画布画面；不要猜测图里画了什么，'
                              '需要看图就请用户再问一次）')))
        sys_text += doc_note          # 附件内容（可能很长）挂在系统提示后面
        msgs = [{'role': 'system', 'content': sys_text}]
        for m in sanitize_messages(messages[-40:]):
            msgs.append(m)

        try:
            msg = call_llm(msgs, CONFIG['model'], timeout=60, tools=WB_TOOLS, raw=True)
        except Exception as e:
            print(f'[AGENT][ERROR] {e}', file=sys.stderr, flush=True)
            return {'content': f'（模型调用失败：{str(e)[:80]}）', 'tool_calls': [], 'context': context}

        def _real(tok):
            tok = str(tok or '').strip()
            return token_map.get(tok.upper(), tok)

        calls = []
        for c in (msg.get('tool_calls') or []):
            fn = c.get('function') or {}
            name = fn.get('name') or ''
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except Exception:
                args = {}
            if isinstance(args, dict):
                if 'id' in args:
                    args['id'] = _real(args['id'])
                if 'target' in args:
                    args['target'] = _real(args['target'])
                if 'from' in args:
                    args['from'] = _real(args['from'])
                if 'to' in args:
                    args['to'] = _real(args['to'])
                if isinstance(args.get('ids'), list):
                    args['ids'] = [_real(x) for x in args['ids']]
            calls.append({'id': c.get('id') or ('call_%d' % len(calls)),
                          'type': 'function',
                          'function': {'name': name,
                                       'arguments': json.dumps(args, ensure_ascii=False)}})

        print('[AGENT] 收到 %d 条消息 -> 工具=%s，文本=%d 字' % (
            len(msgs), [c['function']['name'] for c in calls] or '(无)',
            len(msg.get('content') or '')), file=sys.stderr, flush=True)

        return {'content': (msg.get('content') or '').strip(),
                'tool_calls': calls,
                'context': context,
                'model': CONFIG['model'],
                'used_image': bool(vision_note)}

    def do_review(self, data):
        """主动审阅：只在发现明显缺口时才开口（返回的 ops 仅限圈注类，避免擅自改图）"""
        canvas = data.get('canvas') or {}
        image = data.get('image') or None
        context = (data.get('context') or '').strip()[:200]
        elements = (canvas or {}).get('elements') or []
        if len(elements) < 2:
            return {'reply': '', 'ops': [], 'context': context, 'skip': '画布元素太少'}

        canvas_desc, token_map = describe_canvas(canvas)
        vision_note = ''
        if image and image.startswith('data:image') and CONFIG.get('vision_model'):
            try:
                vision_note = self._vision_describe(image, max_chars=900)
            except Exception as e:
                print(f'[WARN] 审阅时的视觉描述失败: {e}', file=sys.stderr)

        user_text = f"【画布元素结构】\n{canvas_desc}"
        if context:
            user_text = f"【协作上下文】{context}\n\n" + user_text
        if vision_note:
            user_text += f"\n\n【截图内容（视觉模型转述）】\n{vision_note}"
        user_text += "\n\n请判断是否需要主动提醒用户。"

        try:
            raw = call_llm([{'role': 'system', 'content': SYSTEM_REVIEW},
                            {'role': 'user', 'content': user_text}], CONFIG['model'], timeout=45)
        except Exception as e:
            return {'reply': '', 'ops': [], 'context': context, 'error': str(e)[:80]}

        parsed = extract_json(raw) or {}

        def _real(tok):
            tok = str(tok or '').strip()
            return token_map.get(tok.upper(), tok)

        ops = []
        for o in (parsed.get('ops') or [])[:3]:
            if not isinstance(o, dict):
                continue
            op = str(o.get('op') or '').lower()
            if op == 'annotate':
                ops.append({'op': 'annotate',
                            'shape': o.get('shape') if o.get('shape') in ('circle', 'arrow') else 'circle',
                            'target': _real(o.get('target')),
                            'text': str(o.get('text') or '')[:40],
                            'color': '#e03131'})
            elif op == 'select':
                ops.append({'op': 'select', 'targets': [_real(str(x)) for x in (o.get('targets') or [])[:3]]})
            elif op == 'zoom':
                ops.append({'op': 'zoom', 'fit': bool(o.get('fit'))})

        reply = str(parsed.get('reply') or '').strip()[:200]
        new_ctx = str(parsed.get('context') or '').strip()[:200] or context
        return {'reply': reply, 'ops': (ops if reply else []), 'context': new_ctx}

    def do_chat(self, message, canvas):
        """旧接口：不再提供"只分析"的能力，改为提示切换到新版（防止旧页面造成"AI 不能操作画布"的误解）"""
        print('[LEGACY] 命中旧接口 /api/ai/chat —— 客户端仍在跑旧页面', file=sys.stderr, flush=True)
        return {'reply': LEGACY_NOTICE, 'legacy': True}

    def do_draw(self, description):
        """旧接口：不再用模板乱画，改为提示切换到新版"""
        print('[LEGACY] 命中旧接口 /api/ai/draw —— 模板画图已废弃', file=sys.stderr, flush=True)
        return {'action': 'none', 'elements': [], 'reply': LEGACY_NOTICE, 'legacy': True,
                'description': description}


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    load_todos()
    if not CONFIG.get('api_key'):
        print('[WARN] 未检测到 API Key：请先设置环境变量 AI_API_KEY（或写 ai_config.json）。'
              '当前仍可启动，但 AI 接口会返回错误。', file=sys.stderr)
    print(f'AI白板服务器 v2: http://0.0.0.0:{PORT}')
    print(f'  模型: {CONFIG["model"]} / 视觉: {CONFIG["vision_model"]} / Key: {"已配置" if CONFIG.get("api_key") else "缺失"}')
    print(f'  本机访问（127.0.0.1）无需令牌；其它设备访问需要令牌：{ACCESS_TOKEN}')
    print(f'  （令牌也存在 {TOKEN_PATH}，页面会提示你输入）')
    # 可选：再开一个 HTTPS 端口。
    # 为什么需要：浏览器只在安全上下文（HTTPS 或 localhost）下才给麦克风 ——
    # 内网明文 HTTP 下 navigator.mediaDevices 直接不存在，语音输入永远用不了。
    # 用 WB_TLS_PORT + WB_TLS_CERT + WB_TLS_KEY 开启，与 HTTP 端口并存。
    try:
        tls_port = int(os.environ.get('WB_TLS_PORT') or 0)
        tls_cert = os.environ.get('WB_TLS_CERT') or ''
        tls_key = os.environ.get('WB_TLS_KEY') or ''
        if tls_port and os.path.exists(tls_cert) and os.path.exists(tls_key):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tls_cert, tls_key)
            httpsd = ReusableTCPServer(('0.0.0.0', tls_port), Handler)
            httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
            threading.Thread(target=httpsd.serve_forever, daemon=True).start()
            print(f'  HTTPS: https://0.0.0.0:{tls_port} （语音输入需要走这个端口）')
        elif tls_port:
            print('  [WARN] 配了 WB_TLS_PORT 但证书文件不存在，HTTPS 未开启', file=sys.stderr)
    except Exception as e:
        print(f'  [WARN] HTTPS 启动失败：{e}', file=sys.stderr)

    try:
        with ReusableTCPServer(('0.0.0.0', PORT), Handler) as httpd:
            httpd.serve_forever()
    except OSError as e:
        print(f'[ERROR] 端口 {PORT} 被占用或无法绑定：{e}\n'
              f'        先结束旧进程（Windows: netstat -ano | findstr :{PORT} 然后 taskkill /PID <pid> /F）'
              f'，或换端口：set PORT=9092', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
