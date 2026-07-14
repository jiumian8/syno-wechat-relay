import os
import time
import requests
import urllib3
from flask import Flask, request, jsonify
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise.exceptions import InvalidCorpIdException
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.enterprise import parse_message, create_reply

# 禁用 requests 的不安全请求警告 (对应 curl 中的 --insecure)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# 企业微信基础配置
CORPID = os.getenv('CORPID')
CORPSECRET = os.getenv('CORPSECRET')
AGENTID = os.getenv('AGENTID')
TOUSER = os.getenv('TOUSER', '@all')
QYAPI_URL = os.getenv('QYAPI_URL', 'https://qyapi.weixin.qq.com').rstrip('/')

# 企业微信回调配置 (在自建应用设置中获取)
WECHAT_TOKEN = os.getenv('WECHAT_TOKEN', '')
WECHAT_AESKEY = os.getenv('WECHAT_AESKEY', '')

# PVE 配置
PVE_URL = os.getenv('PVE_URL', 'https://192.168.5.100:8006').rstrip('/')
PVE_USER = os.getenv('PVE_USER', 'root@pam')
PVE_PASS = os.getenv('PVE_PASS', 'djm123456')
PVE_NODE = os.getenv('PVE_NODE', 'jiumian')

# 全局缓存
access_token = None
token_expires_at = 0
pve_ticket_cache = None
pve_csrf_cache = None
pve_ticket_expires_at = 0

# ==================== 企业微信发信逻辑 ====================

def get_access_token():
    global access_token, token_expires_at
    if time.time() < token_expires_at and access_token:
        return access_token
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
    url = f"{QYAPI_URL}/cgi-bin/message/send?access_token={token}"
    payload = {
        "touser": TOUSER,
        "msgtype": "text",
        "agentid": int(AGENTID),
        "text": {"content": content}
    }
    return requests.post(url, json=payload).json()

# ==================== PVE 查询逻辑 ====================

def get_pve_auth():
    global pve_ticket_cache, pve_csrf_cache, pve_ticket_expires_at
    # 简单缓存 PVE Ticket (有效期通常约 2 小时)
    if time.time() < pve_ticket_expires_at and pve_ticket_cache:
        return pve_ticket_cache, pve_csrf_cache

    url = f"{PVE_URL}/api2/extjs/access/ticket"
    data = {'username': PVE_USER, 'password': PVE_PASS, 'realm': 'pam'}
    resp = requests.post(url, data=data, verify=False).json()
    
    if resp.get('data'):
        pve_ticket_cache = resp['data']['ticket']
        pve_csrf_cache = resp['data']['CSRFPreventionToken']
        pve_ticket_expires_at = time.time() + 3600  # 缓存 1 小时
        return pve_ticket_cache, pve_csrf_cache
    else:
        return None, None

def query_pve_status():
    ticket, csrf = get_pve_auth()
    if not ticket:
        return "⚠️ PVE 登录失败，请检查账号密码或网络连接。"

    url = f"{PVE_URL}/api2/json/nodes/{PVE_NODE}/status"
    headers = {'CSRFPreventionToken': csrf}
    cookies = {'PVEAuthCookie': ticket}
    
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, verify=False).json()
        data = resp.get('data', {})
        
        # 提取关键信息，温度信息依赖 PVE 是否配置了 sensors
        cpu = data.get('cpu', 0) * 100
        memory_used = data.get('memory', {}).get('used', 0) / (1024**3)
        memory_total = data.get('memory', {}).get('total', 0) / (1024**3)
        
        # 尝试获取温度 (若你的 PVE 扩展了温度显示，通常在 cpuinfo 或 sensors 字段中)
        temp_info = data.get('temperature', '未获取到硬件温度数据(需PVE安装sensors)')
        if 'cpuinfo' in data and 'hwpkg' in data['cpuinfo']:
             temp_info = f"{data['cpuinfo']['hwpkg']} °C"

        result = (
            f"🖥️ 节点 [{PVE_NODE}] 概况\n"
            f"----------------------\n"
            f"🌡️ 温度/状态: {temp_info}\n"
            f"📊 CPU: {cpu:.1f}%\n"
            f"🧠 内存: {memory_used:.1f}GB / {memory_total:.1f}GB\n"
            f"⏱️ 运行时间: {data.get('uptime', 0) // 3600} 小时"
        )
        return result
    except Exception as e:
        return f"查询 PVE 状态异常: {str(e)}"

def query_pve_vms():
    ticket, csrf = get_pve_auth()
    if not ticket:
        return "⚠️ PVE 登录失败"

    url = f"{PVE_URL}/api2/json/cluster/resources"
    headers = {'CSRFPreventionToken': csrf}
    cookies = {'PVEAuthCookie': ticket}
    
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, verify=False).json()
        resources = resp.get('data', [])
        
        vms = [res for res in resources if res.get('type') in ['qemu', 'lxc']]
        if not vms:
            return "未查询到虚拟机数据。"
        
        result_lines = ["📦 虚拟机/LXC 状态\n----------------------"]
        for vm in sorted(vms, key=lambda x: x.get('vmid', 0)):
            vmid = vm.get('vmid')
            name = vm.get('name', 'Unknown')
            status = vm.get('status', 'unknown')
            icon = "🟢" if status == "running" else "🔴"
            result_lines.append(f"{icon} {vmid} - {name} ({status})")
            
        return "\n".join(result_lines)
    except Exception as e:
        return f"查询虚拟机异常: {str(e)}"

# ==================== 路由控制 ====================

# 1. 接收群晖通知的主动群发接口
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

# 2. 核心：企业微信回调与交互接口
@app.route('/wechat', methods=['GET', 'POST'])
def wechat_callback():
    signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    
    if not all([WECHAT_TOKEN, WECHAT_AESKEY, CORPID]):
        return "Webhook not properly configured", 500
        
    crypto = WeChatCrypto(WECHAT_TOKEN, WECHAT_AESKEY, CORPID)

    # 企业微信配置 URL 时的 GET 验证请求
    if request.method == 'GET':
        echostr = request.args.get("echostr", "")
        try:
            echo_str = crypto.check_signature(signature, timestamp, nonce, echostr)
            return echo_str
        except InvalidSignatureException:
            return "Invalid signature", 403

    # 用户在企业微信发消息/点菜单的 POST 请求
    elif request.method == 'POST':
        try:
            # 解密消息
            decrypted_xml = crypto.decrypt_message(request.data, signature, timestamp, nonce)
            msg = parse_message(decrypted_xml)
            reply_content = ""
            
            # 处理文字输入 (比如你在聊天框发送"温度"或"虚拟机")
            if msg.type == 'text':
                content = msg.content.strip()
                if content == "温度":
                    reply_content = query_pve_status()
                elif content == "虚拟机":
                    reply_content = query_pve_vms()
                else:
                    reply_content = "💡 回复关键字获取信息：\n- 输入【温度】查询 PVE 概况\n- 输入【虚拟机】查询状态列表"
            
            # 返回加密的 XML 结果给企业微信
            if reply_content:
                reply = create_reply(reply_content, msg)
                return crypto.encrypt_message(reply.render(), nonce, timestamp)
            return "success"
            
        except Exception as e:
            # 无论发生什么报错，都要返回 success 给企业微信，避免其无限重试报错
            print(f"WeChat Callback Error: {e}")
            return "success"

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
