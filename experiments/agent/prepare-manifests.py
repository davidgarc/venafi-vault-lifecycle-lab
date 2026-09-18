#!/usr/bin/env python3
"""Normalize kubectl's concatenated JSON output and set initial replicas."""
import json
import sys
raw = sys.stdin.read().strip()
decoder = json.JSONDecoder()
items = []
while raw:
    value, end = decoder.raw_decode(raw)
    items.extend(value['items'] if value.get('kind') == 'List' else [value])
    raw = raw[end:].lstrip()
for item in items:
    if item['kind'] == 'Deployment':
        item['spec']['replicas'] = int(sys.argv[1])
print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}))
