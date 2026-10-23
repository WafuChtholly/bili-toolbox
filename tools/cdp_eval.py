"""在已开启 9223 调试口的进程里执行 JS：python tools/cdp_eval.py <url子串> <表达式文件或表达式>"""
import json
import sys

import requests
import websocket


def targets(port=9223):
    return requests.get(f'http://127.0.0.1:{port}/json/list', timeout=5).json()


def evaluate(ws_url, expr, timeout=20):
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
            'expression': expr, 'awaitPromise': True, 'returnByValue': True,
            'userGesture': True,
        }}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get('id') == 1:
                return msg
    finally:
        ws.close()


def main():
    match = sys.argv[1] if len(sys.argv) > 1 else ''
    expr = sys.argv[2] if len(sys.argv) > 2 else '1+1'
    for t in targets():
        if t['type'] == 'page' and match in t['url']:
            print(f"[target] {t['url']}")
            print(json.dumps(evaluate(t['webSocketDebuggerUrl'], expr), ensure_ascii=False, indent=2))
            return
    raise SystemExit(f'no page target matching {match!r}')


if __name__ == '__main__':
    main()
