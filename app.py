import os
import re
import json
import math
import time
import threading
import requests
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')
DATA_DIR = 'data'
GEOCACHE_FILE = os.path.join(DATA_DIR, 'geocache.json')
SAVED_DATA_FILE = os.path.join(DATA_DIR, 'current_data.txt')
SAVED_META_FILE = os.path.join(DATA_DIR, 'meta.json')
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs('static', exist_ok=True)

DATE_RE = re.compile(r'^\d{2}/\d{2}/\d{4}$')
def is_date(val):
    return bool(val and DATE_RE.match(val.strip()))

state = {
    'voters': [], 'total': 0, 'returned': 0,
    'filename': '', 'loaded_at': '',
    'geocache': {}, 'loading': False, 'load_progress': '',
}

# ── GEOCACHE ──────────────────────────────────────────────────────
def load_geocache():
    if os.path.exists(GEOCACHE_FILE):
        try:
            with open(GEOCACHE_FILE, 'r') as f:
                state['geocache'] = json.load(f)
            print(f"Loaded {len(state['geocache'])} cached geocodes")
        except Exception as e:
            print(f"Geocache load error: {e}")

def save_geocache():
    try:
        with open(GEOCACHE_FILE, 'w') as f:
            json.dump(state['geocache'], f)
    except Exception as e:
        print(f"Geocache save error: {e}")

def geocode_nominatim(building_addr, city, state_abbr, zip5):
    key = f"{building_addr},{city},co,{zip5}".lower().replace('  ', ' ')
    if key in state['geocache']:
        return state['geocache'][key], key
    q = f"{building_addr}, {city}, {state_abbr} {zip5}"
    url = 'https://nominatim.openstreetmap.org/search'
    params = {'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'us',
              'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1}
    headers = {'User-Agent': 'BallotReturnFinder/1.0 (Denver GOTV)'}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=5)
        data = r.json()
        if data:
            lat, lng = float(data[0]['lat']), float(data[0]['lon'])
            if 39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600:
                result = [lat, lng]
                state['geocache'][key] = result
                return result, key
    except Exception as e:
        print(f"Geocode error: {e}")
    state['geocache'][key] = None
    return None, key

# ── CSV PARSING ───────────────────────────────────────────────────
def split_line(line, delim):
    result, cur, in_q = [], '', False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == delim and not in_q:
            result.append(cur); cur = ''
        else:
            cur += ch
    result.append(cur)
    return result

def parse_text(text, filename):
    lines = text.split('\n')
    if not lines:
        return
    delim = '|' if '|' in lines[0] else ','
    header_cols = split_line(lines[0].strip(), delim)
    col = {name.strip(): i for i, name in enumerate(header_cols)}

    required = ['VOTER_ID','FIRST_NAME','LAST_NAME','PARTY',
                'RES_ADDRESS','RES_CITY','RES_STATE','RES_ZIP']
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    print(f"MAIL_BALLOT_RECEIVE_DATE at col {col.get('MAIL_BALLOT_RECEIVE_DATE')}, "
          f"IN_PERSON_VOTE_DATE at col {col.get('IN_PERSON_VOTE_DATE')}")

    total, returned, voters = 0, 0, []

    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        cols = split_line(line, delim)
        total += 1

        def get(name, default=''):
            idx = col.get(name)
            if idx is None or idx >= len(cols):
                return default
            return cols[idx].strip()

        if is_date(get('MAIL_BALLOT_RECEIVE_DATE')) or is_date(get('IN_PERSON_VOTE_DATE')):
            returned += 1
            continue

        res_addr = get('RES_ADDRESS')
        if not res_addr:
            continue

        unit_match = re.search(r'#\s*(\S+)\s*$', res_addr)
        unit = unit_match.group(1) if unit_match else None
        building_addr = re.sub(r'\s*#\s*\S+\s*$', '', res_addr).strip() if unit else res_addr

        city = get('RES_CITY') or 'DENVER'
        state_abbr = get('RES_STATE') or 'CO'
        zip5 = get('RES_ZIP').split('-')[0]
        party = get('PARTY') or 'UAF'
        geocode_key = f"{building_addr},{city},co,{zip5}".lower().replace('  ', ' ')

        voters.append({
            'name': f"{get('FIRST_NAME')} {get('LAST_NAME')}".strip(),
            'unit': unit,
            'buildingAddress': building_addr,
            'city': city, 'state': state_abbr, 'zip': zip5,
            'geocodeKey': geocode_key,
            'party': party, 'apt': unit is not None,
        })

    state['voters'] = voters
    state['total'] = total
    state['returned'] = returned
    state['filename'] = filename
    state['loaded_at'] = time.strftime('%-m/%-d/%Y at %-I:%M %p MDT')

    # Save metadata
    try:
        with open(SAVED_META_FILE, 'w') as f:
            json.dump({'filename': filename, 'loaded_at': state['loaded_at'],
                       'total': total, 'returned': returned}, f)
    except Exception as e:
        print(f"Meta save error: {e}")

    print(f"Parsed {total} total, {len(voters)} not returned, {returned} returned")

def geocode_all_background():
    state['loading'] = True
    buildings = {}
    for v in state['voters']:
        k = v['geocodeKey']
        if k not in buildings:
            buildings[k] = v
    need = {k: v for k, v in buildings.items() if k not in state['geocache']}
    total = len(need)
    done = 0
    print(f"Geocoding {total} new addresses...")
    for key, v in need.items():
        geocode_nominatim(v['buildingAddress'], v['city'], v['state'], v['zip'])
        done += 1
        state['load_progress'] = f"Geocoding {done:,} / {total:,} addresses…"
        if done % 200 == 0:
            save_geocache()
            print(f"  {done}/{total} geocoded")
        time.sleep(1.1)
    save_geocache()
    state['loading'] = False
    state['load_progress'] = ''
    print("Geocoding complete.")

# ── HAVERSINE ─────────────────────────────────────────────────────
def haversine(lat1, lng1, lat2, lng2):
    R = 3958.8
    dLat = math.radians(lat2 - lat1)
    dLng = math.radians(lng2 - lng1)
    a = (math.sin(dLat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLng/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# ── ROUTES ────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/status')
def api_status():
    rate = round(state['returned'] / state['total'] * 100) if state['total'] > 0 else 0
    return jsonify({
        'total': state['total'], 'pending': len(state['voters']),
        'returned': state['returned'], 'returnRate': rate,
        'filename': state['filename'], 'loadedAt': state['loaded_at'],
        'loading': state['loading'], 'loadProgress': state['load_progress'],
        'hasData': len(state['voters']) > 0,
    })

@app.route('/api/search')
def api_search():
    try:
        lat = float(request.args.get('lat'))
        lng = float(request.args.get('lng'))
    except (TypeError, ValueError):
        return jsonify({'error': 'lat and lng required'}), 400

    party = request.args.get('party', 'all')
    access = request.args.get('access', 'all')

    buildings = {}
    for v in state['voters']:
        if party != 'all' and v['party'] != party:
            continue
        k = v['geocodeKey']
        if k not in buildings:
            coords = state['geocache'].get(k)
            if not coords:
                continue
            buildings[k] = {
                'buildingAddress': v['buildingAddress'],
                'city': v['city'], 'state': v['state'], 'zip': v['zip'],
                'apt': v['apt'], 'lat': coords[0], 'lng': coords[1],
                'voters': [],
            }
        buildings[k]['voters'].append({
            'name': v['name'], 'unit': v['unit'], 'party': v['party']
        })

    result = list(buildings.values())
    if access == 'accessible':
        result = [b for b in result if not b['apt']]
    elif access == 'inaccessible':
        result = [b for b in result if b['apt']]

    for b in result:
        b['dist'] = haversine(lat, lng, b['lat'], b['lng'])
    result.sort(key=lambda b: b['dist'])
    result = result[:100]

    for b in result:
        if b['apt']:
            b['voters'].sort(key=lambda v: (
                int(v['unit']) if v['unit'] and v['unit'].isdigit() else 9999,
                v['unit'] or ''
            ))

    return jsonify({'results': result})

@app.route('/api/geocode')
def api_geocode():
    addr = request.args.get('address', '')
    if not addr:
        return jsonify({'error': 'address required'}), 400
    url = 'https://nominatim.openstreetmap.org/search'
    params = {'q': addr + ', Denver, CO', 'format': 'json', 'limit': 1,
              'countrycodes': 'us',
              'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1}
    headers = {'User-Agent': 'BallotReturnFinder/1.0'}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=5)
        data = r.json()
        if data:
            lat, lng = float(data[0]['lat']), float(data[0]['lon'])
            if 39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600:
                return jsonify({'lat': lat, 'lng': lng})
    except Exception:
        pass
    return jsonify({'error': 'Address not found in Denver'}), 404

@app.route('/api/autocomplete')
def api_autocomplete():
    q = request.args.get('q', '')
    if len(q) < 3:
        return jsonify([])
    url = 'https://nominatim.openstreetmap.org/search'
    params = {'q': q + ', Denver, CO', 'format': 'json', 'limit': 5,
              'countrycodes': 'us', 'addressdetails': 1,
              'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1}
    headers = {'User-Agent': 'BallotReturnFinder/1.0'}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=5)
        data = r.json()
        results = []
        for item in data:
            a = item.get('address', {})
            lat, lng = float(item['lat']), float(item['lon'])
            if not (39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600):
                continue
            main = ' '.join(filter(None, [a.get('house_number'), a.get('road')]))
            if not main:
                main = item['display_name'].split(',')[0]
            results.append({'main': main, 'lat': lat, 'lng': lng})
        return jsonify(results)
    except Exception:
        return jsonify([])

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'GET':
        return send_from_directory('static', 'admin.html')
    password = request.form.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Invalid password'}), 401
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    text = f.stream.read().decode('utf-8', errors='replace')
    filename = secure_filename(f.filename)

    # Save raw file to disk FIRST so restarts can recover
    try:
        with open(SAVED_DATA_FILE, 'w', encoding='utf-8') as fout:
            fout.write(text)
        print(f"Saved {len(text):,} bytes to disk")
    except Exception as e:
        print(f"File save error: {e}")

    def do_parse_and_geocode():
        try:
            parse_text(text, filename)
            geocode_all_background()
        except Exception as e:
            print(f"Background parse/geocode error: {e}")
            import traceback; traceback.print_exc()

    t = threading.Thread(target=do_parse_and_geocode, daemon=True)
    t.start()

    return jsonify({
        'success': True,
        'message': 'File received and saved. Parsing in background.',
        'filename': filename,
    })

# ── STARTUP ───────────────────────────────────────────────────────
load_geocache()

# Auto-reload saved data on startup (survives restarts)
if os.path.exists(SAVED_DATA_FILE) and os.path.exists(SAVED_META_FILE):
    try:
        with open(SAVED_META_FILE, 'r') as f:
            meta = json.load(f)
        print(f"Found saved data: {meta.get('filename')} — reloading...")
        with open(SAVED_DATA_FILE, 'r', encoding='utf-8') as f:
            saved_text = f.read()
        parse_text(saved_text, meta.get('filename', 'saved_data.txt'))
        print("Saved data reloaded successfully.")
    except Exception as e:
        print(f"Auto-reload error: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
