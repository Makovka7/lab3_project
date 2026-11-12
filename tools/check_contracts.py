# Проверка OpenAPI-контрактов: python tools/check_contracts.py api
import yaml, re, sys, pathlib, glob
API_DIR = sys.argv[1] if len(sys.argv) > 1 else 'api'
errors = []; warns = []
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
DT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$')
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def resolve(doc, ref):
    assert ref.startswith('#/'), ref
    n = doc
    for part in ref[2:].split('/'):
        n = n[part]
    return n


def validate(doc, schema, val, path, errs):
    if '$ref' in schema:
        validate(doc, resolve(doc, schema['$ref']), val, path, errs)
        rest = {k: v for k, v in schema.items() if k != '$ref'}
        if rest:
            validate(doc, rest, val, path, errs)
        return
    for sub in schema.get('allOf', []):
        validate(doc, sub, val, path, errs)
    if 'const' in schema and val != schema['const']:
        errs.append(f'{path}: const {schema["const"]} != {val}')
    if 'enum' in schema and val not in schema['enum']:
        errs.append(f'{path}: {val!r} not in enum {schema["enum"]}')
    t = schema.get('type')
    if t:
        ts = t if isinstance(t, list) else [t]
        ok = False
        for x in ts:
            if x == 'string' and isinstance(val, str): ok = True
            elif x == 'integer' and isinstance(val, int) and not isinstance(val, bool): ok = True
            elif x == 'number' and isinstance(val, (int, float)) and not isinstance(val, bool): ok = True
            elif x == 'boolean' and isinstance(val, bool): ok = True
            elif x == 'object' and isinstance(val, dict): ok = True
            elif x == 'array' and isinstance(val, list): ok = True
            elif x == 'null' and val is None: ok = True
        if not ok:
            errs.append(f'{path}: type {t} vs {type(val).__name__} ({val!r})')
            return
    if isinstance(val, str):
        if 'pattern' in schema and not re.search(schema['pattern'], val):
            errs.append(f'{path}: pattern {schema["pattern"]} fails for {val!r}')
        f = schema.get('format')
        if f == 'uuid' and not UUID.match(val): errs.append(f'{path}: bad uuid {val}')
        if f == 'date-time' and not DT.match(val): errs.append(f'{path}: bad date-time {val}')
        if f == 'date' and not DATE.match(val): errs.append(f'{path}: bad date {val}')
        if 'minLength' in schema and len(val) < schema['minLength']: errs.append(f'{path}: too short')
        if 'maxLength' in schema and len(val) > schema['maxLength']: errs.append(f'{path}: too long')
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if 'minimum' in schema and val < schema['minimum']: errs.append(f'{path}: < minimum')
        if 'exclusiveMinimum' in schema and val <= schema['exclusiveMinimum']: errs.append(f'{path}: <= exclusiveMinimum')
    if isinstance(val, dict):
        for r in schema.get('required', []):
            if r not in val: errs.append(f'{path}: missing required {r}')
        for k, v in val.items():
            if k in schema.get('properties', {}):
                validate(doc, schema['properties'][k], v, f'{path}.{k}', errs)
    if isinstance(val, list):
        if 'minItems' in schema and len(val) < schema['minItems']: errs.append(f'{path}: minItems')
        if 'maxItems' in schema and len(val) > schema['maxItems']: errs.append(f'{path}: maxItems')
        if 'items' in schema:
            for i, v in enumerate(val):
                validate(doc, schema['items'], v, f'{path}[{i}]', errs)


def refs(node, acc):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == '$ref' and isinstance(v, str): acc.append(v)
            else: refs(v, acc)
    elif isinstance(node, list):
        for v in node: refs(v, acc)


opids = {}
allsvc = {}
for f in sorted(glob.glob(API_DIR + '/*.yaml')):
    name = pathlib.Path(f).stem
    try:
        doc = yaml.safe_load(open(f, encoding='utf-8'))
    except Exception as e:
        errors.append(f'{name}: YAML parse error {e}'); continue
    allsvc[name] = doc
    assert doc['openapi'].startswith('3.1')
    for k in ['info', 'paths', 'components', 'tags', 'servers']:
        if k not in doc: errors.append(f'{name}: no {k}')
    rs = []; refs(doc, rs)
    for r in rs:
        try: resolve(doc, r)
        except Exception: errors.append(f'{name}: unresolved $ref {r}')
    used = set(rs)
    for sect, items in doc['components'].items():
        if sect == 'securitySchemes': continue
        for k in items:
            if f'#/components/{sect}/{k}' not in used: warns.append(f'{name}: unused components/{sect}/{k}')
    tagset = {t['name'] for t in doc['tags']}
    nops = 0
    for path, item in doc['paths'].items():
        pparams = item.get('parameters', [])
        for m, op in item.items():
            if m == 'parameters': continue
            nops += 1
            oid = op.get('operationId')
            if not oid: errors.append(f'{name} {m} {path}: no operationId')
            elif oid in opids: errors.append(f'dup operationId {oid}')
            else: opids[oid] = name
            if not op.get('summary'): errors.append(f'{name} {m} {path}: no summary')
            if not op.get('tags') or not set(op['tags']) <= tagset: errors.append(f'{name} {m} {path}: bad tags')
            codes = [str(c) for c in op['responses']]
            if not any(c.startswith('2') for c in codes): errors.append(f'{name} {m} {path}: no 2xx')
            if not any(c.startswith('4') for c in codes): errors.append(f'{name} {m} {path}: no 4xx')
            if path.startswith('/internal') and 'internal' not in op['tags']: errors.append(f'{name} {path}: internal tag missing')
            if path.startswith('/api') and 'internal' in op['tags']: errors.append(f'{name} {path}: internal tag on public path')
            ph = re.findall(r'\{(\w+)\}', path)
            declared = []
            for p in pparams + op.get('parameters', []):
                if '$ref' in p: p = resolve(doc, p['$ref'])
                if p['in'] == 'path': declared.append(p['name'])
            for h in ph:
                if h not in declared: errors.append(f'{name} {m} {path}: path param {h} not declared')
            rb = op.get('requestBody')
            if rb:
                for mt, c in rb['content'].items():
                    if 'example' in c:
                        e = []; validate(doc, c['schema'], c['example'], f'{name} {oid} request', e); errors += e
                    elif rb.get('required'):
                        warns.append(f'{name} {oid}: no request example')
            for code, resp in op['responses'].items():
                if '$ref' in resp: continue
                for mt, c in resp.get('content', {}).items():
                    if 'example' in c:
                        e = []; validate(doc, c['schema'], c['example'], f'{name} {oid} {code}', e); errors += e
    for sn, sc in doc['components']['schemas'].items():
        exs = []
        def find(node, pth):
            if isinstance(node, dict):
                if 'example' in node and ('type' in node or 'allOf' in node):
                    exs.append((pth, node))
                for k, v in node.items(): find(v, pth + '/' + k)
            elif isinstance(node, list):
                for i, v in enumerate(node): find(v, pth + f'/{i}')
        find(sc, sn)
        for pth, node in exs:
            e = []
            s2 = {k: v for k, v in node.items() if k != 'example'}
            validate(doc, s2, node['example'], f'{name} schema {pth}', e); errors += e
    for m in doc.get('x-async-messages', []):
        sc = m['schema']['$ref']
        try: node = resolve(doc, sc)
        except Exception:
            errors.append(f'{name}: async ref {sc}'); continue
        for k in ['eventType', 'topic', 'key', 'publisher', 'subscribers']:
            if k not in m: errors.append(f'{name}: async msg missing {k}')
        consts = [sub['properties']['eventType']['const'] for sub in node['allOf'] if isinstance(sub, dict) and 'properties' in sub and 'eventType' in sub['properties']]
        if consts != [m['eventType']]: errors.append(f'{name}: eventType mismatch {m["eventType"]} vs {consts}')
    print(f'{name}: {nops} operations, {len(doc["components"]["schemas"])} schemas, {len(doc.get("x-async-messages", []))} published messages')

for name, doc in allsvc.items():
    for m in doc.get('x-consumed-messages', []):
        fn, _, ptr = m['schemaRef'].partition('#')
        other = allsvc.get(fn.replace('.yaml', ''))
        if not other:
            errors.append(f'{name}: consumed ref file {fn}'); continue
        try: resolve(other, '#' + ptr)
        except Exception: errors.append(f'{name}: consumed ref unresolved {m["schemaRef"]}')
        pub = other.get('x-async-messages', [])
        types = {x['eventType'] for x in pub}
        for et in [x.strip() for x in m['eventType'].split(',')]:
            if et not in types:
                errors.append(f'{name}: consumes {et} not published by {fn}')
            for x in pub:
                if x['eventType'] == et and name not in x['subscribers']:
                    errors.append(f'{name}: consumes {et} but publisher does not list it as subscriber')
for name, doc in allsvc.items():
    for x in doc.get('x-async-messages', []):
        for sub in x['subscribers']:
            have = set()
            for m in allsvc[sub].get('x-consumed-messages', []):
                for et in m['eventType'].split(','): have.add(et.strip())
            if x['eventType'] not in have:
                errors.append(f'{sub}: listed as subscriber of {x["eventType"]} but does not declare consumption')
print('\nERRORS:', len(errors)); [print(' -', e) for e in errors]
print('WARNINGS:', len(warns)); [print(' -', w) for w in warns]

sys.exit(1 if errors else 0)
