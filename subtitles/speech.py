"""Isolated Whisper process. JSON on stdout is consumed by the parent."""
import argparse
import json


def subtitle_segments(segments):
    return [
        {'start': item.start, 'end': item.end, 'text': text}
        for item in segments
        if (text := item.text.strip())
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('audio')
    parser.add_argument('model')
    parser.add_argument('--language')
    args = parser.parse_args()
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device='cpu', compute_type='int8')
    segments, _ = model.transcribe(args.audio, language=args.language, beam_size=1)
    captions = subtitle_segments(segments)
    if not captions:
        # Filter silent regions before retrying with a broader search.
        segments, _ = model.transcribe(
            args.audio, language=args.language, beam_size=5, vad_filter=True,
            no_speech_threshold=0.8,
        )
        captions = subtitle_segments(segments)
    print(json.dumps(captions, ensure_ascii=False))


if __name__ == '__main__':
    main()
