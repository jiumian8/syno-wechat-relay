import os
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# 从环境变量获取企业微信配置
CORPID = os.getenv('CORPID')
CORPSECRET = os.getenv('CORPSECRET')
AGENTID = os.getenv('AGENTID')
TOUSER = os.getenv('TOUSER', '@all')

# 新增：企业微信 API 代理地址，默认兜底使用官方地址
QYAPI_URL = os.getenv('QYAPI_URL', 'https://qyapi.weixin.qq.com')
# 确保 URL 末尾没有斜杠
QYAPI_URL = QYAPI_URL.rstrip('/')

# Token 缓存
access_token = None
token_expires_at = 0

def get_access_token():
    global access_token, token_expires_at
    if time.time() < token_expires_at and access_token:
        return access_token

    # 这里使用替换后的代理 URL
    url = f"{QYAPI_URL}/cgi-bin/gettoken?corpid={CORPID}&corpsecret={CORPSECRET}"
    resp = requests.get(url).json()
    
    if resp.get('errcode') == 0:
        access_token = resp['access_token']
        token_expires_at = time.time() + resp['expires_in'] - 200
        return access_token
    else:
        raise Exception(f"获取 Token 失败: {resp}")

def send_wechat_msg(content):
    token = get_access_token()
    # 这里使用替换后的代理 URL
    url = f"{QYAPI_URL}/cgi-bin/message/send?access_token={token}"
    
    payload = {
        "touser": TOUSER,
        "msgtype": "text",
        "agentid": int(AGENTID),
        "text": {
            "content": content
        }
    }
    
    resp = requests.post(url, json=payload).json()
    return resp

@app.route('/webhook', methods=['POST'])
def webhook():
    content = ""
    if request.is_json:
        data = request.json
        content = data.get('text') or data.get('content') or str(data)
    else:
        content = request.values.get('text') or request.get_data(as_text=True)

    if not content:
        return jsonify({"errcode": 400, "errmsg": "未找到消息内容"}), 400

    try:
        res = send_wechat_msg(content)
        return jsonify(res)
    except Exception as e:
        return jsonify({"errcode": 500, "errmsg": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
