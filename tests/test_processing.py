import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from subtitles.processing import ProcessingError, Segment, generate_subtitles, render_srt


class ProcessingTests(SimpleTestCase):
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
                self.assertIn('subtitles=filename=captions.srt', command[command.index('-vf') + 1])
                self.assertIn('+faststart', command)
                Path(command[-1]).write_bytes(b'mp4')
            with patch('subtitles.processing._run', side_effect=encode):
                burn_subtitles(source, captions, output)
            self.assertEqual(output.read_bytes(), b'mp4')
