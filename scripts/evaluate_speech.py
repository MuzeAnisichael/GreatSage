"""Licensed human speech ASR/echo benchmark; no recording or speaker playback."""
import argparse
import asyncio
import hashlib
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from greatsage import __version__
from greatsage.echo import EchoGuard
from greatsage.evaluation import recognition, distribution, compare_reports
from greatsage.providers import Providers
from greatsage.settings import SettingsStore


def fixtures():
    manifest = json.loads((ROOT / 'evals/speech.json').read_text(encoding='utf-8'))
    data = []
    for row in manifest['cases']:
        path = ROOT / 'evals' / row['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], 'Fixture hash changed'
        with wave.open(str(path)) as stream:
            assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (1, 2, 16000)
            data.append((row, stream.readframes(stream.getnframes())))
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return manifest, digest, data


def echo_report(data):
    reference = np.frombuffer(data[0][1], dtype='<i2').astype(float)
    other = np.resize(np.frombuffer(data[1][1], dtype='<i2').astype(float), reference.shape)
    records = []
    for packet in (20, 100):
        for scenario in ('scaled_echo', 'double_talk'):
            guard = EchoGuard(); guard.set_reference(data[0][1], 100)
            frame_size = packet * 16
            suppressed = eligible = false_suppressed = user_frames = 0
            costs = []
            for offset in range(0, len(reference), frame_size):
                ref = reference[offset:offset + frame_size] * .4
                user = other[offset:offset + frame_size] * .5 if scenario == 'double_talk' else np.zeros(len(ref))
                raw = np.clip(ref + user, -32768, 32767).astype('<i2').tobytes()
                start = time.perf_counter()
                output = guard.filter(raw, 100 + (offset + len(ref)) / 16000 + .18)
                costs.append((time.perf_counter()-start)*1000)
                # Score 20 ms decisions even if delivery was a 100 ms batch.
                for pos in range(0, len(ref), 320):
                    wanted = ref[pos:pos+320]; human = user[pos:pos+320]
                    is_voice = float(np.sqrt(np.mean(wanted**2))) > 100
                    is_user = float(np.sqrt(np.mean(human**2))) > 100
                    muted = not any(output[pos*2:(pos+len(wanted))*2])
                    eligible += int(is_voice); suppressed += int(is_voice and muted)
                    user_frames += int(is_user); false_suppressed += int(is_user and muted)
            records.append({'scenario': scenario, 'packet_ms': packet, 'voice_frames': eligible,
                            'echo_voice_frames_muted': suppressed, 'user_voice_frames': user_frames,
                            'user_voice_frames_muted': false_suppressed, 'filter_ms': distribution(costs)})
    return {'scope': 'Human source clips with digitally simulated 0.4 gain, 180 ms echo delay and independent 0.5-gain overlapping read speech. Not a room recording or full AEC.', 'cases': records}


async def run(args):
    manifest, digest, data = fixtures()
    report = {'schema_version': 1, 'suite': 'speech', 'version': __version__, 'corpus_sha256': digest,
              'configuration': {'mode': 'live' if args.live else 'offline', 'repeats': args.repeats, 'variant': args.variant},
              'provenance': manifest['provenance'], 'echo': echo_report(data), 'cases': [], 'metrics': {}}
    if not args.live:
        return report
    config = SettingsStore(args.settings_dir).provider('asr')
    report['configuration'].update(provider=config['provider'], model=config['model'])
    providers = Providers()
    try:
        for repetition in range(args.repeats):
            for fixture, pcm in data:
                if args.variant == 'quiet':
                    pcm = (np.frombuffer(pcm, dtype='<i2').astype(float) * .15).astype('<i2').tobytes()
                start = time.monotonic()
                row = {'id': fixture['id'], 'repeat': repetition, 'first_request_in_process': not report['cases']}
                try:
                    result = await providers.transcribe(config, pcm)
                    row.update(status='ok', transcript=result['text'], usage=result.get('usage', {}), **recognition(fixture['text'], result['text']))
                except Exception as error:
                    row.update(status='failed', error=type(error).__name__)
                row['latency_ms'] = (time.monotonic()-start)*1000
                report['cases'].append(row)
    finally:
        await providers.close()
    good = [r for r in report['cases'] if r['status'] == 'ok']
    characters = sum(r['reference_characters'] for r in good)
    report['metrics'] = {'cases': len(report['cases']), 'failures': len(report['cases'])-len(good), 'cancellations': 0,
                         'character_error_rate': sum(r['cer']*r['reference_characters'] for r in good)/max(1, characters)}
    report['latency_ms'] = distribution(r['latency_ms'] for r in good)
    report['notes'] = 'CER ignores punctuation/spacing only. Chinese WER is unsegmented and is not the primary metric. Latency is ASR request-to-completion, not spoken-turn-to-playback. Errors are excluded from CER and shown separately.'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--settings-dir', type=Path, default=ROOT / '.runtime')
    parser.add_argument('--repeats', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--variant', choices=['original', 'quiet'], default='original')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    if args.compare:
        report['comparison'] = compare_reports(json.loads(args.compare.read_text(encoding='utf-8')), report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'metrics': report['metrics'], 'latency_ms': report.get('latency_ms'), 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
