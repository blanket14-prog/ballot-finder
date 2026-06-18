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

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')
GEOCACHE_FILE = 'data/geocache.json'
os.makedirs('data', exist_ok=True)
os.makedirs('static', exist_ok=True)

state = {
    'voters': [], 'total': 0, 'returned': 0,
    'filename': '', 'loaded_at': '',
    'geocache': {}, 'loading': False, 'load_progress': '',
}

DATE_RE = re.compile(r'^\d{2}/\d{2}/\d{4}$')

def is_date(val):
    return bool(val and DATE_RE.match(val.strip()))

def load_geocache():
    if os.path.exists(GEOCACHE_FILE):
        with open(GEOCACHE_FILE, 'r') as f:
            state['geocache'] = json.load(f)
        print(f"Loaded {len(state['geocache'])} cached geocodes")

def save_geocache():
    with open(GEOCACHE_FILE, 'w') as f:
        json.dump(state['geocache'], f)

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

def split_line(line, delim):
    result, cur, in_q = [], '', False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == delim and not in_q:
            result.append(cur)
            cur = ''
        else:
            cur += ch
    result.append(cur)
    return result

def parse_file(text, filename):
    lines = text.split('\n')
    if not lines:
        return
    delim = '|' if '|' in lines[0] else ','
    header_cols = split_line(lines[0].strip(), delim)
    col = {name.strip(): i for i, name in enumerate(header_cols)}

    required = ['VOTER_ID', 'FIRST_NAME', 'LAST_NAME', 'PARTY',
                'RES_ADDRESS', 'RES_CITY', 'RES_STATE', 'RES_ZIP']
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    idx_received = col.get('MAIL_BALLOT_RECEIVE_DATE')
    idx_inperson = col.get('IN_PERSON_VOTE_DATE')
    print(f"MAIL_BALLOT_RECEIVE_DATE at col {idx_received}, IN_PERSON_VOTE_DATE at col {idx_inperson}")

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

        # Check ballot returned — validate values look like dates
        received = get('MAIL_BALLOT_RECEIVE_DATE')
        in_person = get('IN_PERSON_VOTE_DATE')
        if is_date(received) or is_date(in_person):
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
        first = get('FIRST_NAME')
        last = get('LAST_NAME')
        party = get('PARTY') or 'UAF'
        geocode_key = f"{building_addr},{city},co,{zip5}".lower().replace('  ', ' ')

        voters.append({
            'name': f"{first} {last}".strip(),
            'unit': unit,
            'buildingAddress': building_addr,
            'city': city, 'state': state_abbr, 'zip': zip5,
            'geocodeKey': geocode_key,
            'party': party,
            'apt': unit is not None,
        })

    state['voters'] = voters
    state['total'] = total
    state['returned'] = returned
    state['filename'] = filename
    state['loaded_at'] = time.strftime('%b %-d, %Y at %-I:%M %p MDT')
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
        if done % 100 == 0:
            save_geocache()
        time.sleep(1.1)
    save_geocache()
    state['loading'] = False
    state['load_progress'] = ''
    print("Geocoding complete.")

def haversine(lat1, lng1, lat2, lng2):
    R = 3958.8
    dLat = math.radians(lat2 - lat1)
    dLng = math.radians(lng2 - lng1)
    a = (math.sin(dLat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLng/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

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
    try:
        parse_file(text, filename)
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    t = threading.Thread(target=geocode_all_background, daemon=True)
    t.start()
    return jsonify({
        'success': True, 'total': state['total'],
        'pending': len(state['voters']), 'returned': state['returned'],
        'filename': filename,
    })

load_geocache()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
