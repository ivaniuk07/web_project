"""Isolated Whisper process. JSON on stdout is consumed by the parent."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('audio')
    parser.add_argument('model')
    args = parser.parse_args()
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device='cpu', compute_type='int8')
    segments, _ = model.transcribe(args.audio, beam_size=1)
    print(json.dumps([
        {'start': item.start, 'end': item.end, 'text': item.text.strip()}
        for item in segments
    ], ensure_ascii=False))


if __name__ == '__main__':
    main()
