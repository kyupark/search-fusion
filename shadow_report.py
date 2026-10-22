#!/usr/bin/env python3
"""Offline aggregate only; optional read-only later tool-call URL matching."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit

try:
    from .paths import hermes_home
except ImportError:
    from paths import hermes_home


def canonical(url):
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.hostname or '', p.path, '', ''))
    except ValueError:
        return ''


def report(paths, db=None):
    records, malformed = [], 0
    for path in paths:
        if not Path(path).exists():
            continue
        with Path(path).open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or not isinstance(record.get('fusion_only_urls'), list):
                        raise ValueError('schema')
                    records.append(record)
                except (ValueError, TypeError):
                    malformed += 1
    total = sum(len(r.get('fusion_urls', [])) for r in records)
    unique = sum(len(r['fusion_only_urls']) for r in records)
    out = {'runs': len(records), 'malformed_lines': malformed,
           'runs_with_fusion_only_urls': sum(bool(r['fusion_only_urls']) for r in records),
           'fusion_url_observations': total, 'fusion_only_url_observations': unique,
           'fusion_only_url_rate': unique / total if total else 0,
           'later_tool_calls': 'not obtainable: no readable state database requested',
           'limitation': 'URL appearance is not usefulness; query strings/fragments are removed. Same-session later calls only, not citations or tool result bodies.'}
    if db:
        try:
            with sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True) as conn:
                cols = {r[1] for r in conn.execute('PRAGMA table_info(messages)')}
                if not {'session_id', 'timestamp', 'tool_calls'} <= cols:
                    raise ValueError('required columns unavailable')
                matched = eligible = unavailable = 0
                for r in records:
                    session, ts = r.get('session_id'), r.get('ts')
                    if not session or not isinstance(ts, (int, float)):
                        unavailable += 1
                        continue
                    if not conn.execute('SELECT 1 FROM messages WHERE session_id=? LIMIT 1', (session,)).fetchone():
                        unavailable += 1
                        continue
                    eligible += 1
                    urls = set()
                    for (calls,) in conn.execute('SELECT tool_calls FROM messages WHERE session_id=? AND timestamp>? AND tool_calls IS NOT NULL', (session, ts)):
                        # Match URL tokens, not substring prefixes or result bodies.
                        urls.update(canonical(u.rstrip('.,);')) for u in re.findall(r'https?://[^\s"<>\\]+', calls or ''))
                    matched += sum(canonical(u) in urls for u in r['fusion_only_urls'])
                out['later_tool_calls'] = {'obtainable_runs': eligible, 'unavailable_runs': unavailable,
                                           'fusion_only_urls_seen_later': matched}
        except (sqlite3.Error, ValueError, OSError):
            out['later_tool_calls'] = 'not obtainable: database unavailable or unsupported schema'
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    default = hermes_home() / 'state/search-fusion/shadow.jsonl'
    parser.add_argument('logs', nargs='*', type=Path, default=[default.with_name('shadow.jsonl.1'), default])
    parser.add_argument('--db', type=Path, default=hermes_home() / 'state.db')
    args = parser.parse_args()
    print(json.dumps(report(args.logs, args.db), indent=2))
