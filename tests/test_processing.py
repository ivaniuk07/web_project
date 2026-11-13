import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from subtitles.processing import ProcessingError, Segment, generate_subtitles, render_srt


class ProcessingTests(SimpleTestCase):
    @patch('faster_whisper.WhisperModel')
    def test_whisper_retries_when_no_subtitle_text_is_returned(self, model_class):
        from subtitles.speech import main

        model = model_class.return_value
        for language in (None, 'uk'):
            with self.subTest(language=language):
                model.reset_mock()
                model.transcribe.side_effect = [
                    (iter([SimpleNamespace(start=0, end=1, text=' ')]), None),
                    (iter([SimpleNamespace(start=0, end=1, text='Привіт!')]), None),
                ]
                args = ['speech', 'audio.wav', 'tiny']
                if language:
                    args.extend(['--language', language])
                with patch('sys.argv', args), patch('sys.stdout', new_callable=io.StringIO) as output:
                    main()
                self.assertEqual(json.loads(output.getvalue()), [{'start': 0, 'end': 1, 'text': 'Привіт!'}])
                self.assertEqual(model.transcribe.call_count, 2)
                self.assertEqual(model.transcribe.call_args.kwargs['language'], language)

    @patch('subtitles.processing._run')
    @patch('subtitles.processing.extract_audio')
    def test_whisper_receives_language_only_when_selected(self, extract, run):
        run.return_value.stdout = '[{"start": 0, "end": 1, "text": "Привіт!"}]'
        with tempfile.TemporaryDirectory() as root:
            source, output = Path(root) / 'input.mp4', Path(root) / 'output.srt'
            generate_subtitles(source, output)
            self.assertNotIn('--language', run.call_args.args[0])
            generate_subtitles(source, output, language='uk')
            self.assertEqual(run.call_args.args[0][-2:], ['--language', 'uk'])

    def test_srt_timestamps_and_unicode(self):
        result = render_srt([Segment(0, 1.9996, 'Привіт!')])
        self.assertEqual(result, '1\n00:00:00,000 --> 00:00:02,000\nПривіт!\n\n')

    def test_invalid_timestamps_are_rejected(self):
        with self.assertRaises(ProcessingError):
            render_srt([Segment(2, 1, 'bad')])

    @patch('subtitles.processing.extract_audio')
    def test_generates_utf8_file(self, extract):
        class Recognizer:
            def recognize(self, audio):
                return [Segment(0, 1, 'Привіт!')]
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'output.srt'
            generate_subtitles(Path(root) / 'input.mp4', output, Recognizer())
            self.assertIn('Привіт!', output.read_text(encoding='utf-8'))

    @patch('subtitles.processing.subprocess.run')
    def test_missing_audio_is_rejected(self, run):
        from subtitles.processing import extract_audio
        run.return_value.stdout = '{"streams": []}'
        with self.assertRaisesMessage(ProcessingError, 'аудіодоріжки'):
            extract_audio(Path('input.mp4'), Path('audio.wav'))

    def test_burn_video_preserves_previous_result_on_failure(self):
        from subtitles.processing import burn_subtitles
        with tempfile.TemporaryDirectory() as root:
            source, captions, output = [Path(root) / name for name in ('source.mp4', 'captions.srt', 'result.mp4')]
            captions.write_text('test', encoding='utf-8')
            output.write_bytes(b'previous-result')
            with patch('subtitles.processing._run', side_effect=ProcessingError('render failed')):
                with self.assertRaises(ProcessingError):
                    burn_subtitles(source, captions, output)
            self.assertEqual(output.read_bytes(), b'previous-result')

    def test_burn_handles_subtitle_paths_with_quotes(self):
        from subtitles.processing import burn_subtitles
        with tempfile.TemporaryDirectory(prefix="quote's-") as root:
            source, captions, output = [Path(root) / name for name in ('source.mp4', "caption's.srt", 'result.mp4')]
            captions.write_text('captions', encoding='utf-8')
            def encode(command, timeout, **kwargs):
                self.assertEqual(command[command.index('-vf') + 1], 'subtitles=filename=captions.srt,pad=ceil(iw/2)*2:ceil(ih/2)*2')
                self.assertIn('+faststart', command)
                Path(command[-1]).write_bytes(b'mp4')
            with patch('subtitles.processing._run', side_effect=encode):
                burn_subtitles(source, captions, output)
            self.assertEqual(output.read_bytes(), b'mp4')

    def test_burn_applies_the_chosen_style_to_the_video_filter(self):
        from subtitles.processing import burn_subtitles
        styles = {
            'yellow': 'PrimaryColour=&H004CDCFF',
            'dark-box': 'BorderStyle=3',
            'cinema': 'Italic=-1',
            'neon': 'PrimaryColour=&H00F9E67C',
            'studio': 'OutlineColour=&H00FFCFC5',
        }
        with tempfile.TemporaryDirectory() as root:
            source, captions, output = [Path(root) / name for name in ('source.mp4', 'captions.srt', 'result.mp4')]
            srt = '1\n00:00:00,000 --> 00:00:01,000\nПривіт!\n\n'
            captions.write_text(srt, encoding='utf-8')
            for style, setting in styles.items():
                with self.subTest(style=style):
                    def encode(command, timeout, **kwargs):
                        self.assertIn(setting, command[command.index('-vf') + 1])
                        Path(command[-1]).write_bytes(b'mp4')

                    with patch('subtitles.processing._run', side_effect=encode):
                        burn_subtitles(source, captions, output, subtitle_style=style)
            self.assertEqual(captions.read_text(encoding='utf-8'), srt)

    def test_cancellation_kills_and_reaps_running_subprocess(self):
        import subprocess
        import sys
        import time
        from subtitles.processing import ProcessingCancelled, _run
        started = time.monotonic()
        processes = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        def cancel():
            if time.monotonic() - started > 0.1:
                raise ProcessingCancelled('cancelled')
        with patch('subtitles.processing.subprocess.Popen', side_effect=spawn):
            with self.assertRaises(ProcessingCancelled):
                _run([sys.executable, '-c', 'import time; time.sleep(5)'], 10, check_cancel=cancel)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(processes[0].poll())
