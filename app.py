import os, json, re, datetime
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

# ── Startup validation ────────────────────────────────────────
groq_key   = os.getenv("GROQ_API_KEY","").strip()
openai_key = os.getenv("OPENAI_API_KEY","").strip()
if not groq_key and not openai_key:
    print("\n" + "="*60)
    print("  ⚠️  WARNING: No API key found!")
    print("  Add GROQ_API_KEY to your .env file.")
    print("  Get a free key at: https://console.groq.com")
    print("="*60 + "\n")
else:
    provider = "Groq" if groq_key else "OpenAI"
    print(f"\n✅ {provider} API key loaded — Innie is ready!\n")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "internee-chatbot-secret-2024")
CORS(app, supports_credentials=True)

# ── Rate limiter ──────────────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# ── Database (SQLite) ─────────────────────────────────────────
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(DATA_DIR, "users.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

class User(db.Model, UserMixin):
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(80), nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin      = db.Column(db.Boolean, default=False, nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "email": self.email, "is_admin": self.is_admin}


class Conversation(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title      = db.Column(db.String(200), default="New conversation")
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    messages = db.relationship(
        "Message", backref="conversation",
        cascade="all, delete-orphan", order_by="Message.id"
    )

    def to_summary_dict(self):
        first_user_msg = next((m for m in self.messages if m.role == "user"), None)
        return {
            "id":         self.id,
            "title":      self.title,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "preview":    (first_user_msg.content[:80] if first_user_msg else ""),
            "message_count": len(self.messages),
        }


class Message(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False, index=True)
    role            = db.Column(db.String(20), nullable=False)  # "user" or "assistant"
    content         = db.Column(db.Text, nullable=False)
    ts              = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {"role": self.role, "content": self.content, "ts": self.ts.isoformat()}


with app.app_context():
    db.create_all()

# ── Login manager ─────────────────────────────────────────────
login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

@login_manager.unauthorized_handler
def unauthorized():
    return jsonify({"error": "Please log in to continue."}), 401

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Please log in to continue."}), 401
        if not current_user.is_admin:
            return jsonify({"error": "Admin access required."}), 403
        return f(*args, **kwargs)
    return wrapper

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ── Load knowledge base ───────────────────────────────────────
with open("data/knowledge_base.json", "r", encoding="utf-8") as f:
    knowledge_base = json.load(f)

# ── Conversation log (in-memory + file) ──────────────────────
LOG_FILE = "data/conversations.json"
def load_log():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except: pass
    return []

def save_log(entry):
    log = load_log()
    log.append(entry)
    # Keep last 1000 entries
    if len(log) > 1000:
        log = log[-1000:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

# ── Feedback store ────────────────────────────────────────────
FEEDBACK_FILE = "data/feedback.json"
def load_feedback():
    try:
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except: pass
    return []

# ── Fallback FAQ matching (no API needed) ─────────────────────
def find_best_faq(user_message):
    user_lower = user_message.lower()
    best_match = None
    best_score = 0
    for faq in knowledge_base["faqs"]:
        q_words = set(re.sub(r'[^\w\s]', '', faq["question"].lower()).split())
        u_words  = set(re.sub(r'[^\w\s]', '', user_lower).split())
        score = len(q_words & u_words)
        if score > best_score:
            best_score = score
            best_match = faq
    if best_score >= 2:
        return best_match["answer"]
    return None

# ── Build system prompt ───────────────────────────────────────
def build_system_prompt():
    faqs_text = "\n".join([
        f"Q: {item['question']}\nA: {item['answer']}"
        for item in knowledge_base["faqs"]
    ])
    policies = knowledge_base["policies"]
    contact  = knowledge_base["contact"]
    return f"""You are Innie, the official AI chatbot assistant for Internee.pk — Pakistan's leading virtual internship platform.

Your personality:
- Friendly, helpful, and encouraging
- Professional but conversational
- Keep answers concise but complete
- Format lists cleanly with numbers or bullets

Your knowledge base includes:

=== FAQs ===
{faqs_text}

=== Policies ===
Code of Conduct: {policies['code_of_conduct']}
Attendance: {policies['attendance']}
Plagiarism: {policies['plagiarism']}
Communication: {policies['communication']}

=== Contact Information ===
General Support: {contact['general']}
Billing: {contact['billing']}
Technical Issues: {contact['technical']}
Website: {contact['website']}

Instructions:
1. Answer questions ONLY about Internee.pk programs, policies, tasks, and intern life
2. If unrelated, politely redirect: "I'm specialized in Internee.pk queries! Is there anything about your internship I can help with?"
3. For complex issues not in the knowledge base, direct to support@internee.pk
4. The intern's name will be provided — greet them ONLY on the very first message, then answer directly in follow-ups
5. Keep responses under 200 words unless genuinely needed"""

SYSTEM_PROMPT = build_system_prompt()

# ── Suggested follow-ups per topic ────────────────────────────
FOLLOWUP_MAP = {
    "submit": ["What happens if I miss a deadline?", "What format should I submit in?"],
    "certificate": ["How is my performance scored?", "How do I get a LinkedIn recommendation?"],
    "deadline": ["Can I get an extension?", "What is the late submission policy?"],
    "apply": ["What domains are available?", "How long is the program?"],
    "paid": ["How long is the internship?", "What certificate will I receive?"],
    "performance": ["What score do I need to pass?", "How do I get my certificate?"],
    "linkedin": ["How is performance evaluated?", "When are certificates issued?"],
    "contact": ["What is the coordinator's response time?", "Can I message via WhatsApp?"],
    "community": ["How do I join the WhatsApp group?", "Is there a Discord server?"],
    "tools": ["What programming languages should I know?", "Do I need any paid software?"],
    "domains": ["How do I apply for a specific domain?", "Can I change my domain?"],
    "hours": ["Can I work on weekends?", "Is the schedule flexible?"],
    "first day": ["How do I access my tasks?", "Where do I submit my first task?"],
    "password": ["How do I contact technical support?", "What email do I use to sign in?"],
    "plagiarism": ["What are the consequences of plagiarism?", "How is plagiarism detected?"],
}

def get_followups(user_message):
    user_lower = user_message.lower()
    for keyword, suggestions in FOLLOWUP_MAP.items():
        if keyword in user_lower:
            return suggestions
    return []


# ══════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ── Auth routes ────────────────────────────────────────────────
@app.route("/api/signup", methods=["POST"])
@limiter.limit("10 per minute")
def signup():
    data     = request.get_json(silent=True) or {}
    name     = (data.get("name") or "").strip()[:80]
    email    = (data.get("email") or "").strip().lower()[:120]
    password = data.get("password") or ""

    if not name or not email or not password:
        return jsonify({"error": "Name, email, and password are all required."}), 400
    if not EMAIL_RE.match(email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with that email already exists. Try logging in."}), 409

    user = User(name=name, email=email)
    user.set_password(password)
    user.is_admin = (User.query.count() == 0)  # first account becomes admin
    db.session.add(user)
    db.session.commit()

    login_user(user)
    return jsonify({"status": "ok", "user": user.to_dict()})


@app.route("/api/login", methods=["POST"])
@limiter.limit("15 per minute")
def login():
    data     = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Please enter your email and password."}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Incorrect email or password."}), 401

    login_user(user)
    return jsonify({"status": "ok", "user": user.to_dict()})


@app.route("/api/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"status": "ok"})


@app.route("/api/me", methods=["GET"])
def me():
    if current_user.is_authenticated:
        return jsonify({"user": current_user.to_dict()})
    return jsonify({"error": "Not logged in"}), 401


@app.route("/api/chat", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def chat():
    data            = request.get_json()
    user_message    = data.get("message", "").strip()
    chat_history    = data.get("history", [])
    conversation_id = data.get("conversation_id")
    user_name       = current_user.name or "Intern"

    # ── Input validation ──────────────────────────────────────
    if not user_message:
        return jsonify({"error": "Empty message"}), 400
    # Sanitise + length limit
    user_message = re.sub(r'<[^>]+>', '', user_message)[:500]

    # ── Resolve / create the conversation this message belongs to ──
    conversation = None
    if conversation_id:
        conversation = Conversation.query.filter_by(id=conversation_id, user_id=current_user.id).first()
    if not conversation:
        conversation = Conversation(user_id=current_user.id, title=user_message[:60])
        db.session.add(conversation)
        db.session.commit()

    followups = get_followups(user_message)

    gkey = os.getenv("GROQ_API_KEY", "").strip()
    okey = os.getenv("OPENAI_API_KEY", "").strip()

    # ── Build messages ────────────────────────────────────────
    is_first = len(chat_history) == 0
    name_instruction = (
        f"\n\nThe intern's name is {user_name}. This is their FIRST message — you may greet them briefly."
        if is_first else
        f"\n\nThe intern's name is {user_name}. Do NOT greet again — just answer directly."
    )
    system = SYSTEM_PROMPT + name_instruction

    messages = [{"role": "system", "content": system}]
    for msg in chat_history[-10:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    # ── Compute the reply ─────────────────────────────────────
    reply    = None
    source   = "error"
    provider = None

    if gkey:
        try:
            import httpx
            from groq import Groq
            client   = Groq(api_key=gkey, http_client=httpx.Client())
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                max_tokens=400,
                temperature=0.6,
            )
            reply    = response.choices[0].message.content
            source   = "ai"
            provider = "groq"
        except ImportError:
            reply = "⚠️ Groq package not installed. Run: pip install groq httpx"
        except Exception as e:
            err = str(e)
            print(f"[GROQ ERROR] {err}")
            if "401" in err or "auth" in err.lower():
                reply = "⚠️ Invalid Groq API key. Check your .env file — key starts with gsk_"
            elif "429" in err or "rate" in err.lower():
                local = find_best_faq(user_message)
                reply    = local if local else "⚠️ Rate limit hit. Please wait a moment and try again."
                source   = "local" if local else "error"
                provider = "local" if local else None
            else:
                local = find_best_faq(user_message)
                if local:
                    reply    = f"{local}\n\n*(Answered from local knowledge base — AI temporarily unavailable)*"
                    source   = "local"
                    provider = "local"
                else:
                    reply = f"⚠️ Groq error: {err}"

    elif okey:
        try:
            from openai import OpenAI
            client   = OpenAI(api_key=okey)
            response = client.chat.completions.create(
                model="gpt-3.5-turbo", messages=messages, max_tokens=400, temperature=0.6,
            )
            reply    = response.choices[0].message.content
            source   = "ai"
            provider = "openai"
        except Exception as e:
            print(f"[OPENAI ERROR] {e}")
            reply = f"⚠️ OpenAI error: {e}"

    else:
        local = find_best_faq(user_message)
        if local:
            reply    = local
            source   = "local"
            provider = "local"
        else:
            reply  = "⚠️ No API key configured. Add GROQ_API_KEY to your .env file.\nGet a free key at: https://console.groq.com"
            source = "system"

    # ── Persist both sides of the exchange ────────────────────
    db.session.add(Message(conversation_id=conversation.id, role="user", content=user_message))
    db.session.add(Message(conversation_id=conversation.id, role="assistant", content=reply))
    conversation.updated_at = datetime.datetime.utcnow()
    if conversation.title in (None, "New conversation", ""):
        conversation.title = user_message[:60]
    db.session.commit()

    # ── Legacy JSON log (feeds the admin analytics dashboard) ──
    if provider in ("groq", "openai"):
        save_log({
            "ts": datetime.datetime.now().isoformat(),
            "user_id": current_user.id,
            "user": user_name,
            "message": user_message,
            "reply": reply[:200],
            "provider": provider
        })

    return jsonify({
        "reply": reply,
        "source": source,
        "followups": followups,
        "conversation_id": conversation.id,
    })


@app.route("/api/conversations", methods=["GET"])
@login_required
def list_conversations():
    convos = (Conversation.query
              .filter_by(user_id=current_user.id)
              .order_by(Conversation.updated_at.desc())
              .limit(100)
              .all())
    return jsonify({"conversations": [c.to_summary_dict() for c in convos]})


@app.route("/api/conversations/<int:conv_id>", methods=["GET"])
@login_required
def get_conversation(conv_id):
    convo = Conversation.query.filter_by(id=conv_id, user_id=current_user.id).first()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404
    return jsonify({
        "id": convo.id,
        "title": convo.title,
        "messages": [m.to_dict() for m in convo.messages],
    })


@app.route("/api/conversations/<int:conv_id>", methods=["DELETE"])
@login_required
def delete_conversation(conv_id):
    convo = Conversation.query.filter_by(id=conv_id, user_id=current_user.id).first()
    if not convo:
        return jsonify({"error": "Conversation not found."}), 404
    db.session.delete(convo)
    db.session.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/search", methods=["GET"])
@login_required
def search_messages():
    query = (request.args.get("q") or "").strip()
    if not query or len(query) < 2:
        return jsonify({"results": []})

    like = f"%{query}%"
    matches = (
        Message.query
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(Conversation.user_id == current_user.id)
        .filter(Message.content.ilike(like))
        .order_by(Message.ts.desc())
        .limit(30)
        .all()
    )

    results = [{
        "conversation_id": m.conversation_id,
        "conversation_title": m.conversation.title,
        "role": m.role,
        "snippet": m.content[:150],
        "ts": m.ts.isoformat(),
    } for m in matches]

    return jsonify({"results": results})


@app.route("/api/faqs", methods=["GET"])
def get_faqs():
    faqs = [{"question": item["question"]} for item in knowledge_base["faqs"][:10]]
    return jsonify({"faqs": faqs})


@app.route("/api/feedback", methods=["POST"])
@login_required
def feedback():
    """Save thumbs up/down on a message"""
    data = request.get_json()
    entry = {
        "ts":       datetime.datetime.now().isoformat(),
        "user":     data.get("userName", "Unknown"),
        "message":  data.get("message", "")[:300],
        "reply":    data.get("reply", "")[:300],
        "rating":   data.get("rating"),      # "up" or "down"
        "comment":  data.get("comment", ""),
    }
    fb = load_feedback()
    fb.append(entry)
    if len(fb) > 2000:
        fb = fb[-2000:]
    with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(fb, f, ensure_ascii=False, indent=2)
    return jsonify({"status": "saved"})


@app.route("/api/stats", methods=["GET"])
@admin_required
def stats():
    """Admin analytics — most asked topics, total conversations, feedback summary"""
    log      = load_log()
    feedback = load_feedback()

    # Top keywords
    all_messages = [e.get("message","") for e in log]
    word_counts  = {}
    stop_words   = {"the","is","a","an","i","my","do","how","what","can","to","in","for","of","and","or","it"}
    for msg in all_messages:
        for w in re.findall(r'\b\w{3,}\b', msg.lower()):
            if w not in stop_words:
                word_counts[w] = word_counts.get(w, 0) + 1
    top_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    up   = sum(1 for f in feedback if f.get("rating") == "up")
    down = sum(1 for f in feedback if f.get("rating") == "down")

    # Provider breakdown (groq / openai / local fallback)
    provider_counts = {}
    for e in log:
        p = e.get("provider", "unknown")
        provider_counts[p] = provider_counts.get(p, 0) + 1

    # Daily message volume — last 14 days
    daily_counts = {}
    for e in log:
        ts = e.get("ts", "")
        day = ts[:10] if ts else None
        if day:
            daily_counts[day] = daily_counts.get(day, 0) + 1
    today = datetime.date.today()
    last_14_days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    daily_series = [{"date": d, "count": daily_counts.get(d, 0)} for d in last_14_days]

    return jsonify({
        "total_conversations": len(log),
        "total_feedback":      len(feedback),
        "total_users":         User.query.count(),
        "thumbs_up":           up,
        "thumbs_down":         down,
        "satisfaction_pct":    round(up / max(up+down, 1) * 100),
        "top_keywords":        top_words,
        "provider_breakdown":  provider_counts,
        "daily_activity":      daily_series,
        "recent_messages":     log[-8:][::-1] if log else [],
    })


@app.route("/dashboard")
@login_required
def dashboard():
    if not current_user.is_admin:
        return "<h2 style='font-family:sans-serif;color:#DC2626;text-align:center;margin-top:80px'>403 — Admin access required.</h2>", 403
    return render_template("dashboard.html")


@app.route("/api/health", methods=["GET"])
def health():
    gk = bool(os.getenv("GROQ_API_KEY","").strip())
    ok = bool(os.getenv("OPENAI_API_KEY","").strip())
    return jsonify({
        "status":         "ok",
        "bot":            "Innie - Internee.pk Chatbot",
        "provider":       "groq" if gk else ("openai" if ok else "none"),
        "groq_key_set":   gk,
        "openai_key_set": ok,
        "faqs_loaded":    len(knowledge_base.get("faqs", [])),
    })


# ── 404 handler ───────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"reply": "⚠️ Too many messages. Please wait a minute and try again.", "source": "error"}), 429


if __name__ == "__main__":
    app.run(
        debug=True, host="0.0.0.0", port=5000,
        exclude_patterns=["data/*", "*.db", "*.json"],
    )
