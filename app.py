import os, re, json, math, time, threading, requests
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')
DATA_DIR = os.environ.get('RENDER_DISK_PATH', 'data')
GEOCACHE_FILE = os.path.join(DATA_DIR, 'geocache.json')
VOTERS_FILE   = os.path.join(DATA_DIR, 'voters.json')    # parsed voter records
META_FILE     = os.path.join(DATA_DIR, 'meta.json')
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
            with open(GEOCACHE_FILE) as f:
                state['geocache'] = json.load(f)
            print(f"Loaded {len(state['geocache']):,} geocodes")
        except Exception as e:
            print(f"Geocache load error: {e}")

def save_geocache():
    try:
        with open(GEOCACHE_FILE, 'w') as f:
            json.dump(state['geocache'], f)
    except Exception as e:
        print(f"Geocache save error: {e}")

def geocode_one(building_addr, city, state_abbr, zip5):
    key = f"{building_addr},{city},co,{zip5}".lower().replace('  ',' ')
    if key in state['geocache']:
        return state['geocache'][key], key
    q = f"{building_addr}, {city}, {state_abbr} {zip5}"
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
            params={'q':q,'format':'json','limit':1,'countrycodes':'us',
                    'viewbox':'-105.110,39.614,-104.600,39.914','bounded':1},
            headers={'User-Agent':'BallotReturnFinder/1.0'}, timeout=5)
        data = r.json()
        if data:
            lat,lng = float(data[0]['lat']),float(data[0]['lon'])
            if 39.614<=lat<=39.914 and -105.110<=lng<=-104.600:
                state['geocache'][key]=[lat,lng]
                return [lat,lng], key
    except: pass
    state['geocache'][key] = None
    return None, key

# ── CSV PARSING ───────────────────────────────────────────────────
def split_line(line, delim):
    r,cur,inq=[],'' ,False
    for ch in line:
        if ch=='"': inq=not inq
        elif ch==delim and not inq: r.append(cur); cur=''
        else: cur+=ch
    r.append(cur)
    return r

def parse_stream(stream, filename):
    """Parse file stream line by line — never loads whole file into memory."""
    first = stream.readline()
    if isinstance(first, bytes): first = first.decode('utf-8','replace')
    first = first.strip()
    delim = '|' if '|' in first else ','
    header = split_line(first, delim)
    col = {n.strip():i for i,n in enumerate(header)}

    required = ['VOTER_ID','FIRST_NAME','LAST_NAME','PARTY',
                'RES_ADDRESS','RES_CITY','RES_STATE','RES_ZIP']
    missing = [c for c in required if c not in col]
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    total,returned,voters = 0,0,[]

    for raw in stream:
        if isinstance(raw, bytes): raw = raw.decode('utf-8','replace')
        line = raw.strip()
        if not line: continue
        cols = split_line(line, delim)
        total += 1

        def get(name, default=''):
            idx = col.get(name)
            return cols[idx].strip() if idx is not None and idx < len(cols) else default

        if is_date(get('MAIL_BALLOT_RECEIVE_DATE')) or is_date(get('IN_PERSON_VOTE_DATE')):
            returned += 1
            continue

        res_addr = get('RES_ADDRESS')
        if not res_addr: continue

        um = re.search(r'#\s*(\S+)\s*$', res_addr)
        unit = um.group(1) if um else None
        bldg = re.sub(r'\s*#\s*\S+\s*$','',res_addr).strip() if unit else res_addr
        city = get('RES_CITY') or 'DENVER'
        st   = get('RES_STATE') or 'CO'
        zip5 = get('RES_ZIP').split('-')[0]
        key  = f"{bldg},{city},co,{zip5}".lower().replace('  ',' ')

        voters.append({
            'name': f"{get('FIRST_NAME')} {get('LAST_NAME')}".strip(),
            'unit': unit, 'buildingAddress': bldg,
            'city': city, 'state': st, 'zip': zip5,
            'geocodeKey': key, 'party': get('PARTY') or 'UAF',
            'apt': unit is not None,
        })

    return total, returned, voters

def save_voters():
    """Save parsed voter records to disk as compact JSON."""
    try:
        with open(VOTERS_FILE,'w') as f:
            json.dump({'total':state['total'],'returned':state['returned'],
                       'filename':state['filename'],'loaded_at':state['loaded_at'],
                       'voters':state['voters']}, f)
        size = os.path.getsize(VOTERS_FILE)
        print(f"Saved {len(state['voters']):,} voter records to disk ({size/1024/1024:.1f} MB)")
    except Exception as e:
        print(f"Voter save error: {e}")

def load_voters():
    """Load parsed voter records from disk."""
    if not os.path.exists(VOTERS_FILE):
        print("No voters.json found on disk")
        return False
    try:
        size = os.path.getsize(VOTERS_FILE)
        print(f"Loading voters.json from disk ({size/1024/1024:.1f} MB)...")
        with open(VOTERS_FILE) as f:
            data = json.load(f)
        state['voters']    = data['voters']
        state['total']     = data['total']
        state['returned']  = data['returned']
        state['filename']  = data['filename']
        state['loaded_at'] = data['loaded_at']
        print(f"Loaded {len(state['voters']):,} voter records from disk")
        return True
    except Exception as e:
        print(f"Voter load error: {e}")
        return False

def geocode_background():
    state['loading'] = True
    buildings = {}
    for v in state['voters']:
        k = v['geocodeKey']
        if k not in buildings:
            buildings[k] = v
    need = {k:v for k,v in buildings.items() if k not in state['geocache']}
    total = len(need)
    done = 0
    print(f"Geocoding {total:,} new addresses...")
    for key,v in need.items():
        geocode_one(v['buildingAddress'],v['city'],v['state'],v['zip'])
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
def haversine(la1,ln1,la2,ln2):
    R=3958.8; dLa=math.radians(la2-la1); dLn=math.radians(ln2-ln1)
    a=math.sin(dLa/2)**2+math.cos(math.radians(la1))*math.cos(math.radians(la2))*math.sin(dLn/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

# ── ROUTES ────────────────────────────────────────────────────────
@app.route('/')
def index(): return send_from_directory('static','index.html')

@app.route('/api/status')
def api_status():
    rate = round(state['returned']/state['total']*100) if state['total'] else 0
    return jsonify({
        'total':state['total'], 'pending':len(state['voters']),
        'returned':state['returned'], 'returnRate':rate,
        'filename':state['filename'], 'loadedAt':state['loaded_at'],
        'loading':state['loading'], 'loadProgress':state['load_progress'],
        'hasData':len(state['voters'])>0,
    })

@app.route('/api/search')
def api_search():
    try: lat=float(request.args.get('lat')); lng=float(request.args.get('lng'))
    except: return jsonify({'error':'lat and lng required'}),400
    party_set  = set(request.args.get('party','DEM,UAF').split(','))
    access_set = set(request.args.get('access','accessible,inaccessible').split(','))

    buildings = {}
    for v in state['voters']:
        if v['party'] not in party_set: continue
        k = v['geocodeKey']
        if k not in buildings:
            coords = state['geocache'].get(k)
            if not coords: continue
            buildings[k] = {'buildingAddress':v['buildingAddress'],
                'city':v['city'],'state':v['state'],'zip':v['zip'],
                'apt':v['apt'],'lat':coords[0],'lng':coords[1],'voters':[]}
        buildings[k]['voters'].append({'name':v['name'],'unit':v['unit'],'party':v['party']})

    result = list(buildings.values())
    if 'accessible' not in access_set:   result=[b for b in result if b['apt']]
    elif 'inaccessible' not in access_set: result=[b for b in result if not b['apt']]

    for b in result: b['dist']=haversine(lat,lng,b['lat'],b['lng'])
    result.sort(key=lambda b:b['dist'])
    result=result[:100]
    for b in result:
        if b['apt']:
            b['voters'].sort(key=lambda v:(int(v['unit']) if v['unit'] and v['unit'].isdigit() else 9999, v['unit'] or ''))
    return jsonify({'results':result})

@app.route('/api/geocode')
def api_geocode():
    addr = request.args.get('address', '')
    if not addr:
        return jsonify({'error': 'address required'}), 400
    
    # Try Census Geocoder first (no rate limit, very accurate for US addresses)
    try:
        full_addr = addr if 'denver' in addr.lower() or 'co' in addr.lower() else addr + ', Denver, CO'
        url = 'https://geocoding.geo.census.gov/geocoder/locations/onelineaddress'
        params = {'address': full_addr, 'benchmark': 'Public_AR_Current', 'format': 'json'}
        r = requests.get(url, params=params, timeout=8)
        data = r.json()
        matches = data.get('result', {}).get('addressMatches', [])
        if matches:
            coords = matches[0]['coordinates']
            lat, lng = float(coords['y']), float(coords['x'])
            if 39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600:
                return jsonify({'lat': lat, 'lng': lng})
    except Exception as e:
        print(f"Census geocode error: {e}")

    # Fallback to Nominatim
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
            params={'q': addr + ', Denver, CO', 'format': 'json', 'limit': 1,
                    'countrycodes': 'us',
                    'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1},
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
    if len(q) < 3:
        return jsonify([])
    
    # Use Census geocoder for autocomplete suggestions
    try:
        url = 'https://geocoding.geo.census.gov/geocoder/locations/onelineaddress'
        params = {'address': q + ', Denver, CO', 'benchmark': 'Public_AR_Current', 'format': 'json'}
        r = requests.get(url, params=params, timeout=5)
        data = r.json()
        matches = data.get('result', {}).get('addressMatches', [])
        results = []
        for m in matches[:5]:
            coords = m['coordinates']
            lat, lng = float(coords['y']), float(coords['x'])
            if not (39.614 <= lat <= 39.914 and -105.110 <= lng <= -104.600):
                continue
            addr_parts = m.get('addressComponents', {})
            main = m.get('matchedAddress', '').split(',')[0]
            results.append({'main': main, 'lat': lat, 'lng': lng})
        if results:
            return jsonify(results)
    except Exception as e:
        print(f"Census autocomplete error: {e}")

    # Fallback to Nominatim
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
            params={'q': q + ', Denver, CO', 'format': 'json', 'limit': 5,
                    'countrycodes': 'us', 'addressdetails': 1,
                    'viewbox': '-105.110,39.614,-104.600,39.914', 'bounded': 1},
            headers={'User-Agent': 'BallotReturnFinder/1.0'}, timeout=5)
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


