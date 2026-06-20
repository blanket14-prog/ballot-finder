import os, re, json, math, time, threading, requests
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')
CAMPAIGN_NAME = os.environ.get('CAMPAIGN_NAME', 'Ballot Finder')
CAMPAIGN_LOGO = os.environ.get('CAMPAIGN_LOGO', '')  # 'melat', 'phil', or ''
CAMPAIGN_COLOR = os.environ.get('CAMPAIGN_COLOR', '#6c63ff')
DATA_DIR = os.environ.get('DATA_DIR', '/opt/render/project/src/data')
GEOCACHE_FILE = os.path.join(DATA_DIR, 'geocache.json')
SAVED_DATA_FILE = os.path.join(DATA_DIR, 'current_data.txt')
SAVED_META_FILE = os.path.join(DATA_DIR, 'meta.json')
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs('static', exist_ok=True)

DATE_RE = re.compile(r'^\d{2}/\d{2}/\d{4}$')
def is_date(v): return bool(v and DATE_RE.match(v.strip()))

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
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
            params={'q': f"{building_addr}, {city}, {state_abbr} {zip5}",
                    'format': 'json', 'limit': 1, 'countrycodes': 'us',
                    'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1},
            headers={'User-Agent': 'BallotReturnFinder/1.0'}, timeout=5)
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
        if ch == '"': in_q = not in_q
        elif ch == delim and not in_q: result.append(cur); cur = ''
        else: cur += ch
    result.append(cur)
    return result

def parse_from_disk(filename):
    print(f"Parsing from disk: {SAVED_DATA_FILE}")
    voters, total, returned = [], 0, 0
    with open(SAVED_DATA_FILE, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    if not lines:
        return
    delim = '|' if '|' in lines[0] else ','
    header_cols = split_line(lines[0].strip(), delim)
    col = {name.strip(): i for i, name in enumerate(header_cols)}
    required = ['VOTER_ID','FIRST_NAME','LAST_NAME','PARTY','RES_ADDRESS','RES_CITY','RES_STATE','RES_ZIP']
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    print(f"Columns OK. MAIL_BALLOT_RECEIVE_DATE at {col.get('MAIL_BALLOT_RECEIVE_DATE')}")
    for line in lines[1:]:
        line = line.strip()
        if not line: continue
        cols = split_line(line, delim)
        total += 1
        def get(name, default=''):
            idx = col.get(name)
            if idx is None or idx >= len(cols): return default
            return cols[idx].strip()
        if is_date(get('MAIL_BALLOT_RECEIVE_DATE')) or is_date(get('IN_PERSON_VOTE_DATE')):
            returned += 1; continue
        res_addr = get('RES_ADDRESS')
        if not res_addr: continue
        unit_match = re.search(r'#\s*(\S+)\s*$', res_addr)
        unit = unit_match.group(1) if unit_match else None
        building_addr = re.sub(r'\s*#\s*\S+\s*$', '', res_addr).strip() if unit else res_addr
        city = get('RES_CITY') or 'DENVER'
        state_abbr = get('RES_STATE') or 'CO'
        zip5 = get('RES_ZIP').split('-')[0]
        party = get('PARTY') or 'UAF'
        geocode_key = f"{building_addr},{city},co,{zip5}".lower().replace('  ', ' ')
        yob_raw = get('YOB','').strip()
        yob = int(yob_raw) if yob_raw.isdigit() else None
        voters.append({
            'name': f"{get('FIRST_NAME')} {get('LAST_NAME')}".strip(),
            'unit': unit, 'buildingAddress': building_addr,
            'city': city, 'state': state_abbr, 'zip': zip5,
            'geocodeKey': geocode_key, 'party': party, 'apt': unit is not None,
            'yob': yob,
        })
    state['voters'] = voters
    state['total'] = total
    state['returned'] = returned
    state['filename'] = filename
    # Use Mountain time for display
    import datetime
    # UTC-6 for MDT (Mountain Daylight Time)
    mdt = datetime.timezone(datetime.timedelta(hours=-6))
    now_mdt = datetime.datetime.now(mdt)
    state['loaded_at'] = now_mdt.strftime('%-m/%-d/%Y at %-I:%M %p MDT')
    try:
        with open(SAVED_META_FILE, 'w') as f:
            json.dump({'filename': filename, 'loaded_at': state['loaded_at'],
                       'total': total, 'returned': returned}, f)
    except Exception as e:
        print(f"Meta save error: {e}")
    print(f"Parsed {total} total, {len(voters)} not returned, {returned} returned")

def geocode_census(building_addr, city, state_abbr, zip5):
    """Use Census geocoder — no rate limits."""
    key = f"{building_addr},{city},co,{zip5}".lower().replace('  ', ' ')
    if key in state['geocache'] and state['geocache'][key] is not None:
        return state['geocache'][key], key
    try:
        full = f"{building_addr}, {city}, {state_abbr} {zip5}"
        r = requests.get(
            'https://geocoding.geo.census.gov/geocoder/locations/onelineaddress',
            params={'address': full, 'benchmark': 'Public_AR_Current', 'format': 'json'},
            timeout=10)
        matches = r.json().get('result', {}).get('addressMatches', [])
        if matches:
            c = matches[0]['coordinates']
            lat, lng = float(c['y']), float(c['x'])
            if 39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600:
                result = [lat, lng]
                state['geocache'][key] = result
                return result, key
    except Exception as e:
        pass
    state['geocache'][key] = None
    return None, key

def geocode_all_background():
    state['loading'] = True
    buildings = {}
    for v in state['voters']:
        k = v['geocodeKey']
        if k not in buildings: buildings[k] = v
    # Only geocode addresses not yet successfully cached
    need = {k: v for k, v in buildings.items() 
            if state['geocache'].get(k) is None}
    total = len(need)
    done = 0
    print(f"Geocoding {total} new addresses with Census geocoder...")
    for key, v in need.items():
        geocode_census(v['buildingAddress'], v['city'], v['state'], v['zip'])
        done += 1
        state['load_progress'] = f"Geocoding {done:,} / {total:,} addresses…"
        if done % 500 == 0:
            save_geocache()
            print(f"  {done}/{total} geocoded")
        time.sleep(0.05)  # Census has no strict rate limit — 20 req/sec is safe
    save_geocache()
    state['loading'] = False
    state['load_progress'] = ''
    print("Geocoding complete.")

# ── HAVERSINE ─────────────────────────────────────────────────────
def haversine(la1, ln1, la2, ln2):
    R = 3958.8
    dLa = math.radians(la2 - la1); dLn = math.radians(ln2 - ln1)
    a = math.sin(dLa/2)**2 + math.cos(math.radians(la1)) * math.cos(math.radians(la2)) * math.sin(dLn/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ── ROUTES ────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/config')
def api_config():
    logo_url = f'/static/logo-{CAMPAIGN_LOGO}.png' if CAMPAIGN_LOGO else ''
    return jsonify({
        'campaignName': CAMPAIGN_NAME,
        'logoUrl': logo_url,
        'accentColor': CAMPAIGN_COLOR,
    })

@app.route('/static/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/static/sw.js')
def service_worker():
    from flask import Response
    with open('static/sw.js') as f:
        js = f.read()
    return Response(js, mimetype='application/javascript',
                    headers={'Service-Worker-Allowed': '/'})

@app.route('/api/status')
def api_status():
    # Count only DEM + UAF for the top stats (what most volunteers care about)
    dem_uaf = [v for v in state['voters'] if v.get('party') in ('DEM', 'UAF')]
    total_dem_uaf_sent = state['total'] - sum(1 for v in state['voters'] if v.get('party') == 'REP')
    pending_dem_uaf = len(dem_uaf)
    returned_dem_uaf = state['returned']  # approximate — returned voters not in memory
    rate = round(state['returned'] / state['total'] * 100) if state['total'] > 0 else 0
    return jsonify({
        'total': state['total'], 'pending': pending_dem_uaf,
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
    party_param = request.args.get('party', 'DEM,UAF')
    access_param = request.args.get('access', 'accessible,inaccessible')
    party_set = set(party_param.split(','))
    access_set = set(access_param.split(','))
    buildings = {}
    for v in state['voters']:
        if v['party'] not in party_set: continue
        k = v['geocodeKey']
        if k not in buildings:
            coords = state['geocache'].get(k)
            if not coords: continue
            buildings[k] = {
                'buildingAddress': v['buildingAddress'],
                'city': v['city'], 'state': v['state'], 'zip': v['zip'],
                'apt': v['apt'], 'lat': coords[0], 'lng': coords[1], 'voters': [],
            }
        buildings[k]['voters'].append({'name': v['name'], 'unit': v['unit'], 'party': v['party'], 'yob': v.get('yob')})
    result = list(buildings.values())
    if 'accessible' not in access_set: result = [b for b in result if b['apt']]
    elif 'inaccessible' not in access_set: result = [b for b in result if not b['apt']]
    for b in result: b['dist'] = haversine(lat, lng, b['lat'], b['lng'])
    result.sort(key=lambda b: b['dist'])
    limit = min(int(request.args.get('limit', 30)), 100)
    result = result[:limit]
    for b in result:
        if b['apt']:
            b['voters'].sort(key=lambda v: (int(v['unit']) if v['unit'] and v['unit'].isdigit() else 9999, v['unit'] or ''))
    return jsonify({'results': result})

@app.route('/api/geocode')
def api_geocode():
    addr = request.args.get('address', '')
    if not addr: return jsonify({'error': 'address required'}), 400
    # Census Geocoder — no rate limit
    try:
        full = addr if 'denver' in addr.lower() or 'co' in addr.lower() else addr + ', Denver, CO'
        r = requests.get('https://geocoding.geo.census.gov/geocoder/locations/onelineaddress',
            params={'address': full, 'benchmark': 'Public_AR_Current', 'format': 'json'}, timeout=8)
        matches = r.json().get('result', {}).get('addressMatches', [])
        if matches:
            c = matches[0]['coordinates']
            lat, lng = float(c['y']), float(c['x'])
            if 39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600:
                return jsonify({'lat': lat, 'lng': lng})
    except Exception as e:
        print(f"Census geocode error: {e}")
    # Fallback Nominatim
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
            params={'q': addr + ', Denver, CO', 'format': 'json', 'limit': 1,
                    'countrycodes': 'us', 'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1},
            headers={'User-Agent': 'BallotReturnFinder/1.0'}, timeout=5)
        data = r.json()
        if data:
            lat, lng = float(data[0]['lat']), float(data[0]['lon'])
            if 39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600:
                return jsonify({'lat': lat, 'lng': lng})
    except Exception as e:
        print(f"Nominatim geocode error: {e}")
    return jsonify({'error': 'Address not found in Denver'}), 404

@app.route('/api/autocomplete')
def api_autocomplete():
    q = request.args.get('q', '')
    if len(q) < 3: return jsonify([])
    # Census Geocoder autocomplete
    try:
        r = requests.get('https://geocoding.geo.census.gov/geocoder/locations/onelineaddress',
            params={'address': q + ', Denver, CO', 'benchmark': 'Public_AR_Current', 'format': 'json'}, timeout=5)
        matches = r.json().get('result', {}).get('addressMatches', [])
        results = []
        for m in matches[:5]:
            c = m['coordinates']
            lat, lng = float(c['y']), float(c['x'])
            if not (39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600): continue
            main = m.get('matchedAddress', '').split(',')[0]
            results.append({'main': main, 'lat': lat, 'lng': lng})
        if results: return jsonify(results)
    except Exception as e:
        print(f"Census autocomplete error: {e}")
    # Fallback Nominatim
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
            params={'q': q + ', Denver, CO', 'format': 'json', 'limit': 5,
                    'countrycodes': 'us', 'addressdetails': 1,
                    'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1},
            headers={'User-Agent': 'BallotReturnFinder/1.0'}, timeout=5)
        results = []
        for item in r.json():
            a = item.get('address', {})
            lat, lng = float(item['lat']), float(item['lon'])
            if not (39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600): continue
            main = ' '.join(filter(None, [a.get('house_number'), a.get('road')])) or item['display_name'].split(',')[0]
            results.append({'main': main, 'lat': lat, 'lng': lng})
        return jsonify(results)
    except Exception:
        return jsonify([])


@app.route('/api/start-geocoding', methods=['POST'])
def start_geocoding():
    password = request.json.get('password', '')
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Invalid password'}), 401
    if state['loading']:
        return jsonify({'message': 'Geocoding already running'})
    if not state['voters']:
        return jsonify({'error': 'No data loaded'}), 400
    t = threading.Thread(target=geocode_all_background, daemon=True)
    t.start()
    return jsonify({'success': True, 'message': 'Geocoding started'})

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
    filename = secure_filename(f.filename)
    try:
        bytes_written = 0
        with open(SAVED_DATA_FILE, 'wb') as fout:
            while True:
                chunk = f.stream.read(1024 * 1024)
                if not chunk: break
                fout.write(chunk)
                bytes_written += len(chunk)
        print(f"Streamed {bytes_written:,} bytes to disk")
    except Exception as e:
        print(f"File save error: {e}")
        return jsonify({'error': f'File save failed: {str(e)}'}), 500

    def do_parse_and_geocode():
        try:
            parse_from_disk(filename)
            geocode_all_background()
        except Exception as e:
            print(f"Background parse/geocode error: {e}")
            import traceback; traceback.print_exc()

    t = threading.Thread(target=do_parse_and_geocode, daemon=True)
    t.start()
    return jsonify({'success': True, 'message': 'File received. Parsing in background.', 'filename': filename})

# ── STARTUP ───────────────────────────────────────────────────────
load_geocache()

def startup_reload():
    if os.path.exists(SAVED_DATA_FILE):
        try:
            filename = 'saved_data.txt'
            if os.path.exists(SAVED_META_FILE):
                with open(SAVED_META_FILE, 'r') as f:
                    meta = json.load(f)
                filename = meta.get('filename', filename)
            size = os.path.getsize(SAVED_DATA_FILE)
            print(f"Found saved data: {filename} ({size:,} bytes) — reloading...")
            parse_from_disk(filename)
            print(f"Startup reload complete: {state['total']:,} total, {len(state['voters']):,} not returned")
        except Exception as e:
            print(f"Startup reload error: {e}")
            import traceback; traceback.print_exc()
    else:
        print("No saved data file found — waiting for upload")

# Run startup reload synchronously — blocks until data is loaded
startup_reload()

# After loading, auto-start geocoding if there are uncached addresses
def auto_geocode():
    if state['voters']:
        buildings = set(v['geocodeKey'] for v in state['voters'])
        # Count addresses that are either not cached OR cached as None (failed)
        uncached = [k for k in buildings if state['geocache'].get(k) is None]
        cached_ok = [k for k in buildings if state['geocache'].get(k) is not None]
        print(f"Geocache status: {len(cached_ok):,} cached, {len(uncached):,} need geocoding")
        if uncached:
            print(f"Auto-starting geocoding for {len(uncached):,} addresses...")
            geocode_all_background()
        else:
            print("All addresses already geocoded successfully")

t = threading.Thread(target=auto_geocode, daemon=True)
t.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
