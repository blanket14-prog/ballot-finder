import os, re, json, math, time, threading, requests, datetime
from flask import Flask, request, jsonify, send_from_directory, Response
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

# ── ADMIN RATE LIMITING ───────────────────────────────────────────
_failed_attempts = {}  # ip -> [timestamp, ...]
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 900  # 15 minutes

def _check_rate_limit(ip):
    now = time.time()
    attempts = [t for t in _failed_attempts.get(ip, []) if now - t < _LOCKOUT_SECONDS]
    _failed_attempts[ip] = attempts
    return len(attempts) >= _MAX_ATTEMPTS

def _record_failed(ip):
    now = time.time()
    _failed_attempts.setdefault(ip, []).append(now)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

# ── CAMPAIGN CONFIGS ──────────────────────────────────────────────
BASE_DATA_DIR = '/opt/render/project/src/data'
os.makedirs(BASE_DATA_DIR, exist_ok=True)  # ensure root data dir exists
SHARED_DATA_FILE = os.path.join(BASE_DATA_DIR, 'current_data.txt')
SHARED_META_FILE = os.path.join(BASE_DATA_DIR, 'meta.json')

CAMPAIGNS = {
    'default': {
        'name': 'Denver Ballot Finder',
        'logo': '',
        'color': '#6c63ff',
        'password': os.environ.get('ADMIN_PASSWORD', 'changeme'),
        'data_dir': BASE_DATA_DIR,
        'theme': 'dark',
        'public_password': '',
        'show_party_filter': True,
        'show_candidate_filter': False,
        'show_rep_filter': True,     # show Republican filter (neutral public page)
        'all_voters_stats': True,    # show all voters in stats (not just DEM+UAF)
    },
    'melat': {
        'name': 'Melat Kiros for Congress',
        'logo': 'melat',
        'color': '#1a5fa8',
        'password': os.environ.get('MELAT_PASSWORD', 'changeme'),
        'data_dir': os.path.join(BASE_DATA_DIR, 'melat'),
        'theme': 'dark',
        'public_password': '',
        'show_party_filter': True,
        'show_candidate_filter': False,
        'show_rep_filter': False,
        'all_voters_stats': False,
    },
    'phil': {
        'name': 'Phil Weiser for Governor',
        'logo': 'phil',
        'color': '#175ec8',
        'password': os.environ.get('PHIL_PASSWORD', 'changeme'),
        'data_dir': os.path.join(BASE_DATA_DIR, 'phil'),
        'theme': 'dark',
        'public_password': '',
        'show_party_filter': False,
        'show_candidate_filter': False,
        'show_rep_filter': False,
        'all_voters_stats': False,
        'hide_stats': True,
        'tagline': 'Help Phil win — find priority voters near you who haven’t returned their ballot.',
    },
    'arapahoe': {
        'name': 'Arapahoe Ballot Finder',
        'logo': '',
        'color': '#1a6fa8',
        'password': os.environ.get('ADMIN_PASSWORD', 'changeme'),
        'data_dir': os.path.join(BASE_DATA_DIR, 'default'),
        'theme': 'dark',
        'public_password': '',
        'show_party_filter': True,
        'show_candidate_filter': False,
        'show_rep_filter': True,
        'all_voters_stats': False,
        'map_center': [39.6508, -104.8858],
        'map_zoom': 12,
        'county': 'Arapahoe',
    },
    'denverdems': {
        'name': 'Denver Democrats',
        'logo': 'denverdems',
        'color': '#1a5fa8',
        'password': os.environ.get('DENVERDEMS_PASSWORD', 'changeme'),
        'data_dir': os.path.join(BASE_DATA_DIR, 'denverdems'),
        'theme': 'dark',
        'public_password': '',
        'show_party_filter': True,
        'show_candidate_filter': False,
        'show_rep_filter': False,
        'all_voters_stats': False,
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

# ── NOMINATIM BACKGROUND GEOCODER ────────────────────────────────
# Builds a new geocache using Nominatim without touching the live cache.
# Admin can flip to it once complete.

nominatim_cache = {}          # built in background
nominatim_progress = {'done': 0, 'total': 0, 'running': False, 'complete': False}
NOMINATIM_CACHE_FILE = os.path.join(BASE_DATA_DIR, 'geocache_nominatim.json')

def load_nominatim_cache():
    global nominatim_cache
    if os.path.exists(NOMINATIM_CACHE_FILE):
        try:
            with open(NOMINATIM_CACHE_FILE) as f:
                nominatim_cache = json.load(f)
            count = len(nominatim_cache)
            print(f"Loaded {count:,} Nominatim cached geocodes")
            if count > 0:
                nominatim_progress['done'] = count
                nominatim_progress['total'] = count  # will be updated when build starts
                # Mark as paused (not complete, not running) so UI shows resume option
                nominatim_progress['running'] = False
                nominatim_progress['complete'] = False
                print(f"Nominatim: {count:,} cached — ready to resume or flip")
        except Exception as e:
            print(f"Nominatim cache load error: {e}")

def save_nominatim_cache():
    try:
        # Atomic write: write to temp file then rename to avoid corruption on interrupt
        tmp_file = NOMINATIM_CACHE_FILE + '.tmp'
        # Merge with existing to never lose entries
        merged = {}
        if os.path.exists(NOMINATIM_CACHE_FILE):
            try:
                with open(NOMINATIM_CACHE_FILE) as f:
                    merged = json.load(f)
            except: pass
        merged.update(nominatim_cache)
        nominatim_cache.update(merged)
        with open(tmp_file, 'w') as f:
            json.dump(merged, f)
        os.replace(tmp_file, NOMINATIM_CACHE_FILE)  # atomic rename
        print(f"Nominatim cache saved: {len(merged):,} entries")
    except Exception as e:
        print(f"Nominatim cache save error: {e}")

def geocode_nominatim_single(building_addr, city, state_abbr, zip5):
    try:
        full = f"{building_addr}, {city}, {state_abbr} {zip5}"
        r = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': full, 'format': 'json', 'limit': 1, 'countrycodes': 'us', 'featuretype': 'address'},
            headers={'User-Agent': 'BallotFinder/2.0 (Denver GOTV)'},
            timeout=8)
        data = r.json()
        if data:
            lat, lng = float(data[0]['lat']), float(data[0]['lon'])
            if 39.0 <= lat <= 40.5 and -106.0 <= lng <= -104.0:
                return [lat, lng]
    except Exception:
        pass
    return None

def run_nominatim_build(voters):
    global nominatim_cache
    nominatim_progress['running'] = True
    nominatim_progress['complete'] = False

    # Get unique building keys
    buildings = {}
    for v in voters:
        k = v['geocodeKey']
        if k not in buildings:
            buildings[k] = v

    # Skip already done
    todo = {k: v for k, v in buildings.items() if k not in nominatim_cache}
    nominatim_progress['total'] = len(buildings)
    nominatim_progress['done'] = len(buildings) - len(todo)
    print(f"Nominatim: {len(todo):,} addresses to geocode ({nominatim_progress['done']:,} already done)")

    for key, v in todo.items():
        result = geocode_nominatim_single(v['buildingAddress'], v['city'], v['state'], v['zip'])
        if result is None:
            # Fall back to Census for this address
            result = geocode_census_original(v['buildingAddress'], v['city'], v['state'], v['zip'])
        nominatim_cache[key] = result
        nominatim_progress['done'] += 1
        if nominatim_progress['done'] % 500 == 0:
            save_nominatim_cache()
            pct = round(nominatim_progress['done'] / nominatim_progress['total'] * 100)
            print(f"Nominatim: {nominatim_progress['done']:,}/{nominatim_progress['total']:,} ({pct}%)")
        time.sleep(1.1)  # Nominatim rate limit: 1 req/sec

    save_nominatim_cache()
    nominatim_progress['running'] = False
    nominatim_progress['complete'] = True
    print(f"Nominatim geocoding complete! {len(nominatim_cache):,} addresses cached.")

def geocode_census_original(building_addr, city, state_abbr, zip5):
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
    except Exception:
        pass
    return None

def geocode_census(cid, building_addr, city, state_abbr, zip5):
    key = f"{building_addr},{city},co,{zip5}".lower().replace('  ', ' ')
    gc = states[cid]['geocache']
    if key in gc and gc[key] is not None:
        return gc[key], key
    result = geocode_census_original(building_addr, city, state_abbr, zip5)
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
    # Normalize Arapahoe column names (spaces) to Denver format (underscores)
    for old_n, new_n in [('RES ADDRESS','RES_ADDRESS'),('RES CITY','RES_CITY'),('RES STATE','RES_STATE'),('RES ZIP','RES_ZIP'),('VOTE METHOD','VOTE_METHOD')]:
        if old_n in col and new_n not in col:
            col[new_n] = col[old_n]
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
        time.sleep(0.05)
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
        logo_ext = 'svg' if cfg['logo'] == 'phil' else 'png'
        logo_url = f'/static/logo-{cfg["logo"]}.{logo_ext}' if cfg['logo'] else ''
        has_van = len(van_supporters.get(cid, {})) > 0
        return jsonify({
            'campaignName': cfg['name'],
            'logoUrl': logo_url,
            'accentColor': cfg['color'],
            'prefix': url_prefix,
            'theme': cfg.get('theme', 'dark'),
            'publicPassword': cfg.get('public_password', ''),
            'hideStats': cfg.get('hide_stats', False),
            'mapCenter': cfg.get('map_center', [39.7392, -104.9903]),
            'mapZoom': cfg.get('map_zoom', 13),
            'county': cfg.get('county', 'Denver'),
            'tagline': cfg.get('tagline', ''),
            'showPartyFilter': cfg.get('show_party_filter', True),
            'showRepFilter': cfg.get('show_rep_filter', False),
            'showCandidateFilter': cfg.get('show_candidate_filter', False) and has_van,
            'allVotersStats': cfg.get('all_voters_stats', False),
            'candidateName': cfg['name'],
            'vanCount': len(van_supporters.get(cid, {})),
        })

    @app.route(f'{url_prefix}/api/status', endpoint=f'status_{cid}')
    def api_status():
        st = states[cid]
        cfg = CAMPAIGNS[cid]
        if cfg.get('all_voters_stats', False):
            # Show all voters (neutral public page) - st['voters'] is already not-returned list
            total = st['total']
            pending = len(st['voters'])
            returned = total - pending
            rate = round(returned / total * 100) if total > 0 else 0
        else:
            # Show DEM+UAF only
            pending = len([v for v in st['voters'] if v.get('party') in ('DEM','UAF')])
            total = st['total'] - sum(1 for v in st['voters'] if v.get('party') == 'REP')
            returned = total - pending
            rate = round(returned / total * 100) if total > 0 else 0
        return jsonify({
            'total': total, 'pending': pending,
            'returned': returned, 'returnRate': rate,
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
        party_arg = request.args.get('party','DEM,UAF')
        filter_by_party = (party_arg != 'ALL')
        party_set = set(party_arg.split(',')) if filter_by_party else set()
        access_set = set(request.args.get('access','accessible,inaccessible').split(','))
        candidate_only = request.args.get('candidate','') == '1'
        limit = min(int(request.args.get('limit', 30)), 100)
        # Enforce server-side: if party filter disabled in admin, show no one unless candidate filter active
        cfg = CAMPAIGNS[cid]
        if not cfg.get('show_party_filter', True) and not candidate_only:
            # Party filter off AND no candidate filter = show nothing
            return jsonify({'results': []})
        if not cfg.get('show_candidate_filter', False):
            # Candidate filter off in admin = never filter by candidate
            candidate_only = False
        # Build VAN lookup: geocodeKey -> {normalized_name -> vanid}
        van_key_names = {}  # geocodeKey -> set of normalized names
        van_geocoded = {}
        if candidate_only and van_supporters.get(cid):
            geocache = st['geocache']
            for vid, vsup in van_supporters[cid].items():
                k = vsup['geocodeKey']
                if k not in van_key_names:
                    van_key_names[k] = {}
                # Normalize name for matching
                norm = vsup['name'].upper().strip()
                van_key_names[k][norm] = vid
            van_keys = set(van_key_names.keys())
            # Missing supporters are geocoded in background at startup -- skip here
        buildings = {}
        # Build vanid lookup: geocodeKey -> vanid for fast annotation
        van_geocode_to_id = {}
        if van_supporters.get(cid):
            for vid, vsup in van_supporters[cid].items():
                van_geocode_to_id[vsup['geocodeKey']] = vid
        for v in st['voters']:
            # Party filter: skip if party filtering enabled and party not in set
            if filter_by_party and not candidate_only and v['party'] not in party_set: continue
            # Candidate filter: if active, only include VAN supporters
            if candidate_only and van_keys and v['geocodeKey'] not in van_keys: continue
            k = v['geocodeKey']
            if k not in buildings:
                coords = st['geocache'].get(k)
                if not coords: continue
                buildings[k] = {
                    'buildingAddress': v['buildingAddress'],
                    'city': v['city'], 'state': v['state'], 'zip': v['zip'],
                    'apt': v['apt'], 'lat': coords[0], 'lng': coords[1], 'voters': [],
                }
            # When candidate filter active, only include voters who are named VAN supporters
            if candidate_only and van_key_names:
                norm_name = v['name'].upper().strip()
                vanid = van_key_names.get(k, {}).get(norm_name)
                if vanid is None:
                    continue  # not a named VAN supporter - skip
            else:
                vanid = van_geocode_to_id.get(k)
            buildings[k]['voters'].append({
                'name': v['name'], 'unit': v['unit'],
                'party': v['party'], 'yob': v.get('yob'),
                'vanid': vanid,
            })
        # When candidate_only: also add VAN supporters not found in CE-068
        if candidate_only and van_supporters.get(cid):
            geocache = st['geocache']
            for vid, vsup in van_supporters[cid].items():
                k = vsup['geocodeKey']
                if k not in buildings:
                    coords = geocache.get(k)
                    if coords:
                        buildings[k] = {
                            'buildingAddress': vsup['address'],
                            'city': vsup['city'], 'state': vsup.get('state','CO'), 'zip': vsup['zip'],
                            'apt': False, 'lat': coords[0], 'lng': coords[1], 'voters': [{
                                'name': vsup['name'], 'unit': '',
                                'party': 'UAF', 'yob': None,
                                'vanid': vid,
                            }],
                        }
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

    @app.route(f'{url_prefix}/api/lookup', endpoint=f'lookup_{cid}')
    def lookup():
        addr = request.args.get('address','').strip().upper()
        if not addr or not voters_data:
            return jsonify({'found': False, 'results': []})
        import re as _re
        addr_norm = _re.sub(r'[.,#]', '', addr).strip()
        matches = {}
        for v in voters_data:
            baddr = _re.sub(r'[.,#]', '', v.get('buildingAddress','').upper()).strip()
            if addr_norm in baddr or baddr.startswith(addr_norm.split()[0] if addr_norm else ''):
                if addr_norm not in baddr: continue
                key = v['geocodeKey']
                if key not in matches:
                    matches[key] = {'address': v.get('buildingAddress',''), 'voters': []}
                matches[key]['voters'].append({'name': v.get('name',''), 'party': v.get('party',''), 'returned': v.get('returned', False)})
        return jsonify({'found': bool(matches), 'results': list(matches.values())[:5]})

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

    @app.route(f'{url_prefix}/admin-d7x9k', methods=['GET','POST'], endpoint=f'admin_{cid}')
    def admin():
        if request.method == 'GET':
            return send_from_directory('static', 'admin.html')
        ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
        if _check_rate_limit(ip):
            return jsonify({'error': 'Too many failed attempts. Try again in 15 minutes.'}), 429
        password = request.form.get('password','')
        if password != CAMPAIGNS[cid]['password']:
            _record_failed(ip)
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
        save_settings(cid)  # also persist in unified settings file
        return jsonify({'success': True, 'theme': theme})

    @app.route(f'{url_prefix}/api/verify-admin-password', methods=['POST'], endpoint=f'verifyadmin_{cid}')
    def verify_admin_password():
        pw = (request.json or {}).get('password', '')
        if pw == CAMPAIGNS[cid]['password']:
            return jsonify({'success': True})
        return jsonify({'success': False}), 401

    @app.route(f'{url_prefix}/api/verify-public-password', methods=['POST'], endpoint=f'verifypw_{cid}')
    def verify_public_password():
        pw = (request.json or {}).get('password', '')
        expected = CAMPAIGNS[cid].get('public_password', '')
        if not expected:
            return jsonify({'success': True, 'required': False})
        if pw == expected:
            return jsonify({'success': True, 'required': True})
        return jsonify({'success': False, 'required': True, 'error': 'Incorrect password'}), 401

    @app.route(f'{url_prefix}/api/save-settings', methods=['POST'], endpoint=f'savesettings_{cid}')
    def save_settings_route():
        password = (request.json or {}).get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        data = request.json or {}
        cfg = CAMPAIGNS[cid]
        if 'public_password' in data:
            cfg['public_password'] = data['public_password']
        if 'show_party_filter' in data:
            cfg['show_party_filter'] = bool(data['show_party_filter'])
        if 'show_candidate_filter' in data:
            cfg['show_candidate_filter'] = bool(data['show_candidate_filter'])
        save_settings(cid)
        return jsonify({'success': True})

    @app.route(f'{url_prefix}/api/upload-van', methods=['POST'], endpoint=f'uploadvan_{cid}')
    def upload_van():
        password = request.form.get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        f = request.files['file']
        file_bytes = f.read()
        import io
        supporters = parse_van_xls(io.BytesIO(file_bytes), filename=f.filename)
        if not supporters:
            return jsonify({'error': 'Could not parse VAN file. Make sure it is the standard VAN export.'}), 400
        van_supporters[cid] = supporters
        save_van_supporters(cid)
        print(f"[{cid}] Loaded {len(supporters)} VAN supporters")
        return jsonify({'success': True, 'count': len(supporters)})

    @app.route(f'{url_prefix}/api/van-status', endpoint=f'vanstatus_{cid}')
    def van_status():
        password = request.args.get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        return jsonify({
            'count': len(van_supporters.get(cid, {})),
            'campaignName': CAMPAIGNS[cid]['name'],
        })

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

    @app.route(f'{url_prefix}/api/nominatim-status', endpoint=f'nomstatus_{cid}')
    def nominatim_status():
        p = nominatim_progress
        total = p['total'] or 1
        return jsonify({
            'running': p['running'],
            'complete': p['complete'],
            'done': p['done'],
            'total': p['total'],
            'pct': round(p['done'] / total * 100),
            'cacheSize': len(nominatim_cache),
        })

    @app.route(f'{url_prefix}/api/nominatim-clear', methods=['POST'], endpoint=f'nomclear_{cid}')
    def nominatim_clear():
        password = request.form.get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        global nominatim_cache
        nominatim_cache.clear()
        if os.path.exists(NOMINATIM_CACHE_FILE):
            os.remove(NOMINATIM_CACHE_FILE)
        nominatim_progress['done'] = 0
        nominatim_progress['total'] = 0
        nominatim_progress['running'] = False
        nominatim_progress['complete'] = False
        # Start rebuild in background using current voter data
        voters = list(SHARED_VOTERS.values()) if SHARED_VOTERS else []
        if voters:
            import threading
            t = threading.Thread(target=run_nominatim_build, args=(voters,), daemon=True)
            t.start()
        return jsonify({'ok': True, 'message': f'Cache cleared. Rebuilding {len(voters)} addresses.'})

    @app.route(f'{url_prefix}/api/start-nominatim', methods=['POST'], endpoint=f'startnominatim_{cid}')
    def start_nominatim():
        password = (request.json or {}).get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        if nominatim_progress['running']:
            return jsonify({'message': 'Already running', 'done': nominatim_progress['done'], 'total': nominatim_progress['total']})
        voters = states['default']['voters']
        if not voters:
            return jsonify({'error': 'No voter data loaded'}), 400
        threading.Thread(target=run_nominatim_build, args=(voters,), daemon=True).start()
        return jsonify({'success': True, 'message': f'Started geocoding {len(voters):,} addresses with Nominatim'})

    @app.route(f'{url_prefix}/api/flip-to-nominatim', methods=['POST'], endpoint=f'flipnom_{cid}')
    def flip_to_nominatim():
        password = (request.json or {}).get('password','')
        if password != CAMPAIGNS[cid]['password']:
            return jsonify({'error': 'Invalid password'}), 401
        if not nominatim_progress['complete']:
            return jsonify({'error': 'Nominatim geocoding not complete yet'}), 400
        # Swap geocaches for all campaigns
        for c in states:
            states[c]['geocache'] = dict(nominatim_cache)
            try:
                with open(data_file(c, 'geocache.json'), 'w') as f:
                    json.dump(nominatim_cache, f)
            except Exception as e:
                print(f"[{c}] Error saving flipped geocache: {e}")
        return jsonify({'success': True, 'message': f'Flipped to Nominatim geocache ({len(nominatim_cache):,} addresses)'})

# Register routes for all campaigns
make_routes('default', 'default')
make_routes('melat', 'melat')
make_routes('phil', 'phil')
make_routes('denverdems', 'denverdems')
make_routes('arapahoe', 'arapahoe')

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

def settings_file(cid):
    # Store in campaign's own data_dir which is guaranteed to be created on startup
    return os.path.join(CAMPAIGNS[cid]['data_dir'], 'settings.json')

def van_file(cid):
    return os.path.join(BASE_DATA_DIR, f'van_supporters_{cid}.json')

# Per-campaign VAN supporters: {vanid: {name, address, city, state, zip}}
van_supporters = {cid: {} for cid in ['default','melat','phil','denverdems']}

def load_van_supporters(cid):
    vf = van_file(cid)
    if os.path.exists(vf):
        try:
            with open(vf) as f:
                van_supporters[cid] = json.load(f)
            print(f"[{cid}] Loaded {len(van_supporters[cid])} VAN supporters")
        except Exception as e:
            print(f"[{cid}] VAN load error: {e}")

def save_van_supporters(cid):
    try:
        with open(van_file(cid), 'w') as f:
            json.dump(van_supporters[cid], f)
    except Exception as e:
        print(f"[{cid}] VAN save error: {e}")

def load_settings(cid):
    sf = settings_file(cid)
    # Also check old location for migration
    old_sf = os.path.join(BASE_DATA_DIR, f'settings_{cid}.json')
    old_theme_f = os.path.join(BASE_DATA_DIR, f'theme_{cid}.json')

    # Try new location first
    if os.path.exists(sf):
        try:
            with open(sf) as f:
                data = json.load(f)
            cfg = CAMPAIGNS[cid]
            cfg['public_password'] = data.get('public_password', '')
            cfg['show_party_filter'] = data.get('show_party_filter', True)
            cfg['show_candidate_filter'] = data.get('show_candidate_filter', False)
            if 'theme' in data:
                cfg['theme'] = data['theme']
            print(f"[{cid}] Loaded settings from {sf}: {data}")
            return
        except Exception as e:
            print(f"[{cid}] Settings load ERROR: {e}")

    # Fall back to old settings file location
    if os.path.exists(old_sf):
        try:
            with open(old_sf) as f:
                data = json.load(f)
            cfg = CAMPAIGNS[cid]
            cfg['public_password'] = data.get('public_password', '')
            cfg['show_party_filter'] = data.get('show_party_filter', True)
            cfg['show_candidate_filter'] = data.get('show_candidate_filter', False)
            if 'theme' in data:
                cfg['theme'] = data['theme']
            print(f"[{cid}] Migrated settings from old location: {data}")
            save_settings(cid)  # save to new location immediately
            return
        except Exception as e:
            print(f"[{cid}] Old settings migration error: {e}")

    # Fall back to old theme file
    if os.path.exists(old_theme_f):
        try:
            with open(old_theme_f) as f:
                data = json.load(f)
            CAMPAIGNS[cid]['theme'] = data.get('theme', 'dark')
            print(f"[{cid}] Migrated theme from theme file: {CAMPAIGNS[cid]['theme']}")
            save_settings(cid)  # save to new location immediately
            return
        except Exception as e:
            print(f"[{cid}] Theme file migration error: {e}")

    print(f"[{cid}] No settings found anywhere — using defaults")

def save_settings(cid):
    cfg = CAMPAIGNS[cid]
    sf = settings_file(cid)
    try:
        os.makedirs(os.path.dirname(sf), exist_ok=True)
        with open(sf, 'w') as f:
            data = {
                'public_password': cfg.get('public_password', ''),
                'show_party_filter': cfg.get('show_party_filter', True),
                'show_candidate_filter': cfg.get('show_candidate_filter', False),
                'theme': cfg.get('theme', 'dark'),
            }
            json.dump(data, f)
        print(f"[{cid}] Settings saved to {sf}: {data}")
    except Exception as e:
        print(f"[{cid}] Settings save ERROR: {e}")
        import traceback; traceback.print_exc()

def parse_van_xls(file_stream, filename=''):
    """Parse VAN supporter export. Handles .xlsx, .xls (tab-sep UTF-16), and .csv."""
    supporters = {}
    try:
        raw = file_stream.read()
        is_xlsx = raw[:2] == b'PK'
        is_utf16 = raw[:2] in (b'\xff\xfe', b'\xfe\xff')
        if is_xlsx:
            import io as _io, openpyxl
            wb = openpyxl.load_workbook(_io.BytesIO(raw), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows: return supporters
            headers = [str(h).strip() if h is not None else '' for h in rows[0]]
            data_rows = rows[1:]
        elif is_utf16:
            text = raw.decode('utf-16')
            lines = text.strip().split('\n')
            headers = [h.strip().strip('\r') for h in lines[0].split('\t')]
            data_rows = [[c.strip('\r') for c in line.split('\t')] for line in lines[1:] if line.strip()]
        else:
            text = raw.decode('utf-8', errors='replace')
            sep = ',' if (',' in text.split('\n')[0] and '\t' not in text.split('\n')[0]) else '\t'
            lines = text.strip().split('\n')
            headers = [h.strip().strip('\r').strip('"') for h in lines[0].split(sep)]
            data_rows = [[c.strip('\r').strip('"') for c in line.split(sep)] for line in lines[1:] if line.strip()]
        col = {}
        for i, h in enumerate(headers):
            hl = str(h).lower().replace(' ', '').replace('_', '')
            col[hl] = i
        def get_col(row, *names):
            for n in names:
                n = n.lower().replace(' ','').replace('_','')
                if n in col and col[n] < len(row):
                    val = row[col[n]]
                    return str(val).strip().strip('=').strip('"') if val is not None else ''
            return ''
        for row in data_rows:
            vanid = get_col(row, 'VoterFileVANID', 'VANID', 'vanid')
            if not vanid or not str(vanid).strip().replace('.0','').isdigit(): continue
            vanid = str(vanid).strip().replace('.0','')
            addr = get_col(row, 'Address')
            city = get_col(row, 'City')
            state = get_col(row, 'State')
            zip5 = get_col(row, 'Zip5', 'Zip')
            zip5 = str(zip5).replace('.0','').strip()
            first = get_col(row, 'FirstName', 'First')
            last = get_col(row, 'LastName', 'Last')
            if not addr or not zip5: continue
            supporters[vanid] = {
                'vanid': vanid,
                'name': f"{first} {last}".strip(),
                'address': addr,
                'city': city,
                'state': state,
                'zip': zip5,
                'geocodeKey': f"{addr},{city},co,{zip5}".lower().replace('  ',' '),
            }
    except Exception as e:
        print(f"VAN parse error: {e}")
        import traceback; traceback.print_exc()
    return supporters

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
    load_settings(cid)   # loads theme, public_password, filter toggles
    load_van_supporters(cid)
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

# Other campaigns share the voter list but have their own settings/geocache
for cid in CAMPAIGNS:
    if cid == 'default':
        continue
    try:
        os.makedirs(CAMPAIGNS[cid]['data_dir'], exist_ok=True)
    except: pass
    load_settings(cid)        # theme, public_password, filter toggles
    load_van_supporters(cid)  # VAN supporter list
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
    # Auto-resume Nominatim in its own thread immediately — don't wait for auto_geocode
    if len(nominatim_cache) > 0 and not nominatim_progress['complete']:
        voters = states['default']['voters']
        if voters:
            print(f"Auto-resuming Nominatim build from {len(nominatim_cache):,} cached entries")
            threading.Thread(target=run_nominatim_build, args=(voters,), daemon=True).start()
    for cid in CAMPAIGNS:
        auto_geocode(cid)

# Load nominatim cache in main thread so it's immediately available
load_nominatim_cache()
threading.Thread(target=startup_geocoding, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
