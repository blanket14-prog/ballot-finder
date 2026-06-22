import os, re, json, math, time, threading, requests, datetime
from flask import Flask, request, jsonify, send_from_directory, Response
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

# ── CAMPAIGN CONFIGS ──────────────────────────────────────────────
BASE_DATA_DIR = '/opt/render/project/src/data'
SHARED_DATA_FILE = os.path.join(BASE_DATA_DIR, 'current_data.txt')
SHARED_META_FILE = os.path.join(BASE_DATA_DIR, 'meta.json')

CAMPAIGNS = {
    'default': {
        'name': 'Ballot Finder',
        'logo': '',
        'color': '#6c63ff',
        'password': os.environ.get('ADMIN_PASSWORD', 'changeme'),
        'data_dir': BASE_DATA_DIR,
        'theme': 'dark',  # dark or light
    },
    'melat': {
        'name': 'Melat Kiros for Congress',
        'logo': 'melat',
        'color': '#1a5fa8',
        'password': os.environ.get('MELAT_PASSWORD', 'changeme'),
        'data_dir': os.path.join(BASE_DATA_DIR, 'melat'),
        'theme': 'dark',
    },
    'phil': {
        'name': 'Phil Weiser for Governor',
        'logo': 'phil',
        'color': '#1a5fa8',
        'password': os.environ.get('PHIL_PASSWORD', 'changeme'),
        'data_dir': os.path.join(BASE_DATA_DIR, 'phil'),
        'theme': 'dark',
    },
    'denverdems': {
        'name': 'Denver Democrats',
        'logo': 'denverdems',
        'color': '#1a5fa8',
        'password': os.environ.get('DENVERDEMS_PASSWORD', 'changeme'),
        'data_dir': os.path.join(BASE_DATA_DIR, 'denverdems'),
        'theme': 'dark',
    },
}

# ── PER-CAMPAIGN STATE ────────────────────────────────────────────
# Each campaign has completely independent data
states = {}
for cid, cfg in CAMPAIGNS.items():
    states[cid] = {
        'voters': [], 'total': 0, 'returned': 0,
        'filename': '', 'loaded_at': '',
        'geocache': {}, 'loading': False, 'load_progress': '',
    }

def get_campaign(path_prefix):
    """Determine campaign from URL prefix."""
    if path_prefix in CAMPAIGNS:
        return path_prefix
    return 'default'

def data_file(cid, name):
    # All files shared from base data dir — same voter data, same geocache
    return os.path.join(BASE_DATA_DIR, name)

# ── GEOCACHE ──────────────────────────────────────────────────────
def load_geocache(cid):
    f = data_file(cid, 'geocache.json')
    if os.path.exists(f):
        try:
            with open(f) as fp:
                states[cid]['geocache'] = json.load(fp)
            print(f"[{cid}] Loaded {len(states[cid]['geocache'])} cached geocodes")
        except Exception as e:
            print(f"[{cid}] Geocache load error: {e}")

def save_geocache(cid):
    try:
        with open(data_file(cid, 'geocache.json'), 'w') as f:
            json.dump(states[cid]['geocache'], f)
    except Exception as e:
        print(f"[{cid}] Geocache save error: {e}")

def geocode_nominatim(building_addr, city, state_abbr, zip5):
    """Nominatim (OpenStreetMap) geocoder - more accurate parcel-level results."""
    try:
        full = f"{building_addr}, {city}, {state_abbr} {zip5}"
        r = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={
                'q': full,
                'format': 'json',
                'limit': 1,
                'countrycodes': 'us',
                'addressdetails': 0,
            },
            headers={'User-Agent': 'BallotFinder/2.0 (Denver GOTV tool)'},
            timeout=8)
        data = r.json()
        if data:
            lat, lng = float(data[0]['lat']), float(data[0]['lon'])
            if 39.0 <= lat <= 40.5 and -106.0 <= lng <= -104.0:
                return [lat, lng]
    except Exception as e:
        pass
    return None

def geocode_census_fallback(building_addr, city, state_abbr, zip5):
    """Census geocoder as fallback - less accurate but reliable."""
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
            if 39.0 <= lat <= 40.5 and -106.0 <= lng <= -104.0:
                return [lat, lng]
    except Exception as e:
        pass
    return None

def geocode_census(cid, building_addr, city, state_abbr, zip5):
    """Geocode with Nominatim first, Census as fallback. Cache result."""
    key = f"{building_addr},{city},co,{zip5}".lower().replace('  ', ' ')
    gc = states[cid]['geocache']
    if key in gc and gc[key] is not None:
        return gc[key], key

    # Try Nominatim first (more accurate)
    result = geocode_nominatim(building_addr, city, state_abbr, zip5)

    # Fall back to Census if Nominatim fails
    if result is None:
        result = geocode_census_fallback(building_addr, city, state_abbr, zip5)

    gc[key] = result
    return result, key

# ── CSV PARSING ───────────────────────────────────────────────────
DATE_RE = re.compile(r'^\d{2}/\d{2}/\d{4}$')
def is_date(v): return bool(v and DATE_RE.match(v.strip()))

def split_line(line, delim):
    result, cur, in_q = [], '', False
    for ch in line:
        if ch == '"': in_q = not in_q
        elif ch == delim and not in_q: result.append(cur); cur = ''
        else: cur += ch
    result.append(cur)
    return result

def parse_from_disk(cid, filename):
    saved = data_file(cid, 'current_data.txt')
    print(f"[{cid}] Parsing from disk: {saved}")
    voters, total, returned = [], 0, 0
    with open(saved, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    if not lines: return
    delim = '|' if '|' in lines[0] else ','
    header_cols = split_line(lines[0].strip(), delim)
    col = {name.strip(): i for i, name in enumerate(header_cols)}
    required = ['VOTER_ID','FIRST_NAME','LAST_NAME','PARTY','RES_ADDRESS','RES_CITY','RES_STATE','RES_ZIP']
    missing = [c for c in required if c not in col]
    if missing: raise ValueError(f"Missing columns: {missing}")
    print(f"[{cid}] Columns OK. MAIL_BALLOT_RECEIVE_DATE at {col.get('MAIL_BALLOT_RECEIVE_DATE')}")
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
        yob_raw = get('YOB','').strip()
        yob = int(yob_raw) if yob_raw.isdigit() else None
        geocode_key = f"{building_addr},{city},co,{zip5}".lower().replace('  ', ' ')
        voters.append({
            'name': f"{get('FIRST_NAME')} {get('LAST_NAME')}".strip(),
            'unit': unit, 'buildingAddress': building_addr,
            'city': city, 'state': state_abbr, 'zip': zip5,
            'geocodeKey': geocode_key, 'party': party,
            'apt': unit is not None, 'yob': yob,
        })
    st = states[cid]
    st['voters'] = voters; st['total'] = total; st['returned'] = returned
    st['filename'] = filename
    mdt = datetime.timezone(datetime.timedelta(hours=-6))
    st['loaded_at'] = datetime.datetime.now(mdt).strftime('%-m/%-d/%Y at %-I:%M %p MDT')
    try:
        with open(data_file(cid, 'meta.json'), 'w') as f:
            json.dump({'filename': filename, 'loaded_at': st['loaded_at'],
                       'total': total, 'returned': returned}, f)
    except Exception as e:
        print(f"[{cid}] Meta save error: {e}")
    print(f"[{cid}] Parsed {total} total, {len(voters)} not returned, {returned} returned")
    # If default campaign updated, propagate shared data to all other campaigns
    if cid == 'default':
        for other_cid in states:
            if other_cid != 'default':
                states[other_cid]['voters'] = voters
                states[other_cid]['total'] = total
                states[other_cid]['returned'] = returned
                states[other_cid]['filename'] = filename
                states[other_cid]['loaded_at'] = states[cid]['loaded_at']

def geocode_all_background(cid):
    st = states[cid]
    st['loading'] = True
    buildings = {}
    for v in st['voters']:
        k = v['geocodeKey']
        if k not in buildings: buildings[k] = v
    need = {k: v for k, v in buildings.items() if st['geocache'].get(k) is None}
    total = len(need); done = 0
    print(f"[{cid}] Geocoding {total} addresses...")
    for key, v in need.items():
        geocode_census(cid, v['buildingAddress'], v['city'], v['state'], v['zip'])
        done += 1
        st['load_progress'] = f"Geocoding {done:,} / {total:,} addresses…"
        if done % 500 == 0:
            save_geocache(cid)
            print(f"[{cid}] {done}/{total} geocoded")
        time.sleep(1.1)  # Nominatim requires max 1 req/sec
    save_geocache(cid)
    st['loading'] = False; st['load_progress'] = ''
    print(f"[{cid}] Geocoding complete.")

# ── HAVERSINE ─────────────────────────────────────────────────────
def haversine(la1,ln1,la2,ln2):
    R=3958.8;dLa=(la2-la1)*math.pi/180;dLn=(ln2-ln1)*math.pi/180
    a=math.sin(dLa/2)**2+math.cos(la1*math.pi/180)*math.cos(la2*math.pi/180)*math.sin(dLn/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

# ── ROUTES ────────────────────────────────────────────────────────
def make_routes(prefix, cid):
    """Register all routes for a given campaign prefix."""
    url_prefix = f'/{prefix}' if prefix != 'default' else ''

    index_routes = [f'{url_prefix}/']
    if url_prefix:  # only add bare prefix route for non-default campaigns
        index_routes.append(f'{url_prefix}')
    for route in index_routes:
        app.add_url_rule(route, endpoint=f'index_{cid}_{route}',
                        view_func=lambda: send_from_directory('static', 'index.html'))

    @app.route(f'{url_prefix}/api/config', endpoint=f'config_{cid}')
    def api_config():
        cfg = CAMPAIGNS[cid]
        logo_url = f'/static/logo-{cfg["logo"]}.png' if cfg['logo'] else ''
        return jsonify({
            'campaignName': cfg['name'],
            'logoUrl': logo_url,
            'accentColor': cfg['color'],
            'prefix': url_prefix,
            'theme': cfg.get('theme', 'dark'),
        })

    @app.route(f'{url_prefix}/api/status', endpoint=f'status_{cid}')
    def api_status():
        st = states[cid]
        # Count DEM+UAF only for all three stats
        dem_uaf_pending = len([v for v in st['voters'] if v.get('party') in ('DEM','UAF')])
        dem_uaf_total = st['total'] - sum(1 for v in st['voters'] if v.get('party') == 'REP')
        dem_uaf_returned = dem_uaf_total - dem_uaf_pending
        rate = round(dem_uaf_returned / dem_uaf_total * 100) if dem_uaf_total > 0 else 0
        return jsonify({
            'total': dem_uaf_total, 'pending': dem_uaf_pending,
            'returned': dem_uaf_returned, 'returnRate': rate,
            'filename': st['filename'], 'loadedAt': st['loaded_at'],
            'loading': st['loading'], 'loadProgress': st['load_progress'],
            'hasData': len(st['voters']) > 0,
        })

    @app.route(f'{url_prefix}/api/search', endpoint=f'search_{cid}')
    def api_search():
        st = states[cid]
        try:
            lat = float(request.args.get('lat'))
            lng = float(request.args.get('lng'))
        except: return jsonify({'error': 'lat and lng required'}), 400
        party_set = set(request.args.get('party','DEM,UAF').split(','))
        access_set = set(request.args.get('access','accessible,inaccessible').split(','))
        limit = min(int(request.args.get('limit', 30)), 100)
        buildings = {}
        for v in st['voters']:
            if v['party'] not in party_set: continue
            k = v['geocodeKey']
            if k not in buildings:
                coords = st['geocache'].get(k)
                if not coords: continue
                buildings[k] = {
                    'buildingAddress': v['buildingAddress'],
                    'city': v['city'], 'state': v['state'], 'zip': v['zip'],
                    'apt': v['apt'], 'lat': coords[0], 'lng': coords[1], 'voters': [],
                }
            buildings[k]['voters'].append({
                'name': v['name'], 'unit': v['unit'],
                'party': v['party'], 'yob': v.get('yob'),
            })
        result = list(buildings.values())
        if 'accessible' not in access_set: result = [b for b in result if b['apt']]
        elif 'inaccessible' not in access_set: result = [b for b in result if not b['apt']]
        for b in result: b['dist'] = haversine(lat, lng, b['lat'], b['lng'])
        result.sort(key=lambda b: b['dist'])
        result = result[:limit]
        for b in result:
            if b['apt']:
                b['voters'].sort(key=lambda v: (
                    int(v['unit']) if v['unit'] and v['unit'].isdigit() else 9999,
                    v['unit'] or ''))
        return jsonify({'results': result})

    @app.route(f'{url_prefix}/api/geocode', endpoint=f'geocode_{cid}')
    def api_geocode():
        addr = request.args.get('address','')
        if not addr: return jsonify({'error': 'address required'}), 400
        try:
            full = addr if 'co' in addr.lower() else addr + ', Denver, CO'
            r = requests.get('https://geocoding.geo.census.gov/geocoder/locations/onelineaddress',
                params={'address': full, 'benchmark': 'Public_AR_Current', 'format': 'json'}, timeout=8)
            matches = r.json().get('result', {}).get('addressMatches', [])
            if matches:
                c = matches[0]['coordinates']
                lat, lng = float(c['y']), float(c['x'])
                if 38.5 <= lat <= 41.0 and -109.1 <= lng <= -102.0:
                    return jsonify({'lat': lat, 'lng': lng})
        except Exception as e: print(f"Census geocode error: {e}")
        try:
            r = requests.get('https://nominatim.openstreetmap.org/search',
                params={'q': addr + ', Denver, CO', 'format': 'json', 'limit': 1,
                        'countrycodes': 'us', 'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1},
                headers={'User-Agent': 'BallotFinder/1.0'}, timeout=5)
            data = r.json()
            if data:
                lat, lng = float(data[0]['lat']), float(data[0]['lon'])
                return jsonify({'lat': lat, 'lng': lng})
        except: pass
        return jsonify({'error': 'Address not found'}), 404

    @app.route(f'{url_prefix}/api/autocomplete', endpoint=f'autocomplete_{cid}')
    def api_autocomplete():
        q = request.args.get('q','')
        if len(q) < 3: return jsonify([])
        try:
            r = requests.get('https://geocoding.geo.census.gov/geocoder/locations/onelineaddress',
                params={'address': q + ', Denver, CO', 'benchmark': 'Public_AR_Current', 'format': 'json'}, timeout=5)
            matches = r.json().get('result', {}).get('addressMatches', [])
            results = []
            for m in matches[:5]:
                c = m['coordinates']
                lat, lng = float(c['y']), float(c['x'])
                main = m.get('matchedAddress','').split(',')[0]
                results.append({'main': main, 'lat': lat, 'lng': lng})
            if results: return jsonify(results)
        except: pass
        try:
            r = requests.get('https://nominatim.openstreetmap.org/search',
                params={'q': q+', Denver, CO', 'format': 'json', 'limit': 5,
                        'countrycodes': 'us', 'addressdetails': 1,
                        'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1},
                headers={'User-Agent': 'BallotFinder/1.0'}, timeout=5)
            results = []
            for item in r.json():
                a = item.get('address',{})
                lat, lng = float(item['lat']), float(item['lon'])
                main = ' '.join(filter(None,[a.get('house_number'),a.get('road')])) or item['display_name'].split(',')[0]
                results.append({'main': main, 'lat': lat, 'lng': lng})
            return jsonify(results)
        except: return jsonify([])

    @app.route(f'{url_prefix}/admin', methods=['GET','POST'], endpoint=f'admin_{cid}')
    def admin():
        if request.method == 'GET':
            return send_from_directory('static', 'admin.html')
        password = request.form.get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        f = request.files['file']
        if not f.filename: return jsonify({'error': 'Empty filename'}), 400
        filename = secure_filename(f.filename)
        saved = data_file(cid, 'current_data.txt')
        try:
            bytes_written = 0
            with open(saved, 'wb') as fout:
                while True:
                    chunk = f.stream.read(1024*1024)
                    if not chunk: break
                    fout.write(chunk)
                    bytes_written += len(chunk)
            print(f"[{cid}] Streamed {bytes_written:,} bytes to disk")
        except Exception as e:
            return jsonify({'error': f'File save failed: {str(e)}'}), 500
        def do_parse():
            try:
                parse_from_disk(cid, filename)
                geocode_all_background(cid)
            except Exception as e:
                print(f"[{cid}] Parse error: {e}")
                import traceback; traceback.print_exc()
        threading.Thread(target=do_parse, daemon=True).start()
        return jsonify({'success': True, 'message': 'File received. Parsing in background.', 'filename': filename})

    @app.route(f'{url_prefix}/api/set-theme', methods=['POST'], endpoint=f'settheme_{cid}')
    def set_theme():
        password = (request.json or {}).get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        theme = (request.json or {}).get('theme', 'dark')
        if theme not in ('dark', 'light'):
            return jsonify({'error': 'Invalid theme'}), 400
        CAMPAIGNS[cid]['theme'] = theme
        save_theme(cid, theme)
        return jsonify({'success': True, 'theme': theme})

    @app.route(f'{url_prefix}/api/clear-geocache', methods=['POST'], endpoint=f'cleargeo_{cid}')
    def clear_geocache():
        password = (request.json or {}).get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        states[cid]['geocache'] = {}
        save_geocache(cid)
        print(f"[{cid}] Geocache cleared by admin")
        return jsonify({'success': True, 'message': 'Geocache cleared. Re-geocoding will use Nominatim.'})

    @app.route(f'{url_prefix}/api/start-geocoding', methods=['POST'], endpoint=f'startgeo_{cid}')
    def start_geocoding():
        password = (request.json or {}).get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        if states[cid]['loading']: return jsonify({'message': 'Already running'})
        threading.Thread(target=geocode_all_background, args=(cid,), daemon=True).start()
        return jsonify({'success': True})

# Register routes for all campaigns
make_routes('default', 'default')
make_routes('melat', 'melat')
make_routes('phil', 'phil')
make_routes('denverdems', 'denverdems')

# Static files
@app.route('/static/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/static/sw.js')
def service_worker():
    with open('static/sw.js') as f: js = f.read()
    return Response(js, mimetype='application/javascript', headers={'Service-Worker-Allowed': '/'})

# ── STARTUP ───────────────────────────────────────────────────────
def theme_file(cid):
    return os.path.join(BASE_DATA_DIR, f'theme_{cid}.json')

def load_theme(cid):
    try:
        tf = theme_file(cid)
        if os.path.exists(tf):
            with open(tf) as f:
                data = json.load(f)
                CAMPAIGNS[cid]['theme'] = data.get('theme', 'dark')
                print(f"[{cid}] Loaded theme: {CAMPAIGNS[cid]['theme']}")
    except Exception as e:
        print(f"[{cid}] Theme load error: {e}")

def save_theme(cid, theme):
    try:
        with open(theme_file(cid), 'w') as f:
            json.dump({'theme': theme}, f)
    except Exception as e:
        print(f"[{cid}] Theme save error: {e}")

def startup_campaign(cid):
    try:
        os.makedirs(CAMPAIGNS[cid]['data_dir'], exist_ok=True)
    except Exception as e:
        print(f"[{cid}] Could not create data dir: {e}")
    load_theme(cid)
    load_geocache(cid)
    saved = data_file(cid, 'current_data.txt')
    meta = data_file(cid, 'meta.json')
    if os.path.exists(saved):
        try:
            filename = 'saved_data.txt'
            if os.path.exists(meta):
                with open(meta) as f:
                    filename = json.load(f).get('filename', filename)
            size = os.path.getsize(saved)
            print(f"[{cid}] Found saved data: {filename} ({size:,} bytes) — reloading...")
            parse_from_disk(cid, filename)
            print(f"[{cid}] Reload complete: {states[cid]['total']:,} voters")
        except Exception as e:
            print(f"[{cid}] Startup reload error: {e}")
            import traceback; traceback.print_exc()
    else:
        print(f"[{cid}] No saved data — waiting for upload")

def auto_geocode(cid):
    st = states[cid]
    if st['voters']:
        buildings = set(v['geocodeKey'] for v in st['voters'])
        uncached = [k for k in buildings if st['geocache'].get(k) is None]
        cached_ok = len(buildings) - len(uncached)
        print(f"[{cid}] Geocache: {cached_ok:,} cached, {len(uncached):,} need geocoding")
        if uncached:
            geocode_all_background(cid)

# Load voter data ONCE (shared across all campaigns)
startup_campaign('default')
shared_voters = states['default']['voters']
shared_total = states['default']['total']
shared_returned = states['default']['returned']
shared_filename = states['default']['filename']
shared_loaded_at = states['default']['loaded_at']

# Other campaigns share the voter list but have their own geocache
for cid in CAMPAIGNS:
    if cid == 'default':
        continue
    try:
        os.makedirs(CAMPAIGNS[cid]['data_dir'], exist_ok=True)
    except: pass
    load_geocache(cid)
    # Share the voter data from default
    states[cid]['voters'] = shared_voters
    states[cid]['total'] = shared_total
    states[cid]['returned'] = shared_returned
    states[cid]['filename'] = shared_filename
    states[cid]['loaded_at'] = shared_loaded_at
    print(f"[{cid}] Sharing voter data from default: {shared_total:,} voters")

# Start geocoding for any that need it
def startup_geocoding():
    time.sleep(2)
    for cid in CAMPAIGNS:
        auto_geocode(cid)

threading.Thread(target=startup_geocoding, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
