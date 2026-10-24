"""Flask phone-style SMS simulator for sms_hospital_finder.py
Run:  pip install flask pandas numpy rapidfuzz shapely   then   python app.py
Open http://127.0.0.1:5000. Build the data first:  python sms_hospital_finder.py build
Keep this file in the same folder as sms_hospital_finder.py.
"""
import time, uuid
from xml.sax.saxutils import escape
from flask import Flask, request, jsonify, render_template_string, session, Response

from sms_hospital_finder import Store, Session

app = Flask(__name__)
app.secret_key = "change-me"            # needed for the per-browser session cookie

STORE = Store()
SESSIONS = {}                           # conversation state, keyed by browser id or phone number

def get_session(key):
    if key not in SESSIONS:
        SESSIONS[key] = Session(STORE, wrap_width=None)   # the browser wraps text itself
    return SESSIONS[key]

# ---------- simulated phone ----------
@app.route("/")
def index():
    session.setdefault("sid", uuid.uuid4().hex)
    return render_template_string(PAGE)

@app.route("/send", methods=["POST"])
def send():
    sid = session.setdefault("sid", uuid.uuid4().hex)
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    sess = get_session(sid)
    t0 = time.perf_counter()
    reply = sess.send(text)
    ms = (time.perf_counter() - t0) * 1000       # processing time for your evaluation
    return jsonify(reply=reply, follow_up=sess.last_follow_up, ms=round(ms, 2))

@app.route("/reset", methods=["POST"])
def reset():
    SESSIONS.pop(session.get("sid"), None)
    return jsonify(ok=True)

# ---------- optional: real SMS through Twilio ----------
@app.route("/sms", methods=["POST"])
def twilio_webhook():
    """Point your Twilio number's 'A message comes in' webhook at https://<public-url>/sms"""
    sender = request.form.get("From", "unknown")
    sess = get_session(sender)
    reply = sess.send(request.form.get("Body", ""))
    msgs = [reply] + ([sess.last_follow_up] if sess.last_follow_up else [])
    body = "".join(f"<Message>{escape(m)}</Message>" for m in msgs)
    return Response(f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>",
                    mimetype="application/xml")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ResponSMS Simulator</title>
<style>
  :root { --bg:#e9edf2; --phone:#111; --screen:#fff; --me:#0b84ff; --them:#e5e5ea; --text:#111; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14171c; --screen:#1c1c1e; --them:#2c2c2e; --text:#f2f2f7; }
  }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg);
         font-family:-apple-system,"Segoe UI",Roboto,sans-serif; color:var(--text); }
  .phone { width:min(380px,100vw); height:min(760px,100vh); background:var(--phone);
           border-radius:38px; padding:12px; box-shadow:0 20px 50px rgba(0,0,0,.35); }
  .screen { height:100%; background:var(--screen); border-radius:28px; display:flex;
            flex-direction:column; overflow:hidden; }
  header { padding:14px 16px; text-align:center; border-bottom:1px solid rgba(128,128,128,.25); }
  header b { display:block; font-size:15px; } header span { font-size:12px; opacity:.6; }
  #chat { flex:1; overflow-y:auto; padding:14px; display:flex; flex-direction:column; gap:8px; }
  .bubble { max-width:85%; padding:9px 13px; border-radius:18px; font-size:15px; line-height:1.35;
            white-space:pre-wrap; overflow-wrap:anywhere; }
  .bubble a { color:inherit; text-decoration:underline; }
  .me { align-self:flex-end; background:var(--me); color:#fff; border-bottom-right-radius:5px; }
  .them { align-self:flex-start; background:var(--them); border-bottom-left-radius:5px; }
  .meta { font-size:11px; opacity:.55; align-self:flex-start; margin:-4px 6px 2px; }
  form { display:flex; gap:8px; padding:10px; border-top:1px solid rgba(128,128,128,.25); }
  input { flex:1; padding:10px 14px; border-radius:20px; border:1px solid rgba(128,128,128,.4);
          background:transparent; color:var(--text); font-size:15px; outline:none; }
  button { border:0; border-radius:20px; padding:0 14px; font-size:14px; cursor:pointer; }
  .send { background:var(--me); color:#fff; } .loc { background:var(--them); color:var(--text); }
</style></head>
<body>
<div class="phone"><div class="screen">
  <header><b>ResponSMS · Quezon City</b><span>Simulated SMS · not for real emergencies</span></header>
  <div id="chat"></div>
  <form id="f">
    <button type="button" class="loc" id="gps" title="Insert my coordinates">📍</button>
    <input id="msg" placeholder="Text message" autocomplete="off">
    <button class="send">Send</button>
  </form>
</div></div>
<script>
const chat = document.getElementById("chat"), msg = document.getElementById("msg");
function add(text, who, meta) {
  const d = document.createElement("div"); d.className = "bubble " + who;
  text.split(/(https?:\\/\\/\\S+)/).forEach(part => {          // URLs become links, everything else stays text
    if (/^https?:\\/\\//.test(part)) {
      const a = document.createElement("a"); a.href = part; a.target = "_blank";
      a.rel = "noopener"; a.textContent = part; d.appendChild(a);
    } else d.appendChild(document.createTextNode(part));
  });
  chat.appendChild(d);
  if (meta) { const m = document.createElement("div"); m.className = "meta"; m.textContent = meta; chat.appendChild(m); }
  chat.scrollTop = chat.scrollHeight;
}
async function send(text) {
  add(text, "me");
  try {
    const r = await fetch("/send", {method:"POST", headers:{"Content-Type":"application/json"},
                                    body: JSON.stringify({text})});
    const j = await r.json();
    add(j.reply, "them", j.follow_up ? null : "processed in " + j.ms + " ms");
    if (j.follow_up) add(j.follow_up, "them", "processed in " + j.ms + " ms");
  } catch (e) {
    add("Network error. Call 911 or go to the nearest ER.", "them");
  }
}
document.getElementById("f").onsubmit = e => {
  e.preventDefault(); const t = msg.value.trim(); if (!t) return; msg.value = ""; send(t);
};
document.getElementById("gps").onclick = () => {
  navigator.geolocation.getCurrentPosition(
    p => { msg.value = p.coords.latitude.toFixed(4) + "," + p.coords.longitude.toFixed(4); msg.focus(); },
    () => add("Could not read your location.", "them"));
};
add("Send 'help' for the menu.", "them");
</script></body></html>"""

if __name__ == "__main__":
    app.run(debug=True)