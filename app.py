import os
import time
import requests
import urllib3
import re
from flask import Flask, request, jsonify
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise.exceptions import InvalidCorpIdException
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.enterprise import parse_message, create_reply

# 禁用 requests 的不安全请求警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ==================== 环境变量配置 ====================

# 企业微信基础配置
CORPID = os.getenv('CORPID')
CORPSECRET = os.getenv('CORPSECRET')
AGENTID = os.getenv('AGENTID')
TOUSER = os.getenv('TOUSER', '@all')
QYAPI_URL = os.getenv('QYAPI_URL', 'https://qyapi.weixin.qq.com').rstrip('/')

# 企业微信回调配置
WECHAT_TOKEN = os.getenv('WECHAT_TOKEN', '')
WECHAT_AESKEY = os.getenv('WECHAT_AESKEY', '')

# PVE 连接配置
PVE_URL = os.getenv('PVE_URL', 'https://192.168.5.100:8006').rstrip('/')
PVE_USER = os.getenv('PVE_USER', 'root@pam')
PVE_PASS = os.getenv('PVE_PASS', 'djm123456')
PVE_NODE = os.getenv('PVE_NODE', 'jiumian')

# ==================== 全局缓存 ====================
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
    if time.time() < pve_ticket_expires_at and pve_ticket_cache:
        return pve_ticket_cache, pve_csrf_cache

    url = f"{PVE_URL}/api2/extjs/access/ticket"
    
    # 自动分离用户名和认证域
    req_username = PVE_USER
    req_realm = 'pam'
    if '@' in PVE_USER:
        req_username, req_realm = PVE_USER.split('@', 1)
        
    data = {
        'username': req_username, 
        'password': PVE_PASS, 
        'realm': req_realm,
        'new-format': '1'
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'X-Requested-With': 'XMLHttpRequest'
    }
    
    try:
        resp = requests.post(url, data=data, headers=headers, verify=False, timeout=10).json()
        if resp.get('data'):
            pve_ticket_cache = resp['data']['ticket']
            pve_csrf_cache = resp['data']['CSRFPreventionToken']
            pve_ticket_expires_at = time.time() + 3600
            return pve_ticket_cache, pve_csrf_cache
        else:
            print(f"PVE 登录被拒绝，返回内容: {resp}")
            return None, None
    except Exception as e:
        print(f"PVE 网络连接异常: {e}")
        return None, None

def query_pve_status():
    ticket, csrf = get_pve_auth()
    if not ticket:
        return "⚠️ PVE 登录失败，请检查账号密码或网络连通性。"

    url = f"{PVE_URL}/api2/json/nodes/{PVE_NODE}/status"
    headers = {'CSRFPreventionToken': csrf}
    cookies = {'PVEAuthCookie': ticket}
    
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, verify=False, timeout=10).json()
        data = resp.get('data', {})
        
        # 1. CPU & 内存基础数据
        cpu_model = data.get('cpuinfo', {}).get('model', '未知型号')
        cpu_cores = data.get('cpuinfo', {}).get('cpus', 0)
        cpu_usage = data.get('cpu', 0) * 100
        
        mem_used = data.get('memory', {}).get('used', 0) / (1024**3)
        mem_total = data.get('memory', {}).get('total', 0) / (1024**3)
        mem_percent = (mem_used / mem_total * 100) if mem_total > 0 else 0
        
        # 2. 解析 sensors_info (温度与风扇)
        sensors = data.get('sensors_info', '')
        
        cpu_temp_match = re.search(r'Package id 0:\s+\+([\d\.]+)', sensors)
        cpu_temp = cpu_temp_match.group(1) + '°C' if cpu_temp_match else '未知'
        
        board_temp_match = re.search(r'acpitz-acpi-0[\s\S]*?temp1:\s+\+([\d\.]+)', sensors)
        board_temp = board_temp_match.group(1) + '°C' if board_temp_match else '未知'
        
        fans = re.findall(r'fan\d+:\s+(\d+)\s+RPM', sensors)
        active_fans = [f for f in fans if int(f) > 0]
        fan_speed = f"{active_fans[0]} RPM" if active_fans else "停转"
        
        # 3. 解析硬盘状态 (智能搜索所有动态键名)
        disk_raw_data = ""
        for key, value in data.items():
            if isinstance(value, str) and 'PVEASSIST_DISK_BEGIN' in value:
                disk_raw_data += value + "\n"
                
        disks = []
        disk_blocks = re.findall(r'PVEASSIST_DISK_BEGIN(.*?)PVEASSIST_DISK_END', disk_raw_data, re.DOTALL)
        for block in disk_blocks:
            # 兼容 NVMe 和 SATA 硬盘的字段
            model_match = re.search(r'(?:Model Number|Device Model):\s+(.+)', block)
            temp_match = re.search(r'Temperature:\s+(\d+)', block)
            used_match = re.search(r'Percentage Used:\s+(\d+)', block)
            cap_match = re.search(r'Capacity:\s+\[(.*?)\]', block)
            
            if model_match:
                model = model_match.group(1).strip()
                temp = temp_match.group(1) + '°C' if temp_match else '未知'
                cap = cap_match.group(1) if cap_match else '未知'
                life = str(100 - int(used_match.group(1))) + '%' if used_match else '未知'
                disks.append(f"💽 {model}\n  ├─ 容量: {cap}\n  └─ 温度: {temp} | 寿命: {life}")
        
        disk_str = "\n".join(disks) if disks else "未获取到硬盘数据"

        # 4. 组装最终排版结果
        result = (
            f"🖥️ 节点 [{PVE_NODE}] 概况\n"
            f"----------------------\n"
            f"⚙️ CPU: {cpu_cores}核 {cpu_model}\n"
            f"📊 负载: CPU {cpu_usage:.1f}% | 内存 {mem_percent:.1f}%\n"
            f"🧠 内存: {mem_used:.1f}GB / {mem_total:.1f}GB\n"
            f"----------------------\n"
            f"🌡️ 温度: CPU {cpu_temp} | 主板 {board_temp}\n"
            f"🌀 风扇: {fan_speed}\n"
            f"----------------------\n"
            f"{disk_str}"
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
        resp = requests.get(url, headers=headers, cookies=cookies, verify=False, timeout=10).json()
        resources = resp.get('data', [])
        
        # 筛选出虚拟机和LXC容器
        vms = [res for res in resources if res.get('type') in ['qemu', 'lxc']]
        if not vms:
            return "未查询到虚拟机数据。"
        
        result_lines = ["📦 虚拟机/LXC 运行状态\n----------------------"]
        
        # 按 VMID 排序
        for vm in sorted(vms, key=lambda x: x.get('vmid', 0)):
            vmid = vm.get('vmid')
            name = vm.get('name', 'Unknown')
            status = vm.get('status', 'unknown')
            tags = vm.get('tags', '')
            
            # 资源数据计算
            maxcpu = vm.get('maxcpu', 0)
            cpu_usage = vm.get('cpu', 0) * 100
            
            mem_used = vm.get('mem', 0) / (1024**3)
            maxmem = vm.get('maxmem', 0) / (1024**3)
            mem_percent = (mem_used / maxmem * 100) if maxmem > 0 else 0
            
            uptime = vm.get('uptime', 0)
            uptime_hrs = uptime // 3600
            uptime_mins = (uptime % 3600) // 60
            
            # 状态处理与排版
            if status == 'running':
                icon = "🟢"
                status_text = f"运行中 ({uptime_hrs}h{uptime_mins}m)"
            elif status == 'stopped':
                icon = "🔴"
                status_text = "已关机"
                # 关机状态下置零显示
                cpu_usage, mem_percent = 0, 0
            else:
                icon = "🟡"
                status_text = status

            # 组装单台虚拟机的信息
            result_lines.append(f"{icon} [{vmid}] {name}")
            if tags:
                result_lines.append(f"  ├─ IP/标签: {tags}")
            result_lines.append(f"  ├─ 状态: {status_text}")
            result_lines.append(f"  ├─ 规格: {maxcpu}核 | {maxmem:.1f}GB")
            result_lines.append(f"  └─ 负载: CPU {cpu_usage:.1f}% | 内存 {mem_percent:.1f}%\n")
            
        return "\n".join(result_lines).strip()
    except Exception as e:
        return f"查询虚拟机异常: {str(e)}"

# ==================== Web 路由控制 ====================

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

@app.route('/wechat', methods=['GET', 'POST'])
def wechat_callback():
    signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    
    if not all([WECHAT_TOKEN, WECHAT_AESKEY, CORPID]):
        return "Webhook not properly configured", 500
        
    crypto = WeChatCrypto(WECHAT_TOKEN, WECHAT_AESKEY, CORPID)

    if request.method == 'GET':
        echostr = request.args.get("echostr", "")
        try:
            echo_str = crypto.check_signature(signature, timestamp, nonce, echostr)
            return echo_str
        except InvalidSignatureException:
            return "Invalid signature", 403

    elif request.method == 'POST':
        try:
            decrypted_xml = crypto.decrypt_message(request.data, signature, timestamp, nonce)
            msg = parse_message(decrypted_xml)
            reply_content = ""
            
            if msg.type == 'text':
                content = msg.content.strip()
                if content == "温度":
                    reply_content = query_pve_status()
                elif content == "虚拟机":
                    reply_content = query_pve_vms()
                else:
                    reply_content = "💡 回复关键字获取信息：\n- 输入【温度】查询 PVE 概况\n- 输入【虚拟机】查询状态列表"
            
            if reply_content:
                reply = create_reply(reply_content, msg)
                return crypto.encrypt_message(reply.render(), nonce, timestamp)
            return "success"
            
        except Exception as e:
            print(f"WeChat Callback Error: {e}")
            return "success"

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

if __name__ == '__main__':
    # 使用 1500 端口配合 host 网络模式，避免与群晖 5000 端口冲突
    app.run(host='0.0.0.0', port=1500)
