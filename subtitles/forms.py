from django import forms
from django.conf import settings
from django.core.validators import FileExtensionValidator


class UploadForm(forms.Form):
    video = forms.FileField(
        label='Відеофайл',
        validators=[FileExtensionValidator(['mp4', 'mov', 'mkv', 'webm', 'avi'])],
    )

    def clean_video(self):
        video = self.cleaned_data['video']
        if video.size > settings.MAX_UPLOAD_BYTES:
            raise forms.ValidationError('Максимальний розмір файлу — 100 МБ.')
        return video
