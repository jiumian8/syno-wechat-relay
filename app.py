import os
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# 从环境变量获取企业微信配置
CORPID = os.getenv('CORPID')
CORPSECRET = os.getenv('CORPSECRET')
AGENTID = os.getenv('AGENTID')
TOUSER = os.getenv('TOUSER', '@all') # 默认发送给所有人

# Token 缓存
access_token = None
token_expires_at = 0

def get_access_token():
    global access_token, token_expires_at
    # 如果 token 还在有效期内，直接使用缓存
    if time.time() < token_expires_at and access_token:
        return access_token

    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={CORPID}&corpsecret={CORPSECRET}"
    resp = requests.get(url).json()
    
    if resp.get('errcode') == 0:
        access_token = resp['access_token']
        # 提前 200 秒过期以确保稳定性
        token_expires_at = time.time() + resp['expires_in'] - 200
        return access_token
    else:
        raise Exception(f"获取 Token 失败: {resp}")

def send_wechat_msg(content):
    token = get_access_token()
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    
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
    # 尝试解析群晖发来的 JSON 数据
    content = ""
    if request.is_json:
        data = request.json
        # 兼容 content 或 text 字段
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