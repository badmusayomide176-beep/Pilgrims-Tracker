import sqlite3
import hashlib
import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room
from functools import wraps
from geopy.distance import geodesic

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'pilgrims-secret-key-2024')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Use eventlet for production
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Database path - use /tmp for Render free tier
DB_PATH = '/tmp/pilgrims.db' if os.environ.get('RENDER') else 'pilgrims.db'


def get_db():
    return sqlite3.connect(DB_PATH)


def init_database():
    conn = get_db()
    cursor = conn.cursor()

    # Create users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            phone TEXT,
            nationality TEXT,
            passport TEXT,
            user_type TEXT DEFAULT 'user'
        )
    ''')

    # Create locations table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            lat REAL,
            lng REAL,
            inside INTEGER DEFAULT 1,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Create zones table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            lat REAL,
            lng REAL,
            radius REAL,
            description TEXT
        )
    ''')

    # Create alerts table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            lat REAL,
            lng REAL,
            resolved INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()

    # Create admin user if not exists
    cursor.execute("SELECT * FROM users WHERE email = 'admin@pilgrims.com'")
    if not cursor.fetchone():
        hashed = hashlib.sha256('admin123'.encode()).hexdigest()
        cursor.execute('''
            INSERT INTO users (name, email, password, phone, nationality, passport, user_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', ('Admin', 'admin@pilgrims.com', hashed, '1234567890', 'System', 'ADMIN001', 'admin'))
        conn.commit()
        print("✅ Admin user created")

    conn.close()
    print("✅ Database initialized")


# Initialize database
if not os.path.exists(DB_PATH):
    init_database()
else:
    # Check if tables exist
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users LIMIT 1")
        conn.close()
    except:
        init_database()


# Database helper functions
def get_user_by_email(email):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (email.lower(),))
    user = cursor.fetchone()
    conn.close()
    return user


def add_user(name, email, password, phone, nationality, passport):
    try:
        conn = get_db()
        cursor = conn.cursor()
        hashed = hashlib.sha256(password.encode()).hexdigest()
        cursor.execute('''
            INSERT INTO users (name, email, password, phone, nationality, passport)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, email.lower(), hashed, phone, nationality, passport))
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        return user_id
    except:
        return None


def authenticate(email, password):
    conn = get_db()
    cursor = conn.cursor()
    hashed = hashlib.sha256(password.encode()).hexdigest()
    cursor.execute('''
        SELECT id, name, email, user_type FROM users 
        WHERE email = ? AND password = ?
    ''', (email.lower(), hashed))
    user = cursor.fetchone()
    conn.close()
    return user


def update_location_db(user_id, lat, lng):
    conn = get_db()
    cursor = conn.cursor()

    inside = check_zone(lat, lng)

    cursor.execute('''
        INSERT INTO locations (user_id, lat, lng, inside)
        VALUES (?, ?, ?, ?)
    ''', (user_id, lat, lng, inside))
    conn.commit()
    conn.close()

    if not inside:
        user_name = get_user_name(user_id)
        alert_msg = f"⚠️ {user_name} left the safe zone at {lat:.4f}, {lng:.4f}"
        create_alert(user_id, alert_msg, lat, lng)
        return inside, alert_msg

    return inside, None


def get_user_name(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM users WHERE id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else "Unknown"


def check_zone(lat, lng):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM zones")
    zones = cursor.fetchall()
    conn.close()

    if not zones:
        return True

    for zone in zones:
        try:
            dist = geodesic((lat, lng), (zone[2], zone[3])).kilometers
            if dist <= zone[4]:
                return True
        except:
            continue
    return False


def get_locations():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.id, u.name, l.lat, l.lng, l.inside, l.timestamp
        FROM users u
        LEFT JOIN locations l ON u.id = l.user_id
        WHERE l.id IN (SELECT MAX(id) FROM locations GROUP BY user_id) OR l.id IS NULL
    ''')
    locs = cursor.fetchall()
    conn.close()
    return locs


def get_zones():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM zones")
    zones = cursor.fetchall()
    conn.close()
    return zones


def add_zone_db(name, lat, lng, radius, description):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO zones (name, lat, lng, radius, description)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, lat, lng, radius, description))
        conn.commit()
        conn.close()
        return True
    except:
        return False


def delete_zone_db(zone_id):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM zones WHERE id = ?", (zone_id,))
        conn.commit()
        conn.close()
        return True
    except:
        return False


def create_alert(user_id, message, lat, lng):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO alerts (user_id, message, lat, lng)
        VALUES (?, ?, ?, ?)
    ''', (user_id, message, lat, lng))
    conn.commit()
    conn.close()


def get_alerts_db(user_id=None):
    conn = get_db()
    cursor = conn.cursor()
    if user_id:
        cursor.execute('''
            SELECT a.id, a.user_id, a.message, a.lat, a.lng, a.created_at, u.name
            FROM alerts a
            JOIN users u ON a.user_id = u.id
            WHERE a.user_id = ? AND a.resolved = 0
            ORDER BY a.created_at DESC
        ''', (user_id,))
    else:
        cursor.execute('''
            SELECT a.id, a.user_id, a.message, a.lat, a.lng, a.created_at, u.name
            FROM alerts a
            JOIN users u ON a.user_id = u.id
            WHERE a.resolved = 0
            ORDER BY a.created_at DESC
        ''')
    alerts = cursor.fetchall()
    conn.close()
    return alerts


def resolve_alert_db(alert_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE alerts SET resolved = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()


def get_stats():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM users WHERE user_type = 'user'")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM locations WHERE timestamp > datetime('now', '-5 minutes')")
    active = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM locations WHERE inside = 0")
    outside = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM alerts WHERE resolved = 0")
    alerts = cursor.fetchone()[0]

    conn.close()
    return {'total': total, 'active': active, 'outside': outside, 'alerts': alerts}


# Flask routes
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('user_type') != 'admin':
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)

    return decorated


@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            data = request.get_json()
            email = data.get('email', '').strip()
            password = data.get('password', '')

            user = authenticate(email, password)
            if user:
                session['user_id'] = user[0]
                session['user_name'] = user[1]
                session['user_email'] = user[2]
                session['user_type'] = user[3]
                return jsonify({'success': True, 'redirect': url_for('dashboard')})
            return jsonify({'success': False, 'message': 'Invalid credentials'})
        except Exception as e:
            print(f"Login error: {e}")
            return jsonify({'success': False, 'message': 'Server error'})

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            data = request.get_json()
            name = data.get('full_name', '').strip()
            email = data.get('email', '').strip().lower()
            password = data.get('password', '')
            phone = data.get('phone', '')
            nationality = data.get('nationality', '')
            passport = data.get('passport_number', '')

            if not name or not email or not password:
                return jsonify({'success': False, 'message': 'Name, email and password required'})

            if len(password) < 4:
                return jsonify({'success': False, 'message': 'Password must be at least 4 characters'})

            if get_user_by_email(email):
                return jsonify({'success': False, 'message': 'Email already exists'})

            user_id = add_user(name, email, password, phone, nationality, passport)
            if user_id:
                return jsonify({'success': True, 'message': 'Registration successful! Please login.'})
            return jsonify({'success': False, 'message': 'Registration failed'})
        except Exception as e:
            print(f"Registration error: {e}")
            return jsonify({'success': False, 'message': str(e)})

    return render_template('register.html')


@app.route('/dashboard')
@login_required
def dashboard():
    if session['user_type'] == 'admin':
        return render_template('admin.html', user=session)
    return render_template('user.html', user=session)


@app.route('/api/locations')
@login_required
def get_locations_api():
    locs = get_locations()
    result = []
    for loc in locs:
        result.append({
            'id': loc[0],
            'name': loc[1],
            'lat': loc[2],
            'lng': loc[3],
            'inside': loc[4],
            'time': loc[5]
        })
    return jsonify(result)


@app.route('/api/update_location', methods=['POST'])
@login_required
def update_location_api():
    try:
        data = request.get_json()
        lat = data.get('lat')
        lng = data.get('lng')

        if lat and lng:
            inside, alert_msg = update_location_db(session['user_id'], lat, lng)

            socketio.emit('location_update', {
                'user_id': session['user_id'],
                'user_name': session['user_name'],
                'lat': lat,
                'lng': lng,
                'inside': inside
            }, room='admin')

            if not inside and alert_msg:
                socketio.emit('zone_alert', {
                    'user_name': session['user_name'],
                    'message': alert_msg,
                    'lat': lat,
                    'lng': lng
                }, room='admin')

                socketio.emit('personal_alert', {
                    'message': f"⚠️ ALERT: You left the safe zone! Location: {lat:.4f}, {lng:.4f}"
                }, room=f'user_{session["user_id"]}')

            return jsonify({'success': True, 'inside': inside})

        return jsonify({'success': False})
    except Exception as e:
        print(f"Update location error: {e}")
        return jsonify({'success': False})


@app.route('/api/zones', methods=['GET', 'POST', 'DELETE'])
@login_required
def zones_api():
    if request.method == 'GET':
        zones = get_zones()
        result = [
            {'id': z[0], 'name': z[1], 'lat': z[2], 'lng': z[3], 'radius': z[4], 'desc': z[5] if len(z) > 5 else ''} for
            z in zones]
        return jsonify(result)

    elif request.method == 'POST':
        if session['user_type'] != 'admin':
            return jsonify({'error': 'Unauthorized'}), 403
        data = request.get_json()
        success = add_zone_db(data['name'], data['lat'], data['lng'], data['radius'], data.get('desc', ''))
        if success:
            socketio.emit('zone_update')
            return jsonify({'success': True})
        return jsonify({'success': False, 'message': 'Failed to add zone'})

    elif request.method == 'DELETE':
        if session['user_type'] != 'admin':
            return jsonify({'error': 'Unauthorized'}), 403
        data = request.get_json()
        delete_zone_db(data['id'])
        socketio.emit('zone_update')
        return jsonify({'success': True})


@app.route('/api/alerts')
@login_required
def get_alerts_api():
    if session['user_type'] == 'admin':
        alerts = get_alerts_db()
    else:
        alerts = get_alerts_db(session['user_id'])

    result = [{'id': a[0], 'user_id': a[1], 'message': a[2], 'lat': a[3], 'lng': a[4], 'time': a[5], 'user_name': a[6]}
              for a in alerts]
    return jsonify(result)


@app.route('/api/resolve_alert/<int:alert_id>', methods=['POST'])
@admin_required
def resolve_alert_api(alert_id):
    resolve_alert_db(alert_id)
    return jsonify({'success': True})


@app.route('/api/stats')
@admin_required
def get_stats_api():
    return jsonify(get_stats())


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@socketio.on('connect')
def handle_connect():
    if 'user_id' in session:
        if session['user_type'] == 'admin':
            join_room('admin')
            print(f"✅ Admin {session['user_name']} connected")
        else:
            join_room(f'user_{session["user_id"]}')
            print(f"✅ User {session['user_name']} connected")


if __name__ == '__main__':
    print("\n" + "=" * 70)
    print("🚀 PILGRIMS TRACKING SYSTEM")
    print("=" * 70)
    print("👤 Admin: admin@pilgrims.com")
    print("🔑 Password: admin123")
    print("=" * 70 + "\n")

    # For local development
    socketio.run(app, debug=False, host='0.0.0.0', port=5008, allow_unsafe_werkzeug=True)