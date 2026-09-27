"""Measure fact preservation through real three-level summaries and restart/deletion."""
import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from greatsage import __version__
from greatsage.evaluation import normalized, compare_reports
from greatsage.runtime import Runtime
from greatsage.memory import MemoryStore
from greatsage.settings import SettingsStore

# Project-authored fictional records. Stable IDs and literal fact values make
# loss visible without having a second model grade the first model's output.
FACTS = [{'project': f'GS-{100+i}', 'value': f'Cobalt-{201+i}'} for i in range(32)]


async def run(args):
    digest = hashlib.sha256(json.dumps(FACTS, sort_keys=True).encode()).hexdigest()
    result = {'schema_version': 1, 'suite': 'long_memory', 'version': __version__, 'corpus_sha256': digest,
              'provenance': '32 project-authored fictional sessions, 2 records each; literal code retention is a narrow fact-preservation metric.',
              'configuration': {'mode': 'live' if args.live else 'plan'}, 'metrics': {}}
    if not args.live:
        return result
    temporary_root = ROOT / '.runtime/eval'; temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='long-memory-', dir=temporary_root) as directory:
        runtime = Runtime(Path(directory))
        try:
            runtime.settings.update(SettingsStore(args.settings_dir).raw())
            runtime.settings.update({'embedding': {'enabled': False}, 'audit_content': False, 'voice_enabled': False})
            result['configuration'].update(provider=runtime.settings.raw()['llm']['provider'], model=runtime.settings.raw()['llm']['model'])
            originals = []
            for fact in FACTS:
                session = runtime.memory.new_session()
                originals.append(runtime.memory.add_message('user', f"项目 {fact['project']} 的校验口令是 {fact['value']}。", session_id=session))
                runtime.memory.add_message('user', '以上项目编号和口令是虚构评测资料，需要准确保留拼写。', session_id=session)
            for _ in range(64):
                if not runtime.memory.compression_batch(runtime.session_id) or not runtime.background.ready('compression'): break
                before = len(runtime.memory.summaries(1000))
                await runtime._compress()
                if len(runtime.memory.summaries(1000)) == before: break
            summaries = runtime.memory.summaries(1000)
            top = [s for s in summaries if not any(d['owner_type']=='summary' for d in runtime.memory.record_details(s['id'])['dependents'])]
            normalized_text = normalized(' '.join(row['text'] for row in top))
            preserved = [fact['project'] for fact in FACTS if normalized(fact['project']) in normalized_text and normalized(fact['value']) in normalized_text]
            # Identity co-occurrence is reported; this does not prove pairwise binding.
            result['metrics'] = {'sessions': len(FACTS), 'summaries': len(summaries), 'maximum_level': max((r['level'] for r in summaries), default=0),
                                  'literal_fact_survival': len(preserved)/len(FACTS), 'background_failures': runtime.background.state('compression')['attempts']}
            result['preserved_projects'] = preserved
            result['usage'] = [e['data'].get('usage') for e in runtime.memory.events(1000) if e['kind'] == 'usage']
            top_ids = {s['id'] for s in top}
        finally:
            await runtime.close()
        store = MemoryStore(Path(directory))
        try:
            result['metrics']['restart_preserved_top_summaries'] = int(all(store.record(id) for id in top_ids))
            descendants = {s['id'] for s in summaries if originals[0]['id'] in store._leaf_sources(s['source_ids'])}
            store.delete_message(originals[0]['id'])
            result['metrics']['source_delete_invalidated_descendants'] = int(bool(descendants) and all(store.record(id) is None for id in descendants))
        finally:
            store.close()
    return result


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--settings-dir', type=Path, default=ROOT/'.runtime')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    if args.compare: report['comparison'] = compare_reports(json.loads(args.compare.read_text(encoding='utf-8')), report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'metrics': report['metrics'], 'output': str(args.output)}, ensure_ascii=False))
