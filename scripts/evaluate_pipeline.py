"""Repeat the paced speech pipeline with comparable latency and resource reports."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_pipeline import make_parser, run, FIXTURE_TEXT
from greatsage import __version__
from greatsage.evaluation import distribution, compare_reports


async def evaluate(args):
    runs = [await run(args) for _ in range(args.repeats)]
    fixture_id = args.fixture_id or 'synthetic-windows-tts'
    corpus = (ROOT/'evals/speech.json').read_text(encoding='utf-8') if args.fixture_id else FIXTURE_TEXT
    corpus = json.dumps(json.loads(corpus), ensure_ascii=False, sort_keys=True, separators=(',', ':')) if args.fixture_id else corpus
    report = {'schema_version': 1, 'suite': 'pipeline', 'version': __version__,
              'corpus_sha256': hashlib.sha256((fixture_id + corpus).encode()).hexdigest(),
              'configuration': {**runs[0]['configuration'], 'parameters': runs[0]['parameters'], 'fixture_id': fixture_id},
              'scope': runs[0]['scope'], 'cases': runs,
              'metrics': {'cases': len(runs), 'failures': sum(r['status'] != 'ok' for r in runs),
                          'asr_cancelled_requests': sum(r['metrics'].get('asr_cancelled_requests', 0) for r in runs)}}
    report['latencies'] = {}
    for name in ['endpoint_detection_ms', 'speech_end_to_transcript_ms', 'speech_end_to_first_text_ms', 'speech_end_to_audio_ready_ms']:
        summary = distribution(r['metrics'].get(name) for r in runs if r['status'] == 'ok')
        report['latencies'][name] = summary
        report['metrics'][name + '_p50'] = summary['p50']
        report['metrics'][name + '_p95'] = summary['p95']
    report['metrics']['peak_python_rss_mib'] = max(r.get('resources', {}).get('peak_python_rss_mib', 0) for r in runs)
    report['metrics']['mean_cer'] = sum(r.get('recognition', {}).get('cer', 1) for r in runs)/len(runs)
    if args.compare:
        report['comparison'] = compare_reports(json.loads(args.compare.read_text(encoding='utf-8')), report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'metrics': report['metrics'], 'report': str(args.report)}, ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = make_parser()
    parser.add_argument('--repeats', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--compare', type=Path)
    asyncio.run(evaluate(parser.parse_args()))
