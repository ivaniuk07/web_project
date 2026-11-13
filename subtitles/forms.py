from django import forms
from django.conf import settings

from .languages import SubtitleLanguage
from .subtitle_styles import SubtitleStyle
from .validation import validate_upload


class UploadForm(forms.Form):
    video = forms.FileField(
        label='Відеофайл',
    )
    subtitle_language = forms.ChoiceField(
        label='Мова субтитрів',
        choices=SubtitleLanguage.choices,
        required=False,
        help_text='Оберіть мову, якою говорять у відео.',
    )
    subtitle_style = forms.ChoiceField(
        label='Теми субтитрів',
        choices=SubtitleStyle.choices,
        initial=SubtitleStyle.CLASSIC,
        required=False,
        widget=forms.RadioSelect(attrs={'class': 'visually-hidden'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        maximum = settings.MAX_UPLOAD_BYTES / (1024 * 1024)
        minutes, seconds = divmod(settings.MAX_VIDEO_SECONDS, 60)
        duration = f'{minutes} хв' if not seconds else f'{settings.MAX_VIDEO_SECONDS} с'
        formats = ', '.join(settings.ALLOWED_VIDEO_EXTENSIONS)
        self.fields['video'].help_text = f'До {maximum:g} МіБ і {duration}. Формати: {formats}.'
        self.fields['video'].widget.attrs.update({
            'class': 'visually-hidden',
            'accept': ','.join(f'.{extension}' for extension in settings.ALLOWED_VIDEO_EXTENSIONS),
        })

    def clean_video(self):
        video = self.cleaned_data['video']
        validate_upload(video)
        return video

    def clean_subtitle_style(self):
        return self.cleaned_data['subtitle_style'] or SubtitleStyle.CLASSIC
